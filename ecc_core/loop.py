"""
ecc_core/loop.py

ECC v5 — 연결부터 목표 달성까지 에이전트가 전부 담당.

환경변수:
  ECC_MODEL          메인 에이전트 모델 (기본: claude-sonnet-4-6)
  ECC_MAX_TOKENS     메인 에이전트 max_tokens (기본: 8096)
  ECC_COMPACT_MODEL  컨텍스트 압축용 모델 (기본: ECC_MODEL과 동일)
  ECC_SUBAGENT_TURNS subagent 최대 루프 수 (기본: 40)
"""

import os
import anthropic

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

    for _ in range(max_turns):
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
            for b in resp.content:
                if b.type == "text":
                    return b.text
            break

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

        for turn in range(max_turns):

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
                resp = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_with_state,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
            except anthropic.BadRequestError as e:
                if "context" in str(e).lower():
                    messages = compact(messages, goal, todos.format_for_llm(), self.client)
                    continue
                raise

            messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    if len(block.text) < 300 or self.verbose:
                        print(f"\n  💬 {block.text.strip()}", flush=True)

            has_tools = any(b.type == "tool_use" for b in resp.content)
            if resp.stop_reason == "end_turn" and not has_tools:
                print("\n  ✓ 완료")
                break

            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue

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

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if executor.is_finished:
                break

            if self.conn and turn > 0 and turn % 10 == 0:
                self._check_connection(messages)

        else:
            print(f"\n  ⚠️  max turns({max_turns}) reached")

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
                return (
                    "[ssh_connect failed] No board found on local network.\n"
                    "Tried: known_hosts, mDNS (.local), subnet scan.\n"
                    "Suggestions:\n"
                    "- Try ssh_connect with a specific host IP\n"
                    "- Check if the board is powered on and on the same network\n"
                    "- Try different subnets (10.0.0.x, 172.16.x.x)"
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


def _build_initial_message(goal: str) -> str:
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