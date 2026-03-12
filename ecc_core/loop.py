"""
ecc_core/loop.py

ECC v5 — 연결부터 목표 달성까지 에이전트가 전부 담당.

환경변수:
  ECC_MODEL            메인 에이전트 모델 (기본: claude-sonnet-4-6)
  ECC_ESCALATE_MODEL   escalation 시 모델 (기본: ECC_MODEL의 sonnet→opus 자동 치환)
  ECC_ADAPTIVE_MODELS  adaptive thinking 지원 모델 목록, 쉼표 구분 (기본: 버전 4.6 이상 자동 감지)
  ECC_MAX_TOKENS       메인 에이전트 max_tokens (기본: 8096)
  ECC_THINKING         1이면 thinking 항상 활성화
  ECC_THINKING_BUDGET  thinking budget_tokens (기본: 8000, adaptive 모델엔 무시됨)
  ECC_COMPACT_MODEL    컨텍스트 압축용 모델 (기본: ECC_MODEL과 동일)
  ECC_SUBAGENT_TURNS   subagent 최대 루프 수 (기본: 40)
"""

import os
import time
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed

from .connection import BoardConnection, BoardDiscovery
from .todo import TodoManager
from .executor import ToolExecutor
from .compactor import should_compact, compact
from .prompt import build_system_prompt
from .tools import TOOL_DEFINITIONS


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _main_model() -> str:
    return os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

def _escalate_model() -> str:
    # escalation 시 쓸 모델 — 기본은 메인 모델의 opus 버전
    # ECC_ESCALATE_MODEL로 직접 지정 가능
    env = os.environ.get("ECC_ESCALATE_MODEL")
    if env:
        return env
    # 메인 모델에서 sonnet → opus 자동 추론
    main = _main_model()
    if "sonnet" in main:
        return main.replace("sonnet", "opus")
    return main  # 이미 opus거나 알 수 없으면 그대로

def _main_max_tokens() -> int:
    return _env_int("ECC_MAX_TOKENS", 8096)


# ─────────────────────────────────────────────────────────────
# Subagent
# ─────────────────────────────────────────────────────────────

SUBAGENT_TOOLS = [
    t for t in TOOL_DEFINITIONS
    if t["name"] not in ("subagent", "done")
] + [
    {
        "name": "report",
        "description": "Exploration complete. Return findings to the main agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "string",
                    "description": "Findings summary. Include specific values: paths, addresses, parameters, versions."
                }
            },
            "required": ["findings"]
        }
    }
]


def run_subagent(
    goal: str,
    context: str,
    conn: BoardConnection,
    client: anthropic.Anthropic,
    verbose: bool = False,
) -> str:
    system = (
        "You are a subagent for ECC. Perform the given task and call report().\n"
        "Be thorough. Batch independent commands. Do NOT spawn subagents.\n"
        f"SSH: {conn.user}@{conn.host}:{conn.port}\n"
        + (f"\nAlready known:\n{context}" if context else "")
    )

    todos = TodoManager()
    executor = ToolExecutor(conn, todos, verbose)
    messages: list[dict] = [{"role": "user", "content": goal}]
    max_turns = _env_int("ECC_SUBAGENT_TURNS", 40)
    turn = 0

    while True:
        resp = client.messages.create(
            model=_main_model(),
            max_tokens=4096,
            system=system,
            tools=SUBAGENT_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_results = []
        findings = ""
        finished = False

        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "report":
                findings = block.input.get("findings", "")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "reported"
                })
                finished = True
            else:
                out = executor.execute(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            return findings

        if resp.stop_reason == "end_turn" and not any(
            b.type == "tool_use" for b in resp.content
        ):
            # report() 없이 멈춤 → 다시 밀어준다
            messages.append({
                "role": "user",
                "content": (
                    "[system] You stopped without calling report(). "
                    "Complete the task and call report() with your findings."
                )
            })
            continue

        turn += 1
        if turn >= max_turns:
            messages.append({
                "role": "user",
                "content": (
                    f"[system] {turn} turns elapsed. "
                    "Wrap up and call report() with what you have found so far."
                )
            })
            max_turns += 20

    return "(subagent: no report)"


# ─────────────────────────────────────────────────────────────
# AgentLoop
# ─────────────────────────────────────────────────────────────

class AgentLoop:

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.client = anthropic.Anthropic()
        self.conn: BoardConnection | None = None

    def run(self, goal: str, max_turns: int = 100):
        print(f"\n{'═'*60}")
        print(f"  🎯 {goal[:80]}")
        print(f"{'═'*60}")

        if self.conn and not self.conn.is_alive():
            print("  ⚠️  이전 연결이 끊어짐. 에이전트가 재연결합니다.")
            self.conn = None

        todos = TodoManager()
        executor = ToolExecutor(self.conn, todos, self.verbose)
        system = build_system_prompt()

        messages: list[dict] = [
            {"role": "user", "content": _build_initial_message(goal)}
        ]

        model = _main_model()
        max_tokens = _main_max_tokens()
        # thinking이 켜지면 budget + 응답 공간 확보
        if _thinking_enabled():
            max_tokens = max(max_tokens, _thinking_budget() + 4096)
        escalation = EscalationTracker()
        turn = 0
        while True:

            if should_compact(messages):
                messages = compact(messages, goal, todos.format_for_llm(), self.client)

            nag = todos.format_nag()
            conn_status = (
                f"[Connected: {self.conn.address}]"
                if self.conn else
                "[Not connected — call ssh_connect first]"
            )
            system_with_state = (
                system
                + f"\n\nCurrent connection: {conn_status}"
                + (f"\n\n{nag}" if nag else "")
            )

            try:
                # escalation 체크 — 이번 turn에 opus+thinking 써야 하는지
                escalate, reason = escalation.should_escalate()
                turn_model = _escalate_model() if escalate else model
                turn_thinking = escalate or _thinking_enabled()
                # adaptive 모델은 budget_tokens 없으므로 max_tokens 별도 확장 불필요
                if turn_thinking and not _supports_adaptive(turn_model):
                    turn_max_tokens = max(max_tokens, _thinking_budget() + 4096)
                else:
                    turn_max_tokens = max_tokens

                if escalate:
                    print(f"\n  🔺 Escalate → opus+thinking ({reason})", flush=True)

                create_kwargs = dict(
                    model=turn_model,
                    max_tokens=turn_max_tokens,
                    system=system_with_state,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
                if turn_thinking:
                    create_kwargs["thinking"] = _thinking_params(turn_model)
                resp = self.client.messages.create(**create_kwargs)

                if escalate:
                    escalation.reset_escalation()  # sonnet으로 복귀
            except anthropic.RateLimitError as e:
                wait = 60
                print(f"\n  ⏳ Rate limit (429) — {wait}초 대기 후 재시도...", flush=True)
                time.sleep(wait)
                continue
            except anthropic.BadRequestError as e:
                err_msg = str(e).lower()
                # 컨텍스트 초과 에러: "prompt is too long", "input is too long",
                # "context window", "too many tokens" 등 다양한 형식
                is_context_error = any(kw in err_msg for kw in (
                    "context", "too long", "too many token", "input length",
                    "prompt_too_long", "prompt is too long",
                ))
                if is_context_error:
                    print(f"\n  ⚠️  컨텍스트 초과 감지 — 압축 후 재시도", flush=True)
                    messages = compact(messages, goal, todos.format_for_llm(), self.client)
                    continue
                raise

            messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if block.type == "thinking" and block.thinking.strip():
                    _print_thinking(block.thinking)
                elif block.type == "text" and block.text.strip():
                    print(f"\n  💬 {block.text.strip()}", flush=True)

            has_tools = any(b.type == "tool_use" for b in resp.content)
            if resp.stop_reason == "end_turn" and not has_tools:
                # done()을 호출하지 않고 멈춘 경우 → 다시 밀어준다
                print("\n  ⚠️  done() 없이 멈춤. 계속 진행 요청...", flush=True)
                messages.append({
                    "role": "user",
                    "content": (
                        "[system] You stopped without calling done(). "
                        "The goal is not complete until you explicitly call done(). "
                        "Continue working toward the goal, or call done(success=false) "
                        "if it is proven impossible."
                    )
                })
                continue

            tool_results = []
            tool_blocks = [b for b in resp.content if b.type == "tool_use"]

            # ssh_connect / subagent / no-conn 오류는 순서 의존 → 직렬 처리
            # bash, script, read, write, glob, grep, probe, verify → 병렬 가능
            PARALLEL_TOOLS = {"bash", "bash_wait", "script", "read", "write", "glob", "grep", "probe", "verify", "todo"}

            serial_blocks  = [b for b in tool_blocks if b.name not in PARALLEL_TOOLS or self.conn is None]
            parallel_blocks = [b for b in tool_blocks if b.name in PARALLEL_TOOLS and self.conn is not None]

            # 직렬 실행
            serial_results: dict[str, str] = {}
            for block in serial_blocks:
                if block.name == "ssh_connect":
                    out = self._handle_ssh_connect(block.input)
                    executor.conn = self.conn
                elif self.conn is None:
                    out = (
                        "[no connection] SSH connection required before using this tool.\n"
                        "Call ssh_connect first. If you don't know the host, use host='scan'."
                    )
                elif block.name == "subagent":
                    known = _extract_known_context(messages)
                    out = run_subagent(
                        goal=block.input.get("goal", ""),
                        context=block.input.get("context", known),
                        conn=self.conn,
                        client=self.client,
                        verbose=self.verbose,
                    )
                else:
                    out = executor.execute(block.name, block.input)
                serial_results[block.id] = out

            # 병렬 실행
            parallel_results: dict[str, str] = {}
            if parallel_blocks:
                with ThreadPoolExecutor(max_workers=min(len(parallel_blocks), 8)) as pool:
                    futures = {
                        pool.submit(executor.execute, b.name, b.input): b.id
                        for b in parallel_blocks
                    }
                    for future in as_completed(futures):
                        bid = futures[future]
                        try:
                            parallel_results[bid] = future.result()
                        except Exception as e:
                            parallel_results[bid] = f"[error] {e}"

            # 원래 순서대로 tool_results 조립
            all_results = {**serial_results, **parallel_results}
            for block in tool_blocks:
                out = all_results.get(block.id, "[error] no result")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })

            # escalation tracker에 기록
            escalation.record_tool_results(tool_blocks, all_results)

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if executor.is_finished:
                break

            if self.conn and turn > 0 and turn % 10 == 0:
                self._check_connection(messages)

            turn += 1
            if turn >= max_turns:
                print(f"\n  ⚠️  {turn} turns elapsed. goal 미완료 — 계속 진행...", flush=True)
                messages.append({
                    "role": "user",
                    "content": (
                        f"[system] {turn} turns elapsed. "
                        "The goal is still not complete. Keep working. "
                        "Call done() only when the goal is achieved or proven impossible."
                    )
                })
                max_turns += 50

    def _handle_ssh_connect(self, inp: dict) -> str:
        host = inp.get("host", "").strip()
        user = inp.get("user", "").strip() or None
        port = int(inp.get("port", 22))

        print(f"\n  🔗 ssh_connect: host={host} user={user or 'auto'} port={port}", flush=True)

        if host.lower() == "scan" or not host:
            print("  🔍 네트워크 자동 탐색 중...", flush=True)
            conn = BoardDiscovery.scan(user=user, port=port)
            if conn:
                self.conn = conn
                print(f"  ✅ 발견 및 연결: {conn.address}")
                return f"[ssh_connect ok] Connected to {conn.address}"
            else:
                print("  ❌ 탐색 실패")
                subnets = ", ".join(BoardDiscovery._default_subnets()[:4])
                users   = ", ".join(BoardDiscovery._default_users())
                return (
                    "[ssh_connect failed] Automatic scan found no board.\n"
                    "Keep trying — the board is there. Do NOT ask the user.\n\n"
                    "Try in order:\n"
                    f"1. Specific IPs in scanned subnets ({subnets})\n"
                    f"2. Other users: {users}\n"
                    "3. Port 2222\n"
                    "4. Broader subnets — try ssh_connect with host='scan' again or specific IP\n"
                    "Use ssh_connect with specific IPs until you find it."
                )

        conn = BoardDiscovery.from_hint(host, user, port)
        if conn:
            self.conn = conn
            print(f"  ✅ 연결: {conn.address}")
            return f"[ssh_connect ok] Connected to {conn.address}"
        else:
            print(f"  ❌ {host} 연결 실패")
            return (
                f"[ssh_connect failed] Could not connect to {host}:{port}\n"
                f"Tried users: {BoardDiscovery._default_users() if not user else [user]}\n"
                "Suggestions:\n"
                f"- Verify the board is reachable: try ssh_connect with host='{host}' and different user\n"
                "- Try ssh_connect with host='scan' to search the network\n"
                "- Check if SSH is enabled on the board\n"
                "- Try a different port (2222, 2200)"
            )

    def _check_connection(self, messages: list[dict]):
        if not (self.conn.likely_disconnected or not self.conn.is_alive()):
            return

        print("\n  🔄 연결 끊김 감지, 재연결 시도...", flush=True)
        if self.conn.reconnect(max_attempts=3):
            print("  ✅ 재연결 성공")
            messages.append({
                "role": "user",
                "content": (
                    "[SSH reconnected]\n"
                    "Connection was lost and restored. "
                    "Check board state (running processes, temp files) before continuing."
                )
            })
        else:
            print("  ❌ 자동 재연결 실패. 에이전트에게 알림.")
            self.conn = None
            messages.append({
                "role": "user",
                "content": (
                    "[SSH connection lost — reconnect failed]\n"
                    "Automatic reconnect (3 attempts) failed.\n"
                    "Use ssh_connect to re-establish connection before continuing."
                )
            })


