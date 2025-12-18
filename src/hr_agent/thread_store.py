from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass(frozen=True)
class ThreadStore:
    path: Path

    @staticmethod
    def default() -> "ThreadStore":
        # Store under project root (current working dir)
        state_dir = Path.cwd() / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return ThreadStore(path=state_dir / "thread_id.txt")

    def load(self) -> Optional[str]:
        if not self.path.exists():
            return None
        tid = self.path.read_text(encoding="utf-8").strip()
        return tid or None

    def save(self, thread_id: str) -> None:
        if not thread_id:
            return
        self.path.write_text(thread_id, encoding="utf-8")
