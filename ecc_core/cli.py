"""
ecc_core/cli.py

ECC CLI — 사용자와 에이전트 사이의 인터페이스.

v5 설계 원칙:
  - 연결은 전제조건이 아니다. 에이전트가 루프 안에서 달성한다.
  - cli는 goal만 받아서 AgentLoop.run()을 호출한다.
  - 연결 실패로 종료하지 않는다.

Claude Code와 동일한 패턴:
  `claude "fix auth bug"` 를 치면 파일시스템이 이미 있다.
  `ecc "run the vehicle"` 을 치면 에이전트가 보드를 직접 찾는다.
"""

import argparse
import os
import sys
import textwrap

from .loop import AgentLoop


def main():
    parser = argparse.ArgumentParser(
        prog="ecc",
        description="ECC — Embedded Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        예시:
          # goal만 주면 에이전트가 보드를 찾아서 실행
          ecc "시스템 상태 확인하고 차량 주행시켜"
          ecc "CPU 온도를 1초마다 모니터링해줘"
          ecc "0.1 m/s로 5초 전진"

          # 힌트 제공 (탐색 속도 향상, 필수 아님)
          ecc --host 192.168.1.100 "차량 주행시켜"
          ecc --host 10.0.0.1 --user jetson "lidar 데이터 확인해줘"

          # REPL 모드
          ecc

        환경변수:
          ANTHROPIC_API_KEY   API 키 (필수)
          ECC_BOARD_HOST      보드 IP 힌트 (선택)
          ECC_BOARD_USER      SSH 사용자 힌트 (선택)
        """)
    )

    parser.add_argument(
        "goal", nargs="?", default=None,
        help="달성할 목표. 없으면 REPL 모드."
    )
    parser.add_argument(
        "--host", default=os.environ.get("ECC_BOARD_HOST"),
        help="보드 IP 힌트. 없으면 에이전트가 직접 탐색."
    )
    parser.add_argument(
        "--user", default=os.environ.get("ECC_BOARD_USER"),
        help="SSH 사용자 힌트."
    )
    parser.add_argument(
        "--port", type=int, default=22,
        help="SSH 포트 힌트 (기본: 22)"
    )
    parser.add_argument(
        "--max-turns", type=int, default=100,
        help="최대 에이전트 루프 횟수 (기본: 100)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="도구 입력/출력 상세 표시"
    )

    args = parser.parse_args()

    # API 키 확인 — 이것만이 실제 전제조건
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
        print("   export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    # 힌트가 있으면 goal에 포함 (에이전트가 참고)
    hint = _build_hint(args)

    # 연결 없이 바로 에이전트 시작
    agent = AgentLoop(verbose=args.verbose)

    if args.goal:
        goal = args.goal + hint
        try:
            agent.run(goal, max_turns=args.max_turns)
        except KeyboardInterrupt:
            print("\n\n  ⚡ 중단됨")
    else:
        _repl(agent, hint, args)


def _build_hint(args) -> str:
    """CLI 힌트를 goal에 붙일 텍스트로 변환."""
    parts = []
    if args.host:
        parts.append(f"host={args.host}")
    if args.user:
        parts.append(f"user={args.user}")
    if args.port != 22:
        parts.append(f"port={args.port}")
    if parts:
        return "\n\n[Connection hints: " + ", ".join(parts) + "]"
    return ""


def _repl(agent: AgentLoop, hint: str, args):
    """대화형 REPL — 연결 상태와 무관하게 시작."""
    print(f"""
{'═'*60}
  🤖 ECC — Embedded Claude Code
  goal을 입력하세요. 에이전트가 보드를 찾아서 실행합니다.
  명령어: /quit
{'═'*60}
""")

    while True:
        try:
            raw = input("ecc> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  종료합니다.")
            break

        if not raw:
            continue

        if raw.lower() in ("/quit", "/exit", "/q"):
            print("  종료합니다.")
            break

        try:
            agent.run(raw + hint, max_turns=args.max_turns)
        except KeyboardInterrupt:
            print("\n  ⚡ 현재 작업 중단. 다음 goal을 입력하세요.")
