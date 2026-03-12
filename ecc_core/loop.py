"""
ecc_core/loop.py

ECC — Embedded Claude Code 에이전트 루프.

CC 아키텍처를 임베디드 환경으로 확장:

  CC 원본 구조                   ECC 대응
  ──────────────────────────── ──────────────────────────────────────
  Bash(local cmd)              bash(SSH remote cmd)
  Write(local file)            script() / write() on board
  TaskOutput(async)            bash(background=True) + bash_wait()
  Read(local file)             read(remote path)
  Tool dispatcher              ToolExecutor + BoardConnection
  Context compaction           compactor.py (동일 방식)
  Parallel tool execution      ThreadPoolExecutor (동일 방식)
  Subagent (Task tool)         run_subagent() with report()

추가된 ECC 전용 레이어:
  - BoardConnection: SSH 세션 추상화 (CC의 로컬 shell에 해당)
  - EscalationTracker: 반복 실패 감지 → opus+thinking 자동 전환
  - ssh_connect: CC는 항상 연결돼 있지만 ECC는 연결이 첫 번째 목표

환경변수:
  ECC_MODEL            메인 에이전트 모델 (기본: claude-sonnet-4-6)
  ECC_ESCALATE_MODEL   escalation 시 모델 (기본: sonnet→opus 자동 치환)
  ECC_ADAPTIVE_MODELS  adaptive thinking 지원 모델, 쉼표 구분 (기본: 4.6 이상 자동)
  ECC_MAX_TOKENS       메인 에이전트 max_tokens (기본: 8096)
  ECC_THINKING         1이면 thinking 항상 활성화
  ECC_THINKING_BUDGET  thinking budget_tokens (기본: 8000, adaptive 모델엔 무시)
  ECC_COMPACT_MODEL    컨텍스트 압축용 모델 (기본: ECC_MODEL)
  ECC_SUBAGENT_TURNS   subagent 최대 루프 수 (기본: 40)
"""

import os
import re
import time
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed

from .connection import BoardConnection, BoardDiscovery
from .todo import TodoManager
from .executor import ToolExecutor
from .compactor import should_compact, compact
from .prompt import build_system_prompt
from .tools import TOOL_DEFINITIONS, get_tool_definitions


# ─────────────────────────────────────────────────────────────
# 환경변수 헬퍼
# ─────────────────────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _main_model() -> str:
    return os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

def _escalate_model() -> str:
    """escalation 시 사용할 모델 — 기본은 sonnet→opus 자동 치환."""
    env = os.environ.get("ECC_ESCALATE_MODEL")
    if env:
        return env
    main = _main_model()
    if "sonnet" in main:
        return main.replace("sonnet", "opus")
    return main

def _main_max_tokens() -> int:
    return _env_int("ECC_MAX_TOKENS", 8096)

def _thinking_enabled() -> bool:
    return os.environ.get("ECC_THINKING", "").lower() in ("1", "true", "yes")

def _thinking_budget() -> int:
    return _env_int("ECC_THINKING_BUDGET", 8000)

def _supports_adaptive(model: str) -> bool:
    """버전 4.6 이상 모델은 adaptive thinking 지원."""
    env = os.environ.get("ECC_ADAPTIVE_MODELS")
    if env:
        return any(m.strip() in model for m in env.split(","))
    m = re.search(r"(\d+)[\.-](\d+)", model)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        return (major, minor) >= (4, 6)
    return False

def _thinking_params(model: str) -> dict:
    if _supports_adaptive(model):
        return {"type": "adaptive"}
    return {"type": "enabled", "budget_tokens": _thinking_budget()}


# ─────────────────────────────────────────────────────────────
# Subagent — CC의 Task tool에 해당
#
# CC Task 특성을 그대로 계승:
#   - 독립적 탐색 단위 (메인과 다른 컨텍스트)
#   - report()로만 완료 신호 가능 (CC의 TaskOutput return에 해당)
#   - done() 없이 멈추면 다시 밀어줌
#   - subagent 안에서 subagent 금지
# ─────────────────────────────────────────────────────────────

# subagent가 쓸 수 있는 도구: ssh_connect, subagent, done 제외 + report 추가
SUBAGENT_TOOLS = [
    t for t in get_tool_definitions()
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
                    "description": (
                        "Complete findings summary. "
                        "Include specific values: device paths, IP addresses, "
                        "parameter names/values, topic names, service names, versions. "
                        "The main agent will act on this without re-investigating."
                    )
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
    """
    독립적 탐색 서브에이전트.
    CC의 Task(subagent=True) 구조를 계승 — 메인 루프와 분리된 컨텍스트.

    반드시 report()로만 종료. done()은 메인 에이전트 전용.
    """
    system = (
        "You are a subagent for ECC. Perform the given task and call report().\n"
        "Be thorough. Batch independent commands. Do NOT spawn subagents.\n"
        "Include specific values in your report: paths, addresses, parameters, versions.\n"
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

        # subagent도 병렬 실행 지원 (report는 직렬)
        SUBAGENT_PARALLEL = {"bash", "bash_wait", "script", "read", "write",
                             "glob", "grep", "probe", "verify", "todo"}

        serial_blocks   = [b for b in resp.content
                           if b.type == "tool_use" and b.name not in SUBAGENT_PARALLEL]
        parallel_blocks = [b for b in resp.content
                           if b.type == "tool_use" and b.name in SUBAGENT_PARALLEL]

        all_results: dict[str, str] = {}

        # 직렬 먼저 (report 포함)
        for block in serial_blocks:
            if block.name == "report":
                findings = block.input.get("findings", "")
                all_results[block.id] = "reported"
                finished = True
            else:
                all_results[block.id] = executor.execute(block.name, block.input)

        # 병렬
        if parallel_blocks and not finished:
            with ThreadPoolExecutor(max_workers=min(len(parallel_blocks), 8)) as pool:
                futures = {
                    pool.submit(executor.execute, b.name, b.input): b.id
                    for b in parallel_blocks
                }
                for future in as_completed(futures):
                    bid = futures[future]
                    try:
                        all_results[bid] = future.result()
                    except Exception as e:
                        all_results[bid] = f"[error] {e}"

        # tool_results 원래 순서대로 조립
        for block in resp.content:
            if block.type != "tool_use":
                continue
            out = all_results.get(block.id, "[error] no result")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": out,
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            return findings

        # report() 없이 멈추면 CC처럼 다시 밀어줌
        if resp.stop_reason == "end_turn" and not any(
            b.type == "tool_use" for b in resp.content
        ):
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
# AgentLoop — CC의 메인 루프를 임베디드 환경으로 확장
#
# CC와의 구조적 차이:
#   CC: 항상 로컬 연결 상태 → ssh_connect 불필요
#   ECC: conn=None에서 시작 → ssh_connect가 첫 번째 목표
#
# CC와 동일한 패턴:
#   - while True 루프 (max_turns는 soft limit)
#   - parallel tool execution (ThreadPoolExecutor)
#   - context compaction (85% 도달 시)
#   - done() 없이 end_turn → 다시 밀어줌
#   - rate limit → 60초 대기 후 재시도
# ─────────────────────────────────────────────────────────────

class AgentLoop:

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.client = anthropic.Anthropic()
        self.conn: BoardConnection | None = None
        # REPL 세션 간 컨텍스트 유지
        self._session_messages: list[dict] = []
        self._session_goal: str = ""
        self._session_todos: "TodoManager | None" = None
        self._session_executor: "ToolExecutor | None" = None

    # 짧은 후속 응답 판단 ("yes", "no", "ok", "ㅇㅇ" 등)
    @staticmethod
    def _is_followup(goal: str, has_session: bool) -> bool:
        stripped = goal.strip()
        if not has_session:
            return False
        if stripped.startswith("/"):
            return False
        # 단어 수 ≤ 6 이면 후속 응답으로 간주
        return len(stripped.split()) <= 6

    def run(self, goal: str, max_turns: int = 100):
        print(f"\n{'═'*60}")
        print(f"  🎯 {goal[:80]}")
        print(f"{'═'*60}")

        if self.conn and not self.conn.is_alive():
            print("  ⚠️  이전 연결이 끊어짐. 에이전트가 재연결합니다.")
            self.conn = None

        is_followup = self._is_followup(goal, bool(self._session_messages))

        if is_followup:
            # 이전 대화 이어받기: done() 이후 사용자 후속 입력
            todos = self._session_todos or TodoManager()
            executor = self._session_executor or ToolExecutor(self.conn, todos, self.verbose)
            executor.conn = self.conn
            executor.is_finished = False  # done() 플래그 초기화
            messages = self._session_messages + [
                {"role": "user", "content": f"[User follow-up] {goal}"}
            ]
            print(f"  🔁 이전 세션 이어받기 ({len(self._session_messages)}개 메시지)", flush=True)
        else:
            todos = TodoManager()
            executor = ToolExecutor(self.conn, todos, self.verbose)
            messages: list[dict] = [{"role": "user", "content": goal}]

        # 이번 세션의 goal 기록 (followup이면 원래 goal 유지)
        active_goal = self._session_goal if is_followup else goal

        # Ctrl+C 시 _save_partial_session()이 참조할 현재 상태 포인터
        self._current_goal = active_goal
        self._current_todos = todos
        self._current_executor = executor
        self._current_messages: list[dict] = messages  # 참조 (실시간 갱신)

        system = build_system_prompt()

        model = _main_model()
        max_tokens = _main_max_tokens()
        if _thinking_enabled():
            max_tokens = max(max_tokens, _thinking_budget() + 4096)

        escalation = EscalationTracker()
        turn = 0

        while True:

            # 컨텍스트 압축 (CC의 /compact와 동일한 85% 트리거)
            if should_compact(messages):
                messages = compact(messages, active_goal, todos.format_for_llm(), self.client)

            # 매 turn: 연결 상태 + todo nag를 system에 주입
            conn_status = (
                f"[Connected: {self.conn.address}]"
                if self.conn else
                "[Not connected — call ssh_connect first]"
            )
            nag = todos.format_nag()
            system_with_state = (
                system
                + f"\n\nCurrent connection: {conn_status}"
                + (f"\n\n{nag}" if nag else "")
            )

            try:
                # Escalation 체크 — 반복 실패 감지 시 opus+thinking 자동 전환
                escalate, reason = escalation.should_escalate()
                turn_model    = _escalate_model() if escalate else model
                turn_thinking = escalate or _thinking_enabled()

                # adaptive 모델은 budget_tokens 없으므로 max_tokens 별도 확장 불필요
                if turn_thinking and not _supports_adaptive(turn_model):
                    turn_max_tokens = max(max_tokens, _thinking_budget() + 4096)
                else:
                    turn_max_tokens = max_tokens

                if escalate:
                    print(f"\n  🔺 Escalate → {turn_model} + thinking ({reason})", flush=True)

                create_kwargs = dict(
                    model=turn_model,
                    max_tokens=turn_max_tokens,
                    system=system_with_state,
                    tools=get_tool_definitions(),
                    messages=messages,
                )
                if turn_thinking:
                    create_kwargs["thinking"] = _thinking_params(turn_model)

                resp = self.client.messages.create(**create_kwargs)

                if escalate:
                    escalation.reset_escalation()  # 다음 turn은 sonnet으로 복귀

            except anthropic.RateLimitError:
                wait = 60
                print(f"\n  ⏳ Rate limit (429) — {wait}초 대기 후 재시도...", flush=True)
                time.sleep(wait)
                continue

            except anthropic.BadRequestError as e:
                err_msg = str(e).lower()
                is_context_error = any(kw in err_msg for kw in (
                    "context", "too long", "too many token", "input length",
                    "prompt_too_long", "prompt is too long",
                ))
                if is_context_error:
                    print(f"\n  ⚠️  컨텍스트 초과 — 압축 후 재시도", flush=True)
                    messages = compact(messages, active_goal, todos.format_for_llm(), self.client)
                    continue
                raise

            # 중복 append 방지 (버그5: 동일 응답이 두 번 처리되는 경우)
            last_assistant = next(
                (m for m in reversed(messages) if m["role"] == "assistant"), None
            )
            if last_assistant and last_assistant["content"] is resp.content:
                continue  # 이미 처리된 응답 — 스킵
            messages.append({"role": "assistant", "content": resp.content})

            # ── 출력: thinking → text
            # 버그7: text block 안에 <thinking>...</thinking> 태그가 들어오는 경우 필터링
            seen_text = False
            for block in resp.content:
                if block.type == "thinking" and block.thinking.strip():
                    _print_thinking(block.thinking)
                elif block.type == "text" and block.text.strip():
                    if not seen_text:
                        text = block.text.strip()
                        # <thinking>...</thinking> 태그 제거
                        text = re.sub(r'<thinking>.*?</thinking>', '', text,
                                      flags=re.DOTALL).strip()
                        if text:
                            print(f"\n  💬 {text}", flush=True)
                        seen_text = True

            # ── end_turn without done() → CC처럼 다시 밀어줌
            has_tools = any(b.type == "tool_use" for b in resp.content)
            if resp.stop_reason == "end_turn" and not has_tools:
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

            # ── Tool 실행
            tool_blocks = [b for b in resp.content if b.type == "tool_use"]

            # CC와 동일한 직렬/병렬 분리:
            #   ssh_connect, subagent → 상태 변경 의존성 → 직렬
            #   bash, script, read, write, ... → 독립적 → 병렬
            PARALLEL_TOOLS = {
                "bash", "bash_wait", "script",
                "read", "write", "glob", "grep",
                "probe", "verify", "todo",
                "serial_open", "serial_send", "serial_close",
            }
            # ask_user는 터미널 입력이 필요 → 항상 직렬
            # ssh_connect, subagent도 상태 변경 → 직렬

            serial_blocks   = [b for b in tool_blocks
                               if b.name not in PARALLEL_TOOLS or self.conn is None]
            parallel_blocks = [b for b in tool_blocks
                               if b.name in PARALLEL_TOOLS and self.conn is not None]

            # 직렬 실행
            serial_results: dict[str, str] = {}
            for block in serial_blocks:
                if block.name == "ssh_connect":
                    out = self._handle_ssh_connect(block.input)
                    executor.conn = self.conn  # conn 갱신을 executor에 전파

                elif self.conn is None and block.name not in {
                    "bash", "bash_wait", "read", "write", "glob", "grep",
                    "todo", "done", "ask_user",
                }:
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

                elif block.name == "ask_user":
                    # conn 상태와 무관하게 항상 실행 가능
                    out = executor.execute("ask_user", block.input)

                else:
                    out = executor.execute(block.name, block.input)

                serial_results[block.id] = out

            # 병렬 실행 (CC의 parallel tool call 지원과 동일)
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
            tool_results = []
            for block in tool_blocks:
                out = all_results.get(block.id, "[error] no result")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })

            # escalation tracker에 이번 turn 결과 기록
            escalation.record_tool_results(tool_blocks, all_results)

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if executor.is_finished:
                # 세션 컨텍스트 저장 — REPL에서 후속 입력 대비
                self._session_messages = list(messages)
                self._session_goal = active_goal
                self._session_todos = todos
                self._session_executor = executor
                break

            # 10 turn마다 연결 상태 체크 (CC의 keepalive에 해당)
            if self.conn and turn > 0 and turn % 10 == 0:
                self._check_connection(messages)

            turn += 1
            if turn >= max_turns:
                print(f"\n  ⚠️  {turn} turns — 계속 진행...", flush=True)
                messages.append({
                    "role": "user",
                    "content": (
                        f"[system] {turn} turns elapsed. "
                        "The goal is still not complete. Keep working. "
                        "Call done() only when the goal is achieved or proven impossible."
                    )
                })
                max_turns += 50

    # ─────────────────────────────────────────────────────────
    # SSH 연결 핸들러
    # ─────────────────────────────────────────────────────────

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
                    "4. Broader subnets or ssh_connect(host='scan') again\n"
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
                f"- Try a different user: ssh_connect(host='{host}', user='ubuntu')\n"
                "- Try ssh_connect(host='scan') to search the network\n"
                "- Try a different port: ssh_connect(host='{host}', port=2222)\n"
            )

    # ─────────────────────────────────────────────────────────
    # 연결 상태 모니터링 — CC의 keepalive에 해당
    # ─────────────────────────────────────────────────────────

    def _save_partial_session(self):
        """Ctrl+C 중단 시에도 부분 세션을 저장해서 followup 가능하게 한다."""
        if hasattr(self, '_current_messages') and self._current_messages:
            self._session_messages = list(self._current_messages)
            self._session_goal = getattr(self, '_current_goal', self._session_goal)
            self._session_todos = getattr(self, '_current_todos', self._session_todos)
            self._session_executor = getattr(self, '_current_executor', self._session_executor)

    def _check_connection(self, messages: list[dict]):
        """연결 끊김 감지 시 재연결 시도. 결과를 user 메시지로 주입."""
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
            print("  ❌ 자동 재연결 실패.")
            self.conn = None
            messages.append({
                "role": "user",
                "content": (
                    "[SSH connection lost — reconnect failed]\n"
                    "Automatic reconnect (3 attempts) failed.\n"
                    "Use ssh_connect to re-establish connection before continuing."
                )
            })


