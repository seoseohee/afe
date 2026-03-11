"""
ecc_core/todo.py

Claude Code의 TodoWrite/TodoRead를 구현.

Claude Code에서 Todo가 중요한 이유:
  1. 긴 작업에서 모델이 목표를 잃지 않도록 '나침반' 역할
  2. 컨텍스트 압축 후에도 진행 상태 복원 가능
  3. 사용자가 진행 상황을 실시간으로 볼 수 있음

임베디드에서 특히 중요한 이유:
  연결 끊김 후 재연결 시 어디까지 했는지 알 수 있다
"""

from dataclasses import dataclass, field
from typing import Literal


Status = Literal["pending", "in_progress", "completed"]
Priority = Literal["high", "medium", "low"]


@dataclass
class TodoItem:
    id: str
    content: str
    status: Status = "pending"
    priority: Priority = "medium"


class TodoManager:
    STATUS_ICONS = {
        "pending":     "○",
        "in_progress": "→",
        "completed":   "✓",
    }
    PRIORITY_ICONS = {
        "high":   "🔴",
        "medium": "🟡",
        "low":    "🟢",
    }

    def __init__(self):
        self._todos: list[TodoItem] = []

    def update(self, raw_todos: list[dict]):
        """전체 목록을 교체 (부분 업데이트 없음 — Claude Code 방식)"""
        self._todos = [
            TodoItem(
                id=t.get("id", f"t{i}"),
                content=t.get("content", ""),
                status=t.get("status", "pending"),
                priority=t.get("priority", "medium"),
            )
            for i, t in enumerate(raw_todos)
        ]

    def has_todos(self) -> bool:
        return bool(self._todos)

    def all_completed(self) -> bool:
        return bool(self._todos) and all(
            t.status == "completed" for t in self._todos
        )

    def in_progress_items(self) -> list[TodoItem]:
        return [t for t in self._todos if t.status == "in_progress"]

    def format_display(self) -> str:
        """터미널 출력용"""
        if not self._todos:
            return ""
        lines = ["  📋 진행 상황:"]
        for t in self._todos:
            status_icon = self.STATUS_ICONS.get(t.status, "?")
            pri_icon = self.PRIORITY_ICONS.get(t.priority, "")
            lines.append(f"    {status_icon} {pri_icon} [{t.id}] {t.content}")
        return "\n".join(lines)

    def format_for_llm(self) -> str:
        """LLM에게 돌려줄 텍스트"""
        if not self._todos:
            return "(todo 없음)"
        lines = []
        for t in self._todos:
            lines.append(f"[{t.id}] {t.status} | {t.content}")
        return "\n".join(lines)

    def format_nag(self) -> str:
        """
        Claude Code의 'nag reminder' — 매 turn 시스템 메시지에 주입.
        진행 중이거나 대기 중인 항목이 있을 때만 리마인더 생성.
        """
        remaining = [t for t in self._todos if t.status != "completed"]
        if not remaining:
            return ""
        lines = ["[현재 진행 중인 작업]"]
        for t in remaining:
            icon = self.STATUS_ICONS.get(t.status, "?")
            lines.append(f"  {icon} [{t.id}] {t.content}")
        return "\n".join(lines)
