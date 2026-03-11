"""
ecc_core/loop.py

ECC v5 — 연결부터 목표 달성까지 에이전트가 전부 담당.

핵심 변경 (v4 → v5):
  v4: SSH 연결이 전제조건. 연결 실패 → sys.exit
  v5: 연결 자체가 루프 안 첫 번째 목표.
      conn=None으로 시작. ssh_connect 도구로 연결 달성.
      막히면 포기하지 않고 LLM에게 피드백으로 돌려준다.

Claude Code nO loop 계승:
  while stop_reason == "tool_use":
      execute tools → feed results
  stop_reason == "end_turn" + no tools → 자연 종료

포기하는 코드는 없다:
  - 연결 실패 → tool_result: "[ssh_connect failed] ..." → LLM이 다른 방법 시도
  - reconnect 실패 → tool_result: "[reconnect failed] ..." → LLM이 판단
  - 예외 → tool_result: "[error] ..." → LLM이 진단
"""

import anthropic

from .connection import BoardConnection, BoardDiscovery
from .todo import TodoManager
from .executor import ToolExecutor
from .compactor import should_compact, compact
from .prompt import build_system_prompt
from .tools import TOOL_DEFINITIONS


# ─────────────────────────────────────────────────────────────
# Subagent (Claude Code Task/dispatch_agent 패턴)
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
    """
    독립 컨텍스트에서 탐색/분석 실행.
    메인 루프의 context를 탐색 명령들로 오염시키지 않는다.
    """
    system = (
        "You are a subagent for ECC. Perform the given task and call report().\n"
        "Be thorough. Batch independent commands. Do NOT spawn subagents.\n"
        f"SSH: {conn.user}@{conn.host}:{conn.port}\n"
        + (f"\nAlready known:\n{context}" if context else "")
    )

    todos = TodoManager()
    executor = ToolExecutor(conn, todos, verbose)
    messages: list[dict] = [{"role": "user", "content": goal}]

    for _ in range(40):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
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
            for b in resp.content:
                if b.type == "text":
                    return b.text
            break

    return "(subagent: no report)"


# ─────────────────────────────────────────────────────────────
# AgentLoop
# ─────────────────────────────────────────────────────────────

class AgentLoop:
    """
    v5: conn=None으로 시작. 연결 자체가 루프 안 작업.

    구조:
        conn = None
        messages = [user_message(goal)]

        while True:
            resp = llm(system, tools, messages)

            for each tool_use:
                if ssh_connect: 실제 연결 시도 → conn 설정 or 실패 피드백
                elif no conn:   "[no connection]" 피드백 → LLM이 ssh_connect 먼저 호출하도록
                else:           정상 실행

            if end_turn + no tools: break
            if done called: break
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.client = anthropic.Anthropic()
        self.conn: BoardConnection | None = None

    def run(self, goal: str, max_turns: int = 100):
        print(f"\n{'═'*60}")
        print(f"  🎯 {goal[:80]}")
        print(f"{'═'*60}")

        # 이전 run에서 연결이 남아있으면 재사용 (REPL 모드)
        if self.conn and not self.conn.is_alive():
            print("  ⚠️  이전 연결이 끊어짐. 에이전트가 재연결합니다.")
            self.conn = None

        todos = TodoManager()
        executor = ToolExecutor(self.conn, todos, self.verbose)
        system = build_system_prompt()

        messages: list[dict] = [
            {"role": "user", "content": _build_initial_message(goal)}
        ]

        for turn in range(max_turns):

            # ── 컨텍스트 압축 ────────────────────────────────
            if should_compact(messages):
                messages = compact(
                    messages, goal,
                    todos.format_for_llm(),
                    self.client
                )

            # ── Todo nag: 매 turn 시스템에 주입 ─────────────
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

            # ── LLM 호출 ─────────────────────────────────────
            try:
                resp = self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=8096,
                    system=system_with_state,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
            except anthropic.BadRequestError as e:
                if "context" in str(e).lower():
                    messages = compact(
                        messages, goal,
                        todos.format_for_llm(),
                        self.client
                    )
                    continue
                raise

            messages.append({"role": "assistant", "content": resp.content})

            # ── 텍스트 출력 ──────────────────────────────────
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    if len(block.text) < 300 or self.verbose:
                        print(f"\n  💬 {block.text.strip()}", flush=True)

            # ── 종료 조건 ────────────────────────────────────
            has_tools = any(b.type == "tool_use" for b in resp.content)
            if resp.stop_reason == "end_turn" and not has_tools:
                print("\n  ✓ 완료")
                break

            # ── Tool 실행 ────────────────────────────────────
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue

                if block.name == "ssh_connect":
                    # 연결 도구: AgentLoop이 직접 처리
                    out = self._handle_ssh_connect(block.input)
                    # executor에도 conn 갱신
                    executor.conn = self.conn

                elif self.conn is None:
                    # 연결 없이 다른 도구 호출 — 피드백만 줌
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

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if executor.is_finished:
                break

            # ── 연결 체크 (10 turn마다) ──────────────────────
            if self.conn and turn > 0 and turn % 10 == 0:
                self._check_connection(messages)

        else:
            print(f"\n  ⚠️  max turns({max_turns}) reached")

    # ─────────────────────────────────────────────────────────
    # ssh_connect 처리
    # 실패해도 예외 없이 tool_result 피드백으로 반환
    # ─────────────────────────────────────────────────────────

    def _handle_ssh_connect(self, inp: dict) -> str:
        host = inp.get("host", "").strip()
        user = inp.get("user", "").strip() or None
        port = int(inp.get("port", 22))

        print(f"\n  🔗 ssh_connect: host={host} user={user or 'auto'} port={port}", flush=True)

        # "scan" 키워드 → 자동 탐색
        if host.lower() == "scan" or not host:
            print("  🔍 네트워크 자동 탐색 중...", flush=True)
            conn = BoardDiscovery.scan(user=user, port=port)
            if conn:
                self.conn = conn
                print(f"  ✅ 발견 및 연결: {conn.address}")
                return f"[ssh_connect ok] Connected to {conn.address}"
            else:
                msg = (
                    "[ssh_connect failed] No board found on local network.\n"
                    "Tried: known_hosts, mDNS (.local), subnet scan.\n"
                    "Suggestions:\n"
                    "- Try ssh_connect with a specific host IP\n"
                    "- Check if the board is powered on and on the same network\n"
                    "- Try different subnets (10.0.0.x, 172.16.x.x)"
                )
                print(f"  ❌ 탐색 실패")
                return msg

        # 특정 host → 연결 시도
        conn = BoardDiscovery.from_hint(host, user, port)
        if conn:
            self.conn = conn
            print(f"  ✅ 연결: {conn.address}")
            return f"[ssh_connect ok] Connected to {conn.address}"
        else:
            msg = (
                f"[ssh_connect failed] Could not connect to {host}:{port}\n"
                f"Tried users: {BoardDiscovery.DEFAULT_USERS if not user else [user]}\n"
                "Suggestions:\n"
                f"- Verify the board is reachable: try ssh_connect with host='{host}' and different user\n"
                "- Try ssh_connect with host='scan' to search the network\n"
                "- Check if SSH is enabled on the board\n"
                "- Try a different port (2222, 2200)"
            )
            print(f"  ❌ {host} 연결 실패")
            return msg

    # ─────────────────────────────────────────────────────────
    # 연결 체크 — 실패해도 예외 없이 메시지 주입
    # ─────────────────────────────────────────────────────────

    def _check_connection(self, messages: list[dict]):
        if not (self.conn.likely_disconnected or not self.conn.is_alive()):
            return

        print("\n  🔄 연결 끊김 감지, 재연결 시도...", flush=True)
        if self.conn.reconnect(max_attempts=3):
            print("  ✅ 재연결 성공")
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "reconnect_event",
                    "content": (
                        "[SSH reconnected]\n"
                        "Connection was lost and restored. "
                        "Check board state (running processes, temp files) before continuing."
                    )
                }]
            })
        else:
            # 포기하지 않음 — LLM에게 알리고 계속
            print("  ❌ 자동 재연결 실패. 에이전트에게 알림.")
            self.conn = None
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "reconnect_event",
                    "content": (
                        "[SSH connection lost — reconnect failed]\n"
                        "Automatic reconnect (3 attempts) failed.\n"
                        "Use ssh_connect to re-establish connection before continuing."
                    )
                }]
            })


# ─────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────

def _build_initial_message(goal: str) -> str:
    """
    user message 구성. goal 그대로 전달.
    속도/시간 파라미터가 명시됐으면 파싱해서 명시 (LLM 부담 감소).
    """
    import re

    lines = [goal]

    speed = re.search(r'(\d+(?:\.\d+)?)\s*m/s', goal)
    duration = re.search(r'(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?', goal)
    if speed or duration:
        lines.append("")
        lines.append("Extracted parameters:")
        if speed:
            lines.append(f"  - target speed: {speed.group(1)} m/s")
        if duration:
            lines.append(f"  - duration: {duration.group(1)} s")

    return "\n".join(lines)


def _extract_known_context(messages: list[dict]) -> str:
    """대화에서 발견된 핵심 정보를 추출해서 subagent에게 전달."""
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