def _build_initial_message(goal: str) -> str:
    return goal


# ─────────────────────────────────────────────────────────────
# Escalation Tracker
# ─────────────────────────────────────────────────────────────

class EscalationTracker:
    """
    "실행은 됐는데 효과가 없는" 상황을 감지해서
    다음 turn을 opus+thinking으로 자동 escalate.

    트리거 조건 (OR):
      1. verify FAIL 2회 연속
      2. 직전 2개 tool_result에 동일한 실패 패턴 반복
      3. 같은 bash 명령 3회 이상 반복 (polling 제외)
    """

    POLLING_KEYWORDS = ("hz", "echo --once", "topic echo", "ps aux", "is-active", "ping")
    # SSH 탐색 실패는 정상 과정 — escalation 대상 아님
    # 하드웨어 실패 패턴만: 명령은 됐는데 효과가 없는 경우
    FAIL_KEYWORDS    = ("exit code 1", "exit code 255", "no data",
                        "speed: 0.0", "speed=0.0", "0 publishers",
                        "rc=-1", "timed out", "no response")

    def __init__(self):
        self._verify_fail_streak: int = 0
        self._recent_results: list[str] = []   # 최근 tool_result 텍스트 (ssh_connect 제외)
        self._bash_counter: dict[str, int] = {}  # 명령 → 호출 횟수

    def record_tool_results(self, tool_blocks: list, results: dict[str, str]) -> None:
        for block in tool_blocks:
            out = results.get(block.id, "")

            # ssh_connect 결과는 escalation 추적 제외
            # — 탐색 실패는 정상 과정이고 'fail' 키워드가 오탐을 유발함
            if block.name == "ssh_connect":
                continue

            # verify FAIL 스트릭
            if block.name == "verify":
                if "FAIL" in out or "fail" in out.lower():
                    self._verify_fail_streak += 1
                else:
                    self._verify_fail_streak = 0

            # bash 반복 감지 (polling 명령은 제외)
            if block.name == "bash":
                cmd = block.input.get("command", "")
                if not any(kw in cmd for kw in self.POLLING_KEYWORDS):
                    self._bash_counter[cmd] = self._bash_counter.get(cmd, 0) + 1

            # 최근 결과 누적 (최대 4개, ssh_connect 제외)
            if out:
                self._recent_results.append(out.lower()[:300])
                if len(self._recent_results) > 4:
                    self._recent_results.pop(0)

    def should_escalate(self) -> tuple[bool, str]:
        """(escalate 여부, 이유) 반환"""

        # 조건 1: verify FAIL 2회 연속
        if self._verify_fail_streak >= 2:
            return True, f"verify FAIL {self._verify_fail_streak}회 연속"

        # 조건 2: 직전 2개 결과에 동일 실패 패턴
        if len(self._recent_results) >= 2:
            last_two = self._recent_results[-2:]
            for kw in self.FAIL_KEYWORDS:
                if all(kw in r for r in last_two):
                    return True, f"동일 실패 패턴 반복: '{kw}'"

        # 조건 3: 같은 bash 명령 3회 이상
        for cmd, count in self._bash_counter.items():
            if count >= 3:
                return True, f"bash 명령 {count}회 반복: '{cmd[:60]}'"

        return False, ""

    def reset_escalation(self) -> None:
        """escalate 후 리셋 — sonnet으로 복귀"""
        self._verify_fail_streak = 0
        self._bash_counter.clear()
        self._recent_results.clear()

