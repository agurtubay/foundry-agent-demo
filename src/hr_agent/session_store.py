from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import uuid
from typing import Optional

@dataclass(frozen=True)
class SessionStore:
    path: Path

    @staticmethod
    def default() -> "SessionStore":
        state_dir = Path.cwd() / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return SessionStore(path=state_dir / "session_id.txt")

    def load_or_create(self) -> str:
        if self.path.exists():
            sid = self.path.read_text(encoding="utf-8").strip()
            if sid:
                return sid
        sid = f"session_{uuid.uuid4().hex}"
        self.path.write_text(sid, encoding="utf-8")
        return sid
