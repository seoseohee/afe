"""
ecc_core/executor.py

LLM이 요청한 tool_use를 실제 보드 명령으로 실행한다.

Claude Code의 tool executor와의 차이:
  - 모든 명령이 SSH를 통해 원격 실행됨
  - 실패가 '예외'가 아닌 '정상 상태'로 처리됨
  - probe 같은 임베디드 전용 도구 처리
  - 위험 명령 검사
"""

import json
import threading
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .connection import BoardConnection, ExecResult
from .tools import is_dangerous, PROBE_COMMANDS, VERIFY_COMMANDS
from .todo import TodoManager

if TYPE_CHECKING:
    pass


# ── 백그라운드 task 저장소 ─────────────────────────────────────────

@dataclass
class _BgTask:
    task_id: str
    cmd: str
    result: "ExecResult | None" = field(default=None)
    done: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_result(self, r: "ExecResult") -> None:
        with self._lock:
            self.result = r
            self.done = True

    def wait(self, timeout: float) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.done:
                return True
            time.sleep(0.2)
        return self.done


class ToolExecutor:
    """
    각 도구 이름을 받아서 실제 동작으로 디스패치.
    done이 호출되면 is_finished = True.

    v5: conn은 None일 수 있다.
    loop.py가 ssh_connect 후 executor.conn을 갱신해준다.
    conn=None 상태에서 bash 등을 호출하면 loop.py에서 먼저 차단되므로
    여기서는 conn이 있다고 가정해도 된다.
    """

    def __init__(self, conn, todos: TodoManager, verbose: bool = False):
        self.conn = conn
        self.todos = todos
        self.verbose = verbose
        self.is_finished = False
        self._bg_tasks: dict[str, _BgTask] = {}   # task_id → _BgTask

    def execute(self, tool_name: str, tool_input: dict) -> str:
        """
        tool_name에 따라 실행하고 결과 문자열 반환.
        반환값은 LLM의 tool_result content가 된다.
        """
        dispatch = {
            "bash":      self._bash,
            "bash_wait": self._bash_wait,
            "script":    self._script,
            "read":      self._read,
            "write":     self._write,
            "glob":      self._glob,
            "grep":      self._grep,
            "probe":     self._probe,
            "verify":    self._verify,
            "todo":      self._todo,
            "subagent":  self._subagent,
            "done":      self._done,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            return f"[error] 알 수 없는 도구: {tool_name}"
        return handler(tool_input)

    # ─── bash ──────────────────────────────────────────────────

    def _bash(self, inp: dict) -> str:
        cmd = inp["command"]
        timeout = inp.get("timeout", 30)
        desc = inp.get("description", "")
        background = inp.get("background", False)

        _print_tool("bash", f"{cmd[:100]}", desc)

        if is_dangerous(cmd):
            return "[blocked] 위험한 명령으로 판단되어 실행을 거부했습니다."

        if background:
            task_id = _uuid_mod.uuid4().hex[:8]
            task = _BgTask(task_id=task_id, cmd=cmd)
            self._bg_tasks[task_id] = task

            def _run():
                r = self.conn.run(cmd, timeout=timeout)
                task.set_result(r)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            print(f"    ⏳ background task_id={task_id}", flush=True)
            return (
                f"[background] task_id={task_id}\n"
                f"Command is running in background. "
                f"Use bash_wait(task_id='{task_id}') to retrieve the result."
            )

        result = self.conn.run(cmd, timeout=timeout)
        _print_result(result)
        return result.to_tool_result()

    # ─── bash_wait ─────────────────────────────────────────────

    def _bash_wait(self, inp: dict) -> str:
        task_id = inp["task_id"]
        wait_timeout = inp.get("timeout", 120)
        desc = inp.get("description", "")

        _print_tool("bash_wait", f"task_id={task_id}", desc)

        task = self._bg_tasks.get(task_id)
        if task is None:
            return f"[error] task_id '{task_id}' not found. Valid IDs: {list(self._bg_tasks.keys())}"

        finished = task.wait(timeout=wait_timeout)
        if not finished:
            return (
                f"[timeout] task_id={task_id} is still running after {wait_timeout}s.\n"
                "Call bash_wait again with a longer timeout, or continue other work."
            )

        result = task.result
        del self._bg_tasks[task_id]   # 수집 후 정리
        _print_result(result)
        return result.to_tool_result()

    # ─── script ────────────────────────────────────────────────

    def _script(self, inp: dict) -> str:
        code = inp["code"]
        interpreter = inp.get("interpreter", "bash")
        timeout = inp.get("timeout", 60)
        desc = inp.get("description", "")

        lines = code.strip().splitlines()
        _print_tool("script", f"[{interpreter}] {len(lines)}줄", desc)

        if self.verbose:
            preview = "\n    ".join(lines[:6])
            suffix = "\n    ..." if len(lines) > 6 else ""
            print(f"    {preview}{suffix}")

        result = self.conn.upload_and_run(code, interpreter=interpreter, timeout=timeout)
        _print_result(result)
        return result.to_tool_result()

    # ─── read ──────────────────────────────────────────────────

    def _read(self, inp: dict) -> str:
        path = inp["path"]
        head = inp.get("head_lines", 0)
        tail = inp.get("tail_lines", 0)

        _print_tool("read", path)

        if head > 0:
            cmd = f"head -n {head} {path}"
        elif tail > 0:
            cmd = f"tail -n {tail} {path}"
        else:
            cmd = f"cat {path}"

        result = self.conn.run(cmd, timeout=15)
        _print_result(result)
        return result.to_tool_result()

    # ─── write ─────────────────────────────────────────────────

    def _write(self, inp: dict) -> str:
        path = inp["path"]
        content = inp["content"]
        mode = inp.get("mode", "")

        _print_tool("write", path)

        result = self.conn.upload_and_run(
            f"mkdir -p $(dirname {path})",
            interpreter="bash",
            timeout=10
        )

        result = self.conn.upload_and_run(
            content,
            interpreter=f"bash -c 'cat > {path}'",
            timeout=15
        )

        if result.ok and mode:
            self.conn.run(f"chmod {mode} {path}", timeout=5)

        _print_result(result)
        return result.to_tool_result()

    # ─── glob ──────────────────────────────────────────────────

    def _glob(self, inp: dict) -> str:
        pattern = inp["pattern"]
        base = inp.get("base_dir", "/")

        _print_tool("glob", pattern)

        if pattern.startswith("/"):
            cmd = f"find / -path '{pattern}' 2>/dev/null | head -50"
        else:
            cmd = f"find {base} -path '*{pattern}*' 2>/dev/null | head -50"

        result = self.conn.run(cmd, timeout=20)
        _print_result(result)
        return result.to_tool_result()

    # ─── grep ──────────────────────────────────────────────────

    def _grep(self, inp: dict) -> str:
        pattern = inp["pattern"]
        path = inp["path"]
        flags = inp.get("flags", "-rn")
        max_results = inp.get("max_results", 50)

        _print_tool("grep", f'"{pattern}" in {path}')

        # rg가 있으면 rg, 없으면 grep
        cmd = (
            f"(rg {flags} --max-count {max_results} '{pattern}' {path} 2>/dev/null) "
            f"|| (grep {flags} --max-count {max_results} '{pattern}' {path} 2>/dev/null)"
        )
        result = self.conn.run(cmd, timeout=20)
        _print_result(result)
        return result.to_tool_result()

    # ─── probe ─────────────────────────────────────────────────

    def _probe(self, inp: dict) -> str:
        target = inp["target"]

        _print_tool("probe", f"[{target}]", "하드웨어/환경 탐지")

        cmd = PROBE_COMMANDS.get(target)
        if not cmd:
            return f"[error] 알 수 없는 probe target: {target}"

        result = self.conn.run(cmd, timeout=45)
        _print_result(result)
        return result.to_tool_result()

    # ─── todo ──────────────────────────────────────────────────

    def _todo(self, inp: dict) -> str:
        todos = inp.get("todos", [])
        self.todos.update(todos)
        formatted = self.todos.format_display()
        print(f"\n{formatted}")
        return f"[ok] todo 업데이트됨\n{self.todos.format_for_llm()}"

    # ─── verify ────────────────────────────────────────────────
    # probe(존재 확인)와 달리 실제 동작/응답을 확인
    # 어떤 하드웨어든 target + device로 범용 처리

    def _verify(self, inp: dict) -> str:
        target = inp["target"]
        device = inp.get("device", "")

        _print_tool("verify", f"[{target}] {device}", "동작 확인")

        if target == "custom":
            # custom은 device 내용을 bash로 직접 실행
            if device:
                result = self.conn.run(device, timeout=30)
            else:
                return "[error] custom verify: device에 확인할 bash 명령을 넣어라"
            _print_result(result)
            return result.to_tool_result()

        cmd_template = VERIFY_COMMANDS.get(target)
        if not cmd_template:
            return f"[error] 알 수 없는 verify target: {target}"

        # ECC_DEVICE 환경변수로 device 값 전달
        cmd = f"export ECC_DEVICE='{device}'\n{cmd_template}"
        result = self.conn.run(cmd, timeout=60)
        _print_result(result)

        # PASS/FAIL/WARN 한 줄 요약을 앞에 붙임
        out = result.to_tool_result()
        summary = " | ".join(
            l.strip() for l in result.output().splitlines()
            if any(k in l for k in ("PASS", "FAIL", "WARN", "OK"))
        )[:200]
        return (f"[verify:{target} {device}] {summary}\n\n{out}") if summary else out

    # ─── subagent ──────────────────────────────────────────────

    def _subagent(self, inp: dict) -> str:
        """
        독립 컨텍스트에서 탐색/분석 실행.
        이 메서드는 루프 밖에서 실제 API 호출이 일어나야 하므로
        AgentLoop에서 직접 처리한다.
        여기서는 placeholder만 반환.
        """
        # 실제 처리는 AgentLoop._run_subagent() 에서
        return "__subagent_placeholder__"

    # ─── done ──────────────────────────────────────────────────

    def _done(self, inp: dict) -> str:
        success = inp.get("success", False)
        summary = inp.get("summary", "")
        evidence = inp.get("evidence", "")
        notes = inp.get("notes", "")

        icon = "✅" if success else "❌"
        print(f"\n{'═'*60}")
        print(f"  {icon}  {summary}")
        if evidence:
            print(f"  🔍 Evidence: {evidence}")
        if notes:
            print(f"  📝 {notes}")
        print(f"{'═'*60}")

        self.is_finished = True
        return "done"


# ─────────────────────────────────────────────────────────────
# 출력 헬퍼
# ─────────────────────────────────────────────────────────────

def _print_tool(name: str, detail: str = "", desc: str = ""):
    desc_str = f"  # {desc}" if desc else ""
    print(f"\n  ▶ {name}  {detail}{desc_str}", flush=True)

def _print_result(result: ExecResult):
    out = result.output()
    if not out.strip():
        return
    if len(out) > 800:
        preview = out[:600]
        tail = out[-150:]
        print(f"  {preview}\n  ...({len(out)}자)...\n  {tail}", flush=True)
    else:
        # 들여쓰기
        for line in out.splitlines():
            print(f"  {line}", flush=True)