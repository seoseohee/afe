"""
ecc_core/compactor.py

Claude Code의 Compressor wU2 구현.
컨텍스트 윈도우 약 85% 도달 시 대화를 요약하고 핵심만 보존.

Claude Code에서 compaction이 중요한 이유:
  긴 세션에서 모든 tool 결과를 그대로 유지하면
  컨텍스트가 금방 차오른다.
  오래된 탐색 결과 / 실패한 시도 등은 요약으로 대체 가능.

임베디드에서 특히 중요한 이유:
  하드웨어 탐색 결과 (dmesg, lsusb 등)는 길고 반복적이다.
  이미 파악한 정보를 계속 들고 다닐 필요가 없다.

보존해야 할 것:
  - goal
  - 발견한 하드웨어 정보 (디바이스 경로, 주소, 파라미터)
  - todo 상태
  - 실패한 접근법 (재시도 방지)
  - 마지막으로 성공한 상태
"""

import anthropic


COMPACT_TRIGGER_RATIO = 0.85   # 85% 도달 시 압축
MODEL_CONTEXT_LIMIT   = 180_000  # claude-opus-4-6 안전 한계


def estimate_tokens(messages: list[dict]) -> int:
    """대화 토큰 수 대략 추정 (문자 수 ÷ 4)"""
    total = 0
    for m in messages:
        c = m.get("content", "")
        total += len(str(c)) // 4
    return total


def should_compact(messages: list[dict]) -> bool:
    est = estimate_tokens(messages)
    return est > MODEL_CONTEXT_LIMIT * COMPACT_TRIGGER_RATIO


def compact(
    messages: list[dict],
    goal: str,
    todo_summary: str,
    client: anthropic.Anthropic,
) -> list[dict]:
    """
    긴 대화를 요약하고 압축된 컨텍스트로 재시작.
    
    반환값은 새로운 messages 리스트.
    첫 번째 항목은 항상 원래 goal을 담은 user 메시지.
    두 번째 항목은 요약 내용을 담은 assistant 메시지.
    """
    print("\n  📦 컨텍스트 압축 중...", flush=True)

    # 대화를 텍스트로 직렬화
    history_lines: list[str] = []
    for m in messages[1:]:   # 첫 user 메시지(goal) 제외
        role = m.get("role", "")
        content = m.get("content", "")

        if isinstance(content, str):
            history_lines.append(f"[{role}] {content[:400]}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "")[:300]
                    history_lines.append(f"[{role}/text] {text}")
                elif btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    detail = inp.get("command", inp.get("code", inp.get("path", str(inp))))[:120]
                    history_lines.append(f"[tool:{name}] {detail}")
                elif btype == "tool_result":
                    out = str(block.get("content", ""))[:200]
                    history_lines.append(f"[result] {out}")

    history_text = "\n".join(history_lines[-120:])  # 최근 120개 항목만

    prompt = f"""다음은 임베디드 보드 자동화 작업의 대화 기록이다.

목표(goal): {goal}

대화 기록:
{history_text}

위 기록에서 다음을 추출해서 간결하게 요약하라:
1. 완료된 작업 목록
2. 발견한 하드웨어 정보 (디바이스 경로, IP, 파라미터 등 — 구체적인 값 유지)
3. 실패한 접근법과 실패 이유 (재시도 방지를 위해 중요)
4. 현재 상태 (어디까지 진행됐는가)
5. 남은 작업

600자 이내로, 사실 위주로."""

    try:
        resp = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = resp.content[0].text if resp.content else "(요약 실패)"
    except Exception as e:
        summary = f"(컨텍스트 압축 중 오류: {e})"

    compacted: list[dict] = [
        {
            "role": "user",
            "content": f"Goal: {goal}"
        },
        {
            "role": "assistant",
            "content": (
                f"[이전 컨텍스트 요약]\n\n"
                f"{summary}\n\n"
                f"[Todo 상태]\n{todo_summary}"
            )
        }
    ]

    print(f"  ✅ {len(messages)} → {len(compacted)} 메시지로 압축", flush=True)
    return compacted
