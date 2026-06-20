"""Config dataclass + load/save via SafeWriter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .safe_write import SafeWriter

from .safe_write import atomic_write_text as _atomic_write_text

CONFIG_REL = ".echolist/config.json"
DEFAULT_FILE = Path.home() / ".echolist" / "default.json"
BACKUPS_ROOT = Path.home() / ".echolist" / "backups"


def _workspace_id(workspace_root: str | Path) -> str:
    """Short hash of the workspace path so different devices don't collide."""
    # TODO: this probably isn't portable to Windows — drive letters change when
    # the same device is plugged into a different port. The snapshot restore UI
    # should let the user pick the target drive instead of relying on this hash.
    return hashlib.sha256(str(Path(workspace_root).resolve()).encode()).hexdigest()[:12]


def load_defaults() -> dict:
    if DEFAULT_FILE.exists():
        return json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    return {}


def save_defaults(source: str, dest: str) -> None:
    _atomic_write_text(DEFAULT_FILE, json.dumps({
        "source": str(Path(source).resolve()),
        "dest": str(Path(dest).resolve()),
    }))


# ── Metadata backups (stored in ~/.echolist/backups/) ──

def save_backup(workspace_root: str | Path, pid: str, timestamp: str, data: dict) -> Path:
    wid = _workspace_id(workspace_root)
    backup_dir = BACKUPS_ROOT / wid / pid
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / f"{timestamp}.json"
    _atomic_write_text(p, json.dumps(data, indent=2))
    return p


def list_backups(workspace_root: str | Path, pid: str) -> list[dict]:
    wid = _workspace_id(workspace_root)
    backup_dir = BACKUPS_ROOT / wid / pid
    if not backup_dir.exists():
        return []
    results = []
    for f in sorted(backup_dir.iterdir(), reverse=True):
        if f.suffix == ".json":
            results.append({
                "timestamp": f.stem,
                "path": f,
            })
    return results


def list_all_backup_pids(workspace_root: str | Path) -> list[str]:
    """Return all playlist IDs that have at least one backup for this workspace."""
    wid = _workspace_id(workspace_root)
    wid_dir = BACKUPS_ROOT / wid
    if not wid_dir.exists():
        return []
    return sorted(
        d.name for d in wid_dir.iterdir()
        if d.is_dir() and any(f.suffix == ".json" for f in d.iterdir())
    )


def load_backup(workspace_root: str | Path, pid: str, timestamp: str) -> dict | None:
    wid = _workspace_id(workspace_root)
    p = BACKUPS_ROOT / wid / pid / f"{timestamp}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "tracks" not in data:
        return None
    if not isinstance(data["tracks"], list):
        return None
    return data


# ── Playlist snapshot (full playlist structure backup) ──

def save_playlist_snapshot(workspace_root: str | Path, config_data: dict, store_data: dict) -> Path:
    wid = _workspace_id(workspace_root)
    snapshot_dir = BACKUPS_ROOT / wid
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    p = snapshot_dir / "snapshot.json"
    _atomic_write_text(p, json.dumps({
        "config": config_data,
        "store": store_data,
    }, indent=2))
    return p


def load_playlist_snapshot(workspace_root: str | Path) -> dict | None:
    wid = _workspace_id(workspace_root)
    p = BACKUPS_ROOT / wid / "snapshot.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if "config" not in data or "store" not in data:
        return None
    return data


DEFAULT_PLAYLIST_FOLDER = "Playlists"


@dataclass
class Config:
    schema: int = 1
    source_root: str = ""
    node_name: str = "* PLAYLISTS *"
    album_prefix: str = ""
    star_prefix: bool = False
    playlist_folder: str = DEFAULT_PLAYLIST_FOLDER
    backup_interval: int = 5
    _sync_count: int = 0

    @classmethod
    def load(cls, writer: SafeWriter) -> Config:
        p = writer.root / CONFIG_REL
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(
                schema=data.get("schema", 1),
                source_root=data.get("source_root", ""),
                node_name=data.get("node_name", "* PLAYLISTS *"),
                album_prefix=data.get("album_prefix", ""),
                star_prefix=data.get("star_prefix", False),
                playlist_folder=data.get("playlist_folder", DEFAULT_PLAYLIST_FOLDER),
                backup_interval=data.get("backup_interval", 5),
                _sync_count=data.get("_sync_count", 0),
            )
        return cls()

    def save(self, writer: SafeWriter) -> None:
        writer.write_text(CONFIG_REL, json.dumps(asdict(self), indent=2))

    def should_backup(self) -> bool:
        return self._sync_count % self.backup_interval == 0

    def increment_sync(self, writer: SafeWriter) -> None:
        self._sync_count += 1
        self.save(writer)