# ─────────────────────────────────────────────────────────────
# Escalation Tracker
#
# CC에는 없는 ECC 전용 레이어.
# "명령은 됐는데 효과가 없는" 임베디드 특유의 상황을 감지해서
# opus+thinking으로 자동 전환.
#
# CC는 사람이 /model 명령으로 모델을 바꾸지만,
# ECC는 자동화 환경이므로 트래커가 그 역할을 대신함.
# ─────────────────────────────────────────────────────────────

class EscalationTracker:
    """
    트리거 조건 (OR):
      1. verify FAIL 2회 연속
      2. 직전 2개 tool_result에 동일한 하드웨어 실패 패턴
      3. 같은 bash 명령 3회 이상 반복 (polling 제외)
    """

    POLLING_KEYWORDS = ("hz", "echo --once", "topic echo", "ps aux", "is-active", "ping")

    # 하드웨어 실패 패턴 — ssh 탐색 실패 같은 정상적 실패는 제외
    FAIL_KEYWORDS = (
        "exit code 1", "exit code 255", "no data",
        "speed: 0.0", "speed=0.0", "0 publishers",
        "rc=-1", "timed out", "no response",
    )

    def __init__(self):
        self._verify_fail_streak: int = 0
        self._recent_results: list[str] = []   # 최근 tool_result (ssh_connect 제외)
        self._bash_counter: dict[str, int] = {}  # 명령 → 호출 횟수

    def record_tool_results(self, tool_blocks: list, results: dict[str, str]) -> None:
        for block in tool_blocks:
            out = results.get(block.id, "")

            # ssh_connect 탐색 실패는 정상 과정 — escalation 대상 아님
            if block.name == "ssh_connect":
                continue

            # verify FAIL 스트릭
            if block.name == "verify":
                if "FAIL" in out or "fail" in out.lower():
                    self._verify_fail_streak += 1
                else:
                    self._verify_fail_streak = 0

            # bash 반복 감지 (polling 명령 제외)
            if block.name == "bash":
                cmd = block.input.get("command", "")
                if not any(kw in cmd for kw in self.POLLING_KEYWORDS):
                    self._bash_counter[cmd] = self._bash_counter.get(cmd, 0) + 1

            # 최근 결과 누적 (최대 4개)
            if out:
                self._recent_results.append(out.lower()[:300])
                if len(self._recent_results) > 4:
                    self._recent_results.pop(0)

    def should_escalate(self) -> tuple[bool, str]:
        """(escalate 여부, 이유) 반환"""

        if self._verify_fail_streak >= 2:
            return True, f"verify FAIL {self._verify_fail_streak}회 연속"

        if len(self._recent_results) >= 2:
            last_two = self._recent_results[-2:]
            for kw in self.FAIL_KEYWORDS:
                if all(kw in r for r in last_two):
                    return True, f"동일 실패 패턴 반복: '{kw}'"

        for cmd, count in self._bash_counter.items():
            if count >= 3:
                return True, f"bash 명령 {count}회 반복: '{cmd[:60]}'"

        return False, ""

    def reset_escalation(self) -> None:
        """escalate 후 리셋 — 다음 turn은 메인 모델로 복귀."""
        self._verify_fail_streak = 0
        self._bash_counter.clear()
        self._recent_results.clear()