def _thinking_enabled() -> bool:
    return os.environ.get("ECC_THINKING", "").lower() in ("1", "true", "yes")

def _thinking_budget() -> int:
    return _env_int("ECC_THINKING_BUDGET", 8000)

# adaptive thinking 지원 모델 — ECC_ADAPTIVE_MODELS로 추가 가능
# 기본: 4.6 이상 모델은 adaptive 지원 (모델명에 숫자 버전으로 판단)
def _supports_adaptive(model: str) -> bool:
    env = os.environ.get("ECC_ADAPTIVE_MODELS")
    if env:
        return any(m.strip() in model for m in env.split(","))
    # 기본 규칙: major.minor >= 4.6 이면 adaptive 지원
    import re
    m = re.search(r"(\d+)[\.-](\d+)", model)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        return (major, minor) >= (4, 6)
    return False

def _thinking_params(model: str) -> dict:
    if _supports_adaptive(model):
        return {"type": "adaptive"}
    return {"type": "enabled", "budget_tokens": _thinking_budget()}

def _print_thinking(text: str) -> None:
    """thinking 블록을 접어서 출력 — 첫 줄 + 길이 표시."""
    lines = text.strip().splitlines()
    first = lines[0][:120] if lines else ""
    total_chars = len(text)
    print(f"\n  🧠 thinking ({total_chars}ch): {first}", flush=True)
    if len(lines) > 1:
        print(f"     ... ({len(lines)} lines)", flush=True)


def _extract_known_context(messages: list[dict]) -> str:
    import re
    context_lines = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                text = str(block.get("content", ""))
                for m in re.finditer(r'/dev/\w+', text):
                    line = f"device: {m.group()}"
                    if line not in context_lines:
                        context_lines.append(line)
                for m in re.finditer(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', text):
                    line = f"ip: {m.group()}"
                    if line not in context_lines:
                        context_lines.append(line)
                for m in re.finditer(r'(\w+(?:_\w+)*)\s*[:=]\s*([\w./\-]+)', text):
                    if len(m.group()) < 60:
                        line = f"param: {m.group()}"
                        if line not in context_lines:
                            context_lines.append(line)
    return "\n".join(context_lines[:30])
