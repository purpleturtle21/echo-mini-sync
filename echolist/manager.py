"""PlaylistManager — the four operations: create, add, remove, stats."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

_MAX_TAG_WORKERS = 8
from datetime import datetime
from pathlib import Path

from .safe_write import SafeWriter, UnsafeWriteError
from .config import (
    Config, DEFAULT_PLAYLIST_FOLDER,
    save_backup, list_backups, list_all_backup_pids, load_backup,
    save_playlist_snapshot, load_playlist_snapshot,
)
from .store import Store
from .naming import playlist_id, sanitize, track_filename
from .tags import apply_playlist_tags, read_playlist_tags, restore_tags

try:
    import mutagen
except ImportError:
    mutagen = None


class WorkspaceLockError(Exception):
    ...


class PlaylistManager:
    def __init__(self, writer: SafeWriter, config: Config, store: Store):
        self.writer = writer
        self.config = config
        self.store = store
        self._lock_fd = None

    @classmethod
    def init(
        cls,
        source_root: str | Path,
        dest_root: str | Path,
        node_name: str = "* PLAYLISTS *",
        album_prefix: str = "",
        playlist_folder: str = DEFAULT_PLAYLIST_FOLDER,
        backup_interval: int = 5,
    ) -> PlaylistManager:
        source_root = Path(source_root).resolve()
        workspace = Path(dest_root).resolve() / playlist_folder
        _check_overlap(source_root, workspace)
        writer = SafeWriter(workspace)
        config = Config(
            source_root=str(source_root),
            node_name=node_name,
            album_prefix=album_prefix,
            playlist_folder=playlist_folder,
            backup_interval=backup_interval,
        )
        store = Store.load(writer)
        mgr = cls(writer, config, store)
        mgr._acquire_lock()
        config.save(writer)
        return mgr

    @classmethod
    def open(cls, dest_root: str | Path, playlist_folder: str | None = None) -> PlaylistManager:
        folder = playlist_folder or DEFAULT_PLAYLIST_FOLDER
        workspace = Path(dest_root).resolve() / folder
        writer = SafeWriter(workspace)
        config = Config.load(writer)
        if not config.source_root:
            raise ValueError("workspace not initialized — run 'echolist init' first")
        source_root = Path(config.source_root).resolve()
        _check_overlap(source_root, workspace)
        store = Store.load(writer)
        mgr = cls(writer, config, store)
        mgr._acquire_lock()
        return mgr

    def _acquire_lock(self) -> None:
        lock_path = self.writer.root / ".echolist" / "workspace.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
        except (OSError, IOError):
            try:
                os.close(fd)
            except Exception:
                pass
            raise WorkspaceLockError(
                "Another EchoList instance is using this workspace. "
                "Close it first, or delete .echolist/workspace.lock if "
                "the previous session crashed."
            )

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def create_playlist(self, name: str) -> str:
        pid = playlist_id(name)
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")
        folder = sanitize(name)
        # TODO: star prefix feature disabled — re-enable when stable
        # if self.config.star_prefix:
        #     folder = f"★ {folder}"
        self.store.add_playlist(pid, name, folder)
        self._ensure_workspace()
        self.writer.makedirs(folder)
        self.store.save()
        return pid

    # TODO: star prefix feature disabled — re-enable when stable
    # def set_star_prefix(self, enabled: bool, done_event: threading.Event | None = None) -> None:
    #     if enabled == self.config.star_prefix:
    #         if done_event:
    #             done_event.set()
    #         return
    #     self.config.star_prefix = enabled
    #     renames = []
    #     for pid, pl in self.store.playlists.items():
    #         old_folder = pl["folder"]
    #         base = old_folder.removeprefix("★ ") if old_folder.startswith("★ ") else old_folder
    #         new_folder = f"★ {base}" if enabled else base
    #         if old_folder != new_folder:
    #             renames.append((pl, old_folder, new_folder))
    #             pl["folder"] = new_folder
    #     self.store.save()
    #     self.config.save(self.writer)
    #
    #     def _do_renames():
    #         for pl, old, new in renames:
    #             try:
    #                 self.writer.rename(old, new)
    #             except Exception:
    #                 pass
    #         if done_event:
    #             done_event.set()
    #
    #     thread = threading.Thread(target=_do_renames, daemon=True)
    #     thread.start()
    #     self._star_rename_thread = thread

    def rescan_playlist(self, pid: str) -> bool:
        """Update playlist entries from what's actually on the drive."""
        if pid not in self.store.playlists:
            return False
        playlist = self.store.playlists[pid]
        folder_path = self.writer.root / playlist["folder"]
        if not folder_path.exists():
            return False

        disk_files = sorted(
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS
            and not f.name.startswith("_echolist_tmp_")
        )

        by_name = {t["copy_name"]: t for t in playlist["tracks"]}
        by_title = {}
        for t in playlist["tracks"]:
            title_part = _strip_index_prefix(t["copy_name"])
            if title_part not in by_title:
                by_title[title_part] = t

        new_tracks = []
        matched_ids = set()
        for i, f in enumerate(disk_files, 1):
            if f.name in by_name:
                entry = {**by_name[f.name], "index": i, "copy_name": f.name}
                new_tracks.append(entry)
                matched_ids.add(id(by_name[f.name]))
            else:
                title_part = _strip_index_prefix(f.name)
                if title_part in by_title and id(by_title[title_part]) not in matched_ids:
                    old = by_title[title_part]
                    entry = {**old, "index": i, "copy_name": f.name}
                    new_tracks.append(entry)
                    matched_ids.add(id(old))
                else:
                    new_tracks.append({
                        "index": i,
                        "src_path": "",
                        "copy_name": f.name,
                        "src_hash": "",
                    })

        old_state = [(t["index"], t["copy_name"]) for t in playlist["tracks"]]
        new_state = [(t["index"], t["copy_name"]) for t in new_tracks]
        if old_state != new_state:
            playlist["tracks"] = new_tracks
            self.store.save()
            return True
        return False

    def _ensure_workspace(self) -> None:
        if not self.writer.root.exists():
            self.writer.root.mkdir(parents=True, exist_ok=True)
            self.config.save(self.writer)

    def add_track(self, pid: str, src: str | Path, progress_cb=None) -> str:
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")
        playlist = self.store.playlists[pid]
        src = Path(src)
        if not src.is_absolute():
            src = Path(self.config.source_root) / src
        src = src.resolve()
        if not src.exists():
            raise FileNotFoundError(f"source not found: {src}")

        source_root = Path(self.config.source_root).resolve()
        try:
            src_rel = src.relative_to(source_root).as_posix()
        except ValueError:
            src_rel = src.as_posix()

        title = _read_title(src)
        ext = src.suffix
        index = len(playlist["tracks"]) + 1
        pad = 3 if index > 99 else 2
        copy_name = track_filename(index, title, ext, pad)
        folder = playlist["folder"]
        rel = f"{folder}/{copy_name}"

        original_tags = read_playlist_tags(src)

        _, src_hash = self.writer.copy_in(src, rel, progress_cb=progress_cb, compute_hash=True)

        album = self.config.album_prefix + playlist["name"]
        apply_playlist_tags(
            self.writer.resolve(rel),
            self.config.node_name,
            album,
            index,
            src_rel,
            pid,
        )

        self.store.add_track(pid, {
            "index": index,
            "src_path": src_rel,
            "copy_name": copy_name,
            "src_hash": f"blake2b:{src_hash}",
            "original_tags": original_tags,
        })
        self.store.save()
        return rel

    def remove_track(self, pid: str, index: int) -> None:
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")
        removed = self.store.remove_track(pid, index)
        folder = self.store.playlists[pid]["folder"]
        self.writer.delete(f"{folder}/{removed['copy_name']}")
        self._renumber_tracks(pid)
        self.store.save()

    def _renumber_tracks(self, pid: str) -> None:
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        tracks = playlist["tracks"]
        pad = 3 if len(tracks) > 99 else 2
        for t in tracks:
            old_name = t["copy_name"]
            title = old_name.split(" - ", 1)[-1].rsplit(".", 1)[0]
            ext = Path(old_name).suffix
            new_name = track_filename(t["index"], title, ext, pad)
            if old_name != new_name:
                try:
                    self.writer.rename(f"{folder}/{old_name}", f"{folder}/{new_name}")
                    t["copy_name"] = new_name
                except Exception:
                    pass

    def stats(self, cached_device_tracks: int | None = None,
              cached_workspace_bytes: int | None = None) -> dict:
        total_playlists = len(self.store.playlists)
        total_tracks = sum(
            len(p["tracks"]) for p in self.store.playlists.values()
        )
        workspace_bytes = cached_workspace_bytes if cached_workspace_bytes is not None else 0
        device_tracks = cached_device_tracks if cached_device_tracks is not None else 0
        try:
            usage = shutil.disk_usage(self.writer.root.parent)
        except OSError:
            usage = type("U", (), {"total": 1, "used": 0})()
        return {
            "playlists": total_playlists,
            "tracks": total_tracks,
            "device_tracks": device_tracks,
            "workspace_bytes": workspace_bytes,
            "drive_total": usage.total,
            "drive_used": usage.used,
            "drive_used_pct": round(usage.used / usage.total * 100, 1),
            "workspace_pct_of_drive": round(workspace_bytes / usage.total * 100, 4),
            "files_vs_8192": f"{total_tracks}/8192",
        }

    def compute_expensive_stats(self) -> tuple[int, int]:
        """Returns (device_tracks, workspace_bytes). Call from a background thread."""
        if self.writer.root.exists():
            workspace_bytes = sum(
                f.stat().st_size
                for f in self.writer.root.rglob("*")
                if f.is_file()
            )
        else:
            workspace_bytes = 0
        device_root = self.writer.root.parent.parent
        device_tracks = _count_audio_files(device_root)
        return device_tracks, workspace_bytes


    def audit_playlist_metadata(self, pid: str) -> list[dict]:
        """Check tracks for metadata that doesn't match EchoList expectations.

        Returns a list of dicts: {"copy_name", "path", "field", "expected", "actual"}
        """
        if pid not in self.store.playlists:
            return []
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder
        if not folder_path.exists():
            return []

        expected_albumartist = self.config.node_name
        expected_album = self.config.album_prefix + playlist["name"]
        issues = []

        for t in playlist["tracks"]:
            copy_name = t["copy_name"]
            track_path = folder_path / copy_name
            if not track_path.exists():
                continue
            if track_path.suffix.lower() not in AUDIO_EXTS:
                continue

            tags = read_playlist_tags(track_path)

            if tags["albumartist"] != expected_albumartist:
                issues.append({
                    "copy_name": copy_name,
                    "path": str(track_path),
                    "field": "albumartist",
                    "expected": expected_albumartist,
                    "actual": tags["albumartist"],
                })
            if tags["album"] != expected_album:
                issues.append({
                    "copy_name": copy_name,
                    "path": str(track_path),
                    "field": "album",
                    "expected": expected_album,
                    "actual": tags["album"],
                })
            if tags["tracknumber"] != str(t["index"]):
                issues.append({
                    "copy_name": copy_name,
                    "path": str(track_path),
                    "field": "tracknumber",
                    "expected": str(t["index"]),
                    "actual": tags["tracknumber"],
                })

        return issues

    def backup_playlist_metadata(self, pid: str, timestamp: str | None = None) -> Path | None:
        """Save current metadata of all tracks in a playlist before overwriting."""
        if pid not in self.store.playlists:
            return None
        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder

        entries = []
        for t in playlist["tracks"]:
            track_path = folder_path / t["copy_name"]
            entry = {
                "copy_name": t["copy_name"],
                "index": t["index"],
                "src_path": t.get("src_path", ""),
                "src_hash": t.get("src_hash", ""),
                "original_tags": t.get("original_tags", {}),
            }
            if track_path.exists():
                entry["tags"] = read_playlist_tags(track_path)
            entries.append(entry)

        if not entries:
            return None

        new_data = {
            "playlist_name": playlist["name"],
            "folder": folder,
            "tracks": entries,
        }

        for existing in list_backups(self.writer.root, pid):
            old = load_backup(self.writer.root, pid, existing["timestamp"])
            if old and old == new_data:
                return existing["path"]

        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        return save_backup(self.writer.root, pid, timestamp, new_data)

    def fix_playlist_metadata(self, pid: str) -> int:
        """Fix metadata on tracks that don't match expectations. Backs up first.
        Returns the number of tracks fixed."""
        issues = self.audit_playlist_metadata(pid)
        if not issues:
            return 0

        self.backup_playlist_metadata(pid)

        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        track_by_name = {t["copy_name"]: t for t in playlist["tracks"]}

        fix_jobs = []
        fixed_files = set()
        for issue in issues:
            track_path = Path(issue["path"])
            if str(track_path) in fixed_files:
                continue
            fixed_files.add(str(track_path))
            t = track_by_name.get(issue["copy_name"])
            if t is None:
                continue
            fix_jobs.append((track_path, t["index"], t.get("src_path", "")))

        album = self.config.album_prefix + playlist["name"]
        def _apply(job):
            path, index, src_rel = job
            apply_playlist_tags(path, self.config.node_name, album, index, src_rel, pid)

        with ThreadPoolExecutor(max_workers=_MAX_TAG_WORKERS) as pool:
            list(pool.map(_apply, fix_jobs))

        self._renumber_tracks(pid)
        self.store.save()

        return len(fixed_files)

    def restore_playlist_metadata(self, pid: str, timestamp: str | None = None) -> int:
        """Restore original metadata from backup. Returns number of tracks restored.
        If timestamp is None, restores the most recent backup."""
        backups = list_backups(self.writer.root, pid)
        if not backups:
            return 0

        if timestamp is None:
            timestamp = backups[0]["timestamp"]

        data = load_backup(self.writer.root, pid, timestamp)
        if not data:
            return 0

        folder = data.get("folder", "")
        if not folder:
            return 0

        folder_path = self.writer.root / folder

        current_tracks = {}
        if pid in self.store.playlists:
            for t in self.store.playlists[pid]["tracks"]:
                otags = t.get("original_tags", {})
                if otags:
                    key = (otags.get("artist", ""), otags.get("title", ""))
                    current_tracks[key] = t["copy_name"]

        restored = 0
        for entry in data.get("tracks", []):
            tags = entry.get("tags")
            if not tags:
                continue
            track_path = folder_path / entry["copy_name"]
            if not track_path.exists():
                otags = entry.get("original_tags", {})
                if otags:
                    key = (otags.get("artist", ""), otags.get("title", ""))
                    cur_name = current_tracks.get(key)
                    if cur_name:
                        track_path = folder_path / cur_name
            if not track_path.exists():
                continue
            restore_tags(track_path, tags)
            restored += 1

        return restored

    def restore_playlist_to_point(self, pid: str, timestamp: str) -> dict:
        """Restore a playlist to the exact state of a restore point.

        - Tracks in backup but missing from disk → returned in "to_stage" for re-syncing
        - Tracks on disk but not in backup → deleted
        - Tracks in both → metadata restored
        - Renamed tracks (e.g. after reorder) → matched by original_tags, renamed back

        Returns {"restored": int, "removed": int, "to_stage": [{"src_path", "src_hash"}]}
        """
        if pid not in self.store.playlists:
            raise KeyError(f"playlist '{pid}' does not exist")

        data = load_backup(self.writer.root, pid, timestamp)
        if not data:
            raise KeyError(f"backup '{timestamp}' not found for playlist '{pid}'")

        self.backup_playlist_metadata(pid)

        playlist = self.store.playlists[pid]
        folder = playlist["folder"]
        folder_path = self.writer.root / folder

        backup_tracks = data.get("tracks", [])
        backup_names = {t["copy_name"] for t in backup_tracks}

        current_by_name = {t["copy_name"]: t for t in playlist["tracks"]}
        extra_current = {
            name: t for name, t in current_by_name.items()
            if name not in backup_names
        }

        missing_backup = [
            t for t in backup_tracks
            if not (folder_path / t["copy_name"]).exists()
        ]

        # Match renamed files by original_tags before deleting anything
        rename_map = {}
        for bt in missing_backup:
            bt_otags = bt.get("original_tags", {})
            if not bt_otags:
                continue
            for cur_name, ct in list(extra_current.items()):
                ct_otags = ct.get("original_tags", {})
                if ct_otags and ct_otags == bt_otags:
                    rename_map[cur_name] = bt["copy_name"]
                    del extra_current[cur_name]
                    break

        for old_name, new_name in rename_map.items():
            try:
                self.writer.rename(f"{folder}/{old_name}", f"{folder}/{new_name}")
            except Exception:
                pass

        removed = 0
        for name in extra_current:
            try:
                self.writer.delete(f"{folder}/{name}")
            except Exception:
                pass
            removed += 1

        restored = 0
        to_stage = []
        new_tracks = []
        for entry in backup_tracks:
            track_path = folder_path / entry["copy_name"]
            tags = entry.get("tags")
            if track_path.exists() and tags:
                restore_tags(track_path, tags)
                restored += 1
                new_tracks.append({
                    "index": entry["index"],
                    "src_path": entry.get("src_path", ""),
                    "copy_name": entry["copy_name"],
                    "src_hash": entry.get("src_hash", ""),
                    "original_tags": entry.get("original_tags", {}),
                })
            elif entry.get("src_path"):
                to_stage.append({
                    "src_path": entry["src_path"],
                    "src_hash": entry.get("src_hash", ""),
                })

        playlist["tracks"] = new_tracks
        self.store.save()

        return {"restored": restored, "removed": removed, "to_stage": to_stage}

    def list_metadata_backups(self, pid: str) -> list[dict]:
        return list_backups(self.writer.root, pid)

    def has_metadata_backup(self, pid: str) -> bool:
        return len(list_backups(self.writer.root, pid)) > 0

    def list_deleted_playlists(self) -> list[dict]:
        """Return backup info for playlists that have been deleted but have restore points."""
        all_pids = list_all_backup_pids(self.writer.root)
        deleted = []
        for pid in all_pids:
            if pid in self.store.playlists:
                continue
            backups = list_backups(self.writer.root, pid)
            if not backups:
                continue
            latest = load_backup(self.writer.root, pid, backups[0]["timestamp"])
            if not latest:
                continue
            deleted.append({
                "pid": pid,
                "name": latest.get("playlist_name", pid),
                "backups": backups,
            })
        return deleted

    def restore_deleted_playlist(self, pid: str, timestamp: str | None = None) -> list[dict]:
        """Recreate a deleted playlist from a backup. Returns track source info for staging.

        Each returned dict has: {"src_path": str, "src_hash": str}.
        The caller should stage these as pending adds (same as snapshot restore).
        """
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")

        backups = list_backups(self.writer.root, pid)
        if not backups:
            raise KeyError(f"no backups found for playlist '{pid}'")

        if timestamp is None:
            timestamp = backups[0]["timestamp"]

        data = load_backup(self.writer.root, pid, timestamp)
        if not data:
            raise KeyError(f"backup '{timestamp}' not found for playlist '{pid}'")

        name = data.get("playlist_name", pid)
        self.create_playlist(name)

        sources = []
        for entry in data.get("tracks", []):
            src_path = entry.get("src_path", "")
            if src_path:
                sources.append({
                    "src_path": src_path,
                    "src_hash": entry.get("src_hash", ""),
                })
        return sources

    def check_external_import(self) -> bool:
        """Returns True if the Playlists folder exists but has no .echolist marker."""
        if not self.writer.root.exists():
            return False
        echolist_dir = self.writer.root / ".echolist"
        return not echolist_dir.exists()

    def detect_untracked_playlists(self) -> list[dict]:
        """Find folders with audio files inside the workspace that aren't tracked."""
        if not self.writer.root.exists():
            return []
        tracked_folders = {pl["folder"] for pl in self.store.playlists.values()}
        untracked = []
        for d in sorted(self.writer.root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name in tracked_folders:
                continue
            audio_files = [
                f for f in d.iterdir()
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS
            ]
            if audio_files:
                untracked.append({
                    "folder": d.name,
                    "pid": playlist_id(d.name),
                    "track_count": len(audio_files),
                })
        return untracked

    def adopt_playlist(self, folder_name: str) -> str:
        """Adopt an untracked folder as an EchoList playlist.

        Reads original tags, creates a 'before_echolist' backup, then applies
        EchoList metadata. Returns the playlist ID.
        """
        folder_path = self.writer.root / folder_name
        if not folder_path.is_dir():
            raise FileNotFoundError(f"folder not found: {folder_path}")

        pid = playlist_id(folder_name)
        if pid in self.store.playlists:
            raise ValueError(f"playlist '{pid}' already exists")

        audio_files = sorted(
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS
        )
        if not audio_files:
            raise ValueError(f"no audio files in {folder_name}")

        self.store.add_playlist(pid, folder_name, folder_name)

        with ThreadPoolExecutor(max_workers=_MAX_TAG_WORKERS) as pool:
            tag_results = list(pool.map(read_playlist_tags, audio_files))

        for i, (f, original_tags) in enumerate(zip(audio_files, tag_results), 1):
            self.store.add_track(pid, {
                "index": i,
                "src_path": "",
                "copy_name": f.name,
                "src_hash": "",
                "original_tags": original_tags,
            })

        self.store.save()

        save_backup(self.writer.root, pid, "before_echolist", {
            "playlist_name": folder_name,
            "folder": folder_name,
            "tracks": [
                {
                    "copy_name": t["copy_name"],
                    "index": t["index"],
                    "src_path": "",
                    "src_hash": "",
                    "original_tags": t["original_tags"],
                    "tags": t["original_tags"],
                }
                for t in self.store.playlists[pid]["tracks"]
            ],
        })

        album = self.config.album_prefix + folder_name
        def _apply_tags(t):
            track_path = folder_path / t["copy_name"]
            if track_path.exists():
                apply_playlist_tags(
                    track_path, self.config.node_name, album,
                    t["index"], "", pid,
                )

        with ThreadPoolExecutor(max_workers=_MAX_TAG_WORKERS) as pool:
            list(pool.map(_apply_tags, self.store.playlists[pid]["tracks"]))

        self._renumber_tracks(pid)
        self.store.save()
        self.config.save(self.writer)
        return pid

    def save_snapshot(self) -> Path:
        """Save full playlist structure to ~/.echolist/ so it survives a device wipe."""
        from dataclasses import asdict
        return save_playlist_snapshot(
            self.writer.root,
            asdict(self.config),
            {"schema": 1, "playlists": self.store.playlists},
        )

    @staticmethod
    def find_snapshot(dest_root: str | Path, playlist_folder: str = DEFAULT_PLAYLIST_FOLDER) -> dict | None:
        workspace_root = Path(dest_root).resolve() / playlist_folder
        return load_playlist_snapshot(workspace_root)


AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".wma", ".alac", ".aiff", ".dsf", ".dff"})


def _count_audio_files(root: Path, exclude: Path | None = None) -> int:
    count = 0
    try:
        for f in root.rglob("*"):
            if exclude and f.is_relative_to(exclude):
                continue
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                count += 1
    except OSError:
        pass
    return count


def _check_overlap(source: Path, workspace: Path) -> None:
    if source == workspace or source.is_relative_to(workspace):
        raise UnsafeWriteError(
            f"source is inside workspace: source={source}, workspace={workspace}"
        )


def _blake2b(path: Path) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_index_prefix(name: str) -> str:
    parts = name.split(" - ", 1)
    return parts[1] if len(parts) > 1 else name


def _read_title(path: Path) -> str:
    if mutagen is None:
        return path.stem
    try:
        m = mutagen.File(path, easy=True)
        if m and "title" in m:
            return m["title"][0]
    except Exception:
        pass
    return path.stem