# ─────────────────────────────────────────────────────────────
# 출력 헬퍼
# ─────────────────────────────────────────────────────────────

def _print_thinking(text: str) -> None:
    """thinking 블록을 접어서 출력 — 첫 줄 + 길이 표시."""
    lines = text.strip().splitlines()
    first = lines[0][:120] if lines else ""
    total_chars = len(text)
    print(f"\n  🧠 thinking ({total_chars}ch): {first}", flush=True)
    if len(lines) > 1:
        print(f"     ... ({len(lines)} lines)", flush=True)


# ─────────────────────────────────────────────────────────────
# 컨텍스트 추출 — subagent에게 전달할 "이미 아는 것" 요약
# ─────────────────────────────────────────────────────────────

def _extract_known_context(messages: list[dict]) -> str:
    """
    메시지 히스토리에서 device path, IP, 파라미터를 추출.
    subagent가 이미 발견된 것을 중복 탐색하지 않도록.
    """
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
                for m in re.finditer(r"/dev/\w+", text):
                    line = f"device: {m.group()}"
                    if line not in context_lines:
                        context_lines.append(line)
                for m in re.finditer(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text):
                    line = f"ip: {m.group()}"
                    if line not in context_lines:
                        context_lines.append(line)
                for m in re.finditer(r"(\w+(?:_\w+)*)\s*[:=]\s*([\w./\-]+)", text):
                    if len(m.group()) < 60:
                        line = f"param: {m.group()}"
                        if line not in context_lines:
                            context_lines.append(line)
    return "\n".join(context_lines[:30])
