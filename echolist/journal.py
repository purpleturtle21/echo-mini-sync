"""Sync journal — crash-safe record of in-progress sync operations."""

from __future__ import annotations

import json
from pathlib import Path

from .safe_write import atomic_write_text as _atomic_write_text

JOURNAL_FILE = Path.home() / ".echolist" / "sync_journal.json"


class SyncJournal:
    """Writes a step-by-step plan before sync; marks each step done as it completes.

    On next startup, if a journal exists, it means we crashed mid-sync.
    The caller can inspect ``pending_actions`` and resume or clean up.
    """

    def __init__(self, actions: list[dict] | None = None):
        self.actions: list[dict] = actions or []

    @classmethod
    def begin(cls, removes: list[dict], adds: list[dict], reorders: dict) -> SyncJournal:
        actions = []
        for r in removes:
            actions.append({"op": "remove", "pid": r["pid"], "index": r["index"],
                            "copy_name": r.get("copy_name", ""), "status": "pending"})
        for a in adds:
            actions.append({"op": "add", "pid": a["pid"], "src": a["src"],
                            "title": a.get("title", ""), "status": "pending"})
        for pid in reorders:
            actions.append({"op": "reorder", "pid": pid, "status": "pending"})
        journal = cls(actions)
        journal._save()
        return journal

    @classmethod
    def load_incomplete(cls) -> SyncJournal | None:
        if not JOURNAL_FILE.exists():
            return None
        try:
            data = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
            actions = data.get("actions", [])
        except (json.JSONDecodeError, OSError):
            cls.discard()
            return None
        if not any(a.get("status") == "pending" for a in actions):
            cls.discard()
            return None
        return cls(actions)

    @property
    def pending_actions(self) -> list[dict]:
        return [a for a in self.actions if a.get("status") == "pending"]

    def mark_done(self, index: int) -> None:
        if 0 <= index < len(self.actions):
            self.actions[index]["status"] = "done"
            self._save()

    def mark_current(self, index: int) -> None:
        if 0 <= index < len(self.actions):
            self.actions[index]["status"] = "in_progress"
            self._save()

    def complete(self) -> None:
        self.discard()

    def _save(self) -> None:
        _atomic_write_text(JOURNAL_FILE, json.dumps({"actions": self.actions}, indent=2))

    @staticmethod
    def discard() -> None:
        try:
            JOURNAL_FILE.unlink(missing_ok=True)
        except OSError:
            pass
