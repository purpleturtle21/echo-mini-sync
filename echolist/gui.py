"""EchoList GUI — dark/red cassette-player theme with staged sync."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from queue import Queue, Empty
from threading import Thread

import os
import signal
import platform
import string
from datetime import datetime

from .manager import PlaylistManager, WorkspaceLockError, AUDIO_EXTS
from .naming import playlist_id, sanitize
from .config import load_defaults, save_defaults, DEFAULT_PLAYLIST_FOLDER
from .journal import SyncJournal
from .m3u import parse_m3u, curate_playlist_name

M3U_EXTS = frozenset({".m3u", ".m3u8"})


def _detect_echo_mini() -> str | None:
    system = platform.system()
    if system == "Windows":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(drive) == 2:  # DRIVE_REMOVABLE
                if ctypes.windll.kernel32.GetVolumeInformationW(
                    drive, buf, 1024, None, None, None, None, 0
                ):
                    if buf.value.upper() == "ECHO MINI":
                        return drive
    elif system == "Darwin":
        volumes = Path("/Volumes")
        if volumes.exists():
            for vol in volumes.iterdir():
                if vol.name.upper() == "ECHO MINI" and vol.is_dir():
                    return str(vol)
    else:
        for base in (Path("/media"), Path("/run/media")):
            if not base.exists():
                continue
            for user_dir in base.iterdir():
                if not user_dir.is_dir():
                    continue
                for vol in user_dir.iterdir():
                    if vol.name.upper() == "ECHO MINI" and vol.is_dir():
                        return str(vol)
    return None


def _default_source() -> str:
    music = Path.home() / "Music"
    if music.exists():
        return str(music)
    return str(Path.home())


def _is_external_path(path: str) -> bool:
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes
            drive = Path(path).anchor
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == 2
        except Exception:
            return False
    elif system == "Darwin":
        return path.startswith("/Volumes/")
    else:
        return path.startswith("/media/") or path.startswith("/run/media/")


# ── Theme colors ──
BG = "#1a1a1a"
BG_PANEL = "#222222"
BG_INPUT = "#2a2a2a"
FG = "#cccccc"
FG_DIM = "#888888"
FG_BRIGHT = "#eeeeee"
RED = "#cc3333"
RED_DARK = "#991111"
RED_BRIGHT = "#ff4444"
GREEN = "#44aa44"
YELLOW = "#ccaa33"
BORDER = "#333333"
PENDING_FG = "#cc9933"

MAX_TRACKS = 8192
PENDING_FILE = Path.home() / ".echolist" / "pending.json"
_HIDDEN_DIRS = frozenset({
    "playlists", "system volume information", "$recycle.bin",
    "recycler", "found.000", "msos",
})


def _read_tags_from_file(path: Path) -> tuple[str, str]:
    try:
        import mutagen
        m = mutagen.File(path, easy=True)
        if m:
            return m.get("title", [""])[0], m.get("artist", [""])[0]
    except Exception:
        pass
    return path.stem, ""


def _resolve_source_file(src_path: str, source_root: Path) -> Path | None:
    """Try to locate a source file using multiple strategies (read-only).

    Resolution order:
    1. Relative to source_root (the normal case)
    2. As an absolute path (if src_path is absolute)
    3. Just the filename, searched directly under source_root subfolders
    Returns None if not found anywhere.
    """
    if not src_path:
        return None
    p = Path(src_path.replace("\\", "/"))

    # 1. Relative to source_root
    if not p.is_absolute():
        candidate = source_root / p
        if candidate.exists():
            return candidate.resolve()

    # 2. Absolute path as-is
    if p.is_absolute() and p.exists():
        return p.resolve()

    # 3. Filename search under source_root (handles moved files)
    name = p.name
    try:
        for hit in source_root.rglob(name):
            if hit.is_file():
                return hit.resolve()
    except OSError:
        pass

    return None


def _format_backup_timestamp(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _cleanup_temp_files(workspace_root: Path) -> None:
    """Remove _echolist_tmp_* files left by interrupted reorders."""
    if not workspace_root.exists():
        return
    for f in workspace_root.rglob("_echolist_tmp_*"):
        try:
            f.unlink()
        except OSError:
            pass


class StagingState:
    """Tracks pending add/remove operations before SYNC."""

    def __init__(self):
        self.pending_adds: list[dict] = []
        self.pending_removes: list[dict] = []
        self.pending_reorders: dict[str, list] = {}
        self._load()

    def _load(self):
        if PENDING_FILE.exists():
            try:
                data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
                self.pending_adds = data.get("adds", [])
                self.pending_removes = data.get("removes", [])
                self.pending_reorders = data.get("reorders", {})
            except Exception:
                pass

    def save(self):
        from .safe_write import atomic_write_text
        atomic_write_text(PENDING_FILE, json.dumps({
            "adds": self.pending_adds,
            "removes": self.pending_removes,
            "reorders": self.pending_reorders,
        }, indent=2))

    def clear(self):
        self.pending_adds.clear()
        self.pending_removes.clear()
        self.pending_reorders.clear()
        if PENDING_FILE.exists():
            PENDING_FILE.unlink()

    def stage_add(self, pid: str, src: str, title: str, artist: str):
        self.pending_adds.append({
            "pid": pid, "src": src, "title": title, "artist": artist,
        })
        self.save()

    def stage_remove(self, pid: str, index: int, copy_name: str):
        self.pending_removes.append({
            "pid": pid, "index": index, "copy_name": copy_name,
        })
        self.save()

    def set_reorder(self, pid: str, order: list[dict]):
        self.pending_reorders[pid] = order
        self.save()

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_adds or self.pending_removes or self.pending_reorders)

    @property
    def total_ops(self) -> int:
        return len(self.pending_adds) + len(self.pending_removes) + len(self.pending_reorders)

    def virtual_tracks(self, pid: str, committed_tracks: list[dict]) -> tuple[list[dict], list[dict]]:
        """Returns (active_tracks, removed_tracks)."""
        removed_indices = {
            r["index"] for r in self.pending_removes if r["pid"] == pid
        }
        result = []
        removed = []
        for t in committed_tracks:
            if t["index"] in removed_indices:
                removed.append({**t, "_pending": False, "_removed": True, "_key": f"c:{t['index']}"})
            else:
                result.append({**t, "_pending": False, "_key": f"c:{t['index']}"})

        pending_count = 0
        for a in self.pending_adds:
            if a["pid"] == pid:
                result.append({
                    "index": 0,
                    "title": a["title"],
                    "artist": a["artist"],
                    "src_path": a["src"],
                    "_pending": True,
                    "_key": f"p:{pending_count}",
                })
                pending_count += 1

        if pid in self.pending_reorders:
            order = self.pending_reorders[pid]
            by_key = {}
            for t in result:
                by_key[t["_key"]] = t
            reordered = []
            for entry in order:
                if entry["key"] in by_key:
                    reordered.append(by_key.pop(entry["key"]))
            for leftover in by_key.values():
                reordered.append(leftover)
            result = reordered

        for i, t in enumerate(result, 1):
            t["index"] = i

        return result, removed

    def virtual_track_count(self, store) -> int:
        committed = sum(len(p["tracks"]) for p in store.playlists.values())
        return committed + len(self.pending_adds) - len(self.pending_removes)


ICON_PATH = Path(__file__).with_name("echolist.ico")
ICON_PNG_PATH = Path(__file__).with_name("echolist.png")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("EchoList")
        self.root.configure(bg=BG)
        self._set_icon()
        self._center_window(420, 700)
        self.root.minsize(380, 550)
        self.mgr = None
        self.source = None
        self.dest = None
        self.current_pid = None
        self.staging = StagingState()
        self._undo_stack: list[dict] = []
        self._sort_col = None
        self._sort_reverse = False
        self._drag_data = None
        self._cached_device_tracks = 0
        self._cached_workspace_bytes = 0
        self._stats_pending = False
        self._alive = True
        self._syncing = False
        self._tag_cache: dict[str, tuple[str, str]] = {}
        self._audit_cache: dict[str, list[dict]] = {}
        self._tracks_loading = False
        self._tracks_gen = 0
        self._callback_queue: Queue = Queue()
        self._poll_callbacks()
        self._apply_theme()
        self._show_setup()

    def _invalidate_caches(self, pid: str | None = None):
        """Clear tag and audit caches. If pid given, only that playlist."""
        if pid is None:
            self._tag_cache.clear()
            self._audit_cache.clear()
        else:
            self._audit_cache.pop(pid, None)
            pl = self.mgr.store.playlists.get(pid) if self.mgr else None
            if pl:
                prefix = f"{pl['folder']}/"
                self._tag_cache = {k: v for k, v in self._tag_cache.items()
                                   if not k.startswith(prefix)}

    def _poll_callbacks(self):
        """Drain the callback queue on the main thread (tkinter is not thread-safe on Windows)."""
        try:
            while True:
                fn = self._callback_queue.get_nowait()
                fn()
        except Empty:
            pass
        if self._alive:
            self.root.after(16, self._poll_callbacks)

    def _schedule_callback(self, fn):
        """Thread-safe way to schedule a function on the main thread."""
        self._callback_queue.put(fn)

    def _set_icon(self):
        if ICON_PNG_PATH.exists():
            try:
                icon = tk.PhotoImage(file=str(ICON_PNG_PATH))
                self.root.iconphoto(True, icon)
                self._icon_ref = icon
            except tk.TclError:
                pass
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("echolist.app")
        except Exception:
            pass

    def run(self):
        self.root.mainloop()

    def _center_window(self, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _apply_theme(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_INPUT,
                         bordercolor=BORDER, darkcolor=BG, lightcolor=BG,
                         troughcolor=BG_PANEL, selectbackground=RED_DARK,
                         selectforeground=FG_BRIGHT, font=("Consolas", 9))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background=BG_PANEL, foreground=FG, bordercolor=BORDER, padding=4)
        style.map("TButton",
                   background=[("active", RED_DARK), ("pressed", RED)],
                   foreground=[("active", FG_BRIGHT)])
        style.configure("Accent.TButton", background=RED_DARK, foreground=FG_BRIGHT)
        style.map("Accent.TButton",
                   background=[("active", RED), ("pressed", RED_BRIGHT)])
        style.configure("Sync.TButton", background=RED, foreground=FG_BRIGHT,
                         font=("Consolas", 11, "bold"), padding=6)
        style.map("Sync.TButton",
                   background=[("active", RED_BRIGHT), ("pressed", RED_DARK)])
        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG_BRIGHT,
                         insertcolor=FG_BRIGHT, bordercolor=BORDER)
        style.configure("Title.TLabel", font=("Consolas", 18, "bold"), foreground=RED, background=BG)
        style.configure("Section.TLabel", font=("Consolas", 10, "bold"), foreground=RED, background=BG)
        style.configure("Treeview", background=BG_INPUT, foreground=FG, fieldbackground=BG_INPUT,
                         bordercolor=BORDER, font=("Consolas", 9), rowheight=22)
        style.configure("Treeview.Heading", background=BG_PANEL, foreground=RED,
                         bordercolor=BORDER, font=("Consolas", 9, "bold"))
        style.map("Treeview",
                   background=[("selected", RED_DARK)],
                   foreground=[("selected", FG_BRIGHT)])
        style.configure("TScrollbar", background=BG_PANEL, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG_DIM)
        style.configure("TPanedwindow", background=BORDER)
        style.configure("Red.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=RED, bordercolor=BORDER)
        style.configure("Green.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=GREEN, bordercolor=BORDER)
        style.configure("Yellow.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=YELLOW, bordercolor=BORDER)
        style.configure("Sync.Horizontal.TProgressbar", troughcolor=BG_PANEL,
                         background=RED, bordercolor=BORDER)

    # ── Setup ──

    def _show_setup(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.config(menu=tk.Menu(self.root))
        self._center_window(420, 280)

        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="ECHOLIST", style="Title.TLabel").pack(pady=(0, 20))

        self._setup_status = tk.Label(frame, text="",
                                       font=("Consolas", 10), bg=BG, fg=FG_DIM,
                                       wraplength=350)
        self._setup_status.pack(pady=(0, 10))

        self._setup_btn_frame = ttk.Frame(frame)
        self._setup_btn_frame.pack(pady=(5, 10))

        adv_lbl = tk.Label(frame, text="Settings...",
                            font=("Consolas", 9, "underline"),
                            bg=BG, fg=FG_DIM, cursor="hand2")
        adv_lbl.pack(pady=(10, 0))
        adv_lbl.bind("<Button-1>", lambda e: self._show_settings())

        self.root.after(200, self._detect_and_show)

    def _detect_and_show(self):
        defaults = load_defaults()
        saved_source = defaults.get("source", "")
        saved_dest = defaults.get("dest", "")
        echo_mini = _detect_echo_mini()

        for w in self._setup_btn_frame.winfo_children():
            w.destroy()

        if saved_source and saved_dest:
            dest_exists = Path(saved_dest).exists()
            if echo_mini:
                self._setup_status.config(
                    text=f"Echo Mini connected at {echo_mini}", fg=GREEN)
            elif dest_exists:
                self._setup_status.config(
                    text=f"Workspace: {saved_dest}", fg=FG)
            else:
                self._setup_status.config(
                    text=f"Saved destination not found:\n{saved_dest}\n\n"
                         f"Connect your device or change workspace in Settings.",
                    fg=FG_DIM)
                btn_row = ttk.Frame(self._setup_btn_frame)
                btn_row.pack()
                ttk.Button(btn_row, text="Retry",
                            command=self._detect_and_show).pack(side="left", padx=5)
                ttk.Button(btn_row, text="Browse...",
                            command=self._browse_dest).pack(side="left", padx=5)
                return

            dest = echo_mini or saved_dest
            ttk.Button(self._setup_btn_frame, text="[ OPEN ]",
                        style="Accent.TButton",
                        command=lambda: self._open_with_dest(dest)).pack(pady=5)
        elif echo_mini:
            self._setup_status.config(
                text=f"Echo Mini found at {echo_mini}", fg=GREEN)
            ttk.Button(self._setup_btn_frame, text="[ OPEN ]",
                        style="Accent.TButton",
                        command=lambda: self._open_with_dest(echo_mini)).pack(pady=5)
        else:
            self._setup_status.config(
                text="Echo Mini not detected.\nConnect your player or choose a folder.",
                fg=FG_DIM)
            btn_row = ttk.Frame(self._setup_btn_frame)
            btn_row.pack()
            ttk.Button(btn_row, text="Retry",
                        command=self._detect_and_show).pack(side="left", padx=5)
            ttk.Button(btn_row, text="Browse...",
                        command=self._browse_dest).pack(side="left", padx=5)

    def _browse_dest(self):
        d = filedialog.askdirectory(title="Select destination device or folder")
        if not d:
            return
        if not _is_external_path(d):
            result = messagebox.askokcancel(
                "Local folder selected",
                "You selected a local folder. EchoList works best with "
                "an external music player like Echo Mini.\n\n"
                "Playlists created in a local folder won't be "
                "playable on a device.\n\n"
                "Continue anyway?")
            if not result:
                return
        self._open_with_dest(d)

    def _open_with_dest(self, dest):
        defaults = load_defaults()
        source = defaults.get("source") or _default_source()
        self._open_workspace(source, dest)

    def _show_settings(self):
        """Unified settings screen — used both for initial setup and in-app config."""
        for w in self.root.winfo_children():
            w.destroy()
        self.root.config(menu=tk.Menu(self.root))
        self._center_window(460, 440)

        is_open = self.mgr is not None

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="SETTINGS", style="Title.TLabel").pack(pady=(0, 15))

        frame = ttk.Frame(outer)
        frame.pack(fill="x")
        frame.columnconfigure(1, weight=1)

        defaults = load_defaults()
        cwd = str(Path.cwd())

        cur_source = self.source or defaults.get("source", cwd)
        cur_dest = self.dest or defaults.get("dest", cwd)
        existing_config = self._load_existing_config(cur_dest) if not is_open else {}

        row = 0

        # ── Source ──
        ttk.Label(frame, text="SOURCE").grid(row=row, column=0, sticky="w", pady=(4, 0))
        self._source_var = tk.StringVar(value=cur_source)
        src_row = ttk.Frame(frame)
        src_row.grid(row=row, column=1, columnspan=2, sticky="ew", padx=5, pady=(4, 0))
        ttk.Entry(src_row, textvariable=self._source_var, width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(src_row, text="...", width=3,
                    command=lambda: self._browse_var(self._source_var, "Select source music library")).pack(side="left", padx=(4, 0))
        row += 1
        tk.Label(frame, text="Your music library — files are copied from here, never modified",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Dest ──
        ttk.Label(frame, text="DEST").grid(row=row, column=0, sticky="w", pady=(4, 0))
        self._dest_var = tk.StringVar(value=cur_dest)
        dest_row = ttk.Frame(frame)
        dest_row.grid(row=row, column=1, columnspan=2, sticky="ew", padx=5, pady=(4, 0))
        ttk.Entry(dest_row, textvariable=self._dest_var, width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="...", width=3,
                    command=lambda: self._browse_var(self._dest_var, "Select destination device or folder")).pack(side="left", padx=(4, 0))
        row += 1
        tk.Label(frame, text="Device or folder where playlists are stored",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Separator ──
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        # ── Playlist folder ──
        ttk.Label(frame, text="FOLDER").grid(row=row, column=0, sticky="w", pady=(4, 0))
        default_folder = (self.mgr.config.playlist_folder if is_open
                          else existing_config.get("playlist_folder", DEFAULT_PLAYLIST_FOLDER))
        self._folder_var = tk.StringVar(value=default_folder)
        ttk.Entry(frame, textvariable=self._folder_var, width=20).grid(
            row=row, column=1, sticky="w", padx=5, pady=(4, 0))
        row += 1
        tk.Label(frame, text="Root folder name on the device (default: Playlists)",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Backup interval ──
        ttk.Label(frame, text="BACKUP EVERY").grid(row=row, column=0, sticky="w", pady=(4, 0))
        default_interval = (str(self.mgr.config.backup_interval) if is_open
                            else str(existing_config.get("backup_interval", 5)))
        self._backup_var = tk.StringVar(value=default_interval)
        backup_frame = ttk.Frame(frame)
        backup_frame.grid(row=row, column=1, sticky="w", padx=5, pady=(4, 0))
        vcmd = (frame.register(lambda v: v.isdigit() or v == ""), "%P")
        ttk.Entry(backup_frame, textvariable=self._backup_var, width=5,
                  validate="key", validatecommand=vcmd).pack(side="left")
        ttk.Label(backup_frame, text=" syncs").pack(side="left")
        row += 1
        tk.Label(frame, text="How often to create automatic restore points (1 = every sync)",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 6))
        row += 1

        # ── Buttons (centered) ──
        btn_row = ttk.Frame(outer)
        btn_row.pack(pady=(15, 0))
        if is_open:
            ttk.Button(btn_row, text="Cancel",
                        command=self._show_main).pack(side="left", padx=5)
        else:
            ttk.Button(btn_row, text="Back",
                        command=self._show_setup).pack(side="left", padx=5)
        ttk.Button(btn_row, text="[ OPEN ]", style="Accent.TButton",
                    command=self._on_settings_open).pack(side="left", padx=5)
        self.root.bind("<Return>", lambda e: self._on_settings_open())

    def _load_existing_config(self, dest: str) -> dict:
        """Try to read config from an existing workspace to pre-populate settings."""
        for folder in (DEFAULT_PLAYLIST_FOLDER, "Music", "Playlists"):
            config_path = Path(dest) / folder / ".echolist" / "config.json"
            if config_path.exists():
                try:
                    import json as _json
                    return _json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}

    def _browse_var(self, var, title="Select folder"):
        d = filedialog.askdirectory(initialdir=var.get(), title=title)
        if d:
            var.set(d)

    def _on_settings_open(self):
        source = self._source_var.get().strip()
        dest = self._dest_var.get().strip()
        if not source or not dest:
            messagebox.showwarning("Missing paths",
                                    "Both source and destination are required.")
            return
        folder = self._folder_var.get().strip() or DEFAULT_PLAYLIST_FOLDER
        try:
            interval = max(1, int(self._backup_var.get().strip() or "5"))
        except ValueError:
            interval = 5

        if self.mgr:
            self.mgr.config.backup_interval = interval
            if folder != self.mgr.config.playlist_folder:
                self.mgr.config.playlist_folder = folder
            self.mgr.config.save(self.mgr.writer)

            new_source = source
            new_dest = dest
            if new_source != self.source or new_dest != self.dest:
                self.mgr.release_lock()
                self._open_workspace(new_source, new_dest,
                                     playlist_folder=folder,
                                     backup_interval=interval)
            else:
                save_defaults(source, dest)
                self._show_main()
        else:
            self._open_workspace(source, dest,
                                 playlist_folder=folder,
                                 backup_interval=interval)

    def _open_workspace(self, source: str, dest: str,
                        playlist_folder: str = DEFAULT_PLAYLIST_FOLDER,
                        backup_interval: int = 5):
        try:
            playlists_dir = Path(dest) / playlist_folder
            config_file = playlists_dir / ".echolist" / "config.json"
            is_external = playlists_dir.exists() and not config_file.exists()
            is_empty = not config_file.exists()

            if config_file.exists():
                mgr = PlaylistManager.open(dest, playlist_folder=playlist_folder)
                resolved_source = str(Path(source).resolve())
                if mgr.config.source_root != resolved_source:
                    mgr.config.source_root = resolved_source
                    mgr.config.save(mgr.writer)
            else:
                snapshot = PlaylistManager.find_snapshot(dest, playlist_folder=playlist_folder)
                if snapshot and self._offer_snapshot_restore(snapshot, source, dest):
                    return
                mgr = PlaylistManager.init(
                    source, dest,
                    playlist_folder=playlist_folder,
                    backup_interval=backup_interval,
                )
            save_defaults(source, dest)
        except WorkspaceLockError as e:
            messagebox.showerror("Workspace locked", str(e))
            return
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.mgr = mgr
        self.source = source
        self.dest = dest
        self.root.unbind("<Return>")

        incomplete = SyncJournal.load_incomplete()
        if incomplete:
            pending = incomplete.pending_actions
            n = len(pending)
            messagebox.showwarning(
                "Interrupted sync detected",
                f"A previous sync was interrupted with {n} operation(s) "
                f"remaining.\n\n"
                f"The workspace may be in a partial state. Use the Playlists "
                f"menu to restore from a backup, or press Refresh (F5) to "
                f"rescan from the device.",
            )
            SyncJournal.discard()
            _cleanup_temp_files(mgr.writer.root)
            for pid in list(mgr.store.playlists):
                mgr.rescan_playlist(pid)

        if is_external and is_empty:
            has_audio = any(
                f.suffix.lower() in AUDIO_EXTS
                for f in playlists_dir.rglob("*") if f.is_file()
            )
            if has_audio:
                messagebox.showwarning(
                    "Possible external playlist",
                    f"The folder:\n{playlists_dir}\n\n"
                    "contains audio files but was not created by EchoList. "
                    "You may have imported this playlist from an external source.\n\n"
                    "EchoList will need to modify metadata (album artist, album, "
                    "track number) on files in this folder to make playlists work "
                    "on the device.\n\n"
                    "Make sure this is not your only copy of these files.",
                )

        self._show_main()

    def _offer_snapshot_restore(self, snapshot: dict, source: str, dest: str) -> bool:
        """Show restore dialog. Returns True if restore was accepted (and workspace opened)."""
        store_data = snapshot.get("store", {})
        playlists = store_data.get("playlists", {})
        if not playlists:
            return False

        snap_config = snapshot.get("config", {})
        snap_source = snap_config.get("source_root", source)

        snap_folder = snap_config.get("playlist_folder", DEFAULT_PLAYLIST_FOLDER)
        workspace = Path(dest) / snap_folder
        total_tracks = sum(len(pl.get("tracks", [])) for pl in playlists.values())
        pl_list = "\n".join(
            f"  - {workspace / pl['folder']}"
            for pl in list(playlists.values())[:8]
        )
        if len(playlists) > 8:
            pl_list += f"\n  ... and {len(playlists) - 8} more"

        result = messagebox.askyesno(
            "Previous playlists found",
            f"This device had EchoList playlists before.\n\n"
            f"Playlists will be restored to:\n{pl_list}\n\n"
            f"{total_tracks} track(s) total.\n\n"
            f"Source library: {snap_source}\n\n"
            f"Restore these playlists? The tracks will be staged as "
            f"pending — press Sync to re-copy them to the device.",
        )
        if not result:
            return False

        try:
            mgr = PlaylistManager.init(
                snap_source, dest,
                node_name=snap_config.get("node_name", "* PLAYLISTS *"),
                album_prefix=snap_config.get("album_prefix", ""),
                playlist_folder=snap_folder,
                backup_interval=snap_config.get("backup_interval", 5),
            )
            save_defaults(snap_source, dest)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return False

        self.mgr = mgr
        self.source = snap_source
        self.dest = dest
        self.root.unbind("<Return>")

        missing_sources = []
        source_root = Path(snap_source)
        for pid, pl in playlists.items():
            try:
                mgr.create_playlist(pl["name"])
            except ValueError:
                pass
            for t in pl.get("tracks", []):
                src_path = t.get("src_path", "")
                if not src_path:
                    continue
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                else:
                    missing_sources.append(src_path)

        self._show_main()

        if missing_sources:
            n = len(missing_sources)
            sample = "\n".join(f"  - {s}" for s in missing_sources[:10])
            if n > 10:
                sample += f"\n  ... and {n - 10} more"
            messagebox.showwarning(
                "Some source files missing",
                f"{n} track(s) could not be found in the source library "
                f"and were skipped:\n\n{sample}\n\n"
                f"The remaining tracks are staged and ready to sync.",
            )

        return True

    # ── Main ──

    def _show_main(self):
        for w in self.root.winfo_children():
            w.destroy()

        self._center_window(850, 700)
        self.root.minsize(750, 600)

        # Pack status bar FIRST (side=bottom) so it never gets hidden
        self._build_status_bar()

        # Main content above status
        main_pw = ttk.PanedWindow(self.root, orient="horizontal")
        main_pw.pack(fill="both", expand=True, padx=4, pady=(4, 0))

        # ── LEFT COLUMN ──
        left_col = ttk.Frame(main_pw)
        main_pw.add(left_col, weight=1)

        left_pw = ttk.PanedWindow(left_col, orient="vertical")
        left_pw.pack(fill="both", expand=True)

        # Source browser
        src_frame = ttk.Frame(left_pw)
        left_pw.add(src_frame, weight=3)

        src_header = ttk.Frame(src_frame)
        src_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(src_header, text="SOURCE", style="Section.TLabel").pack(side="left")
        ttk.Button(src_header, text="Browse...", command=self._add_files_dialog).pack(side="right")

        # Source search bar
        self._src_search_var = tk.StringVar()
        self._src_search_var.trace_add("write", lambda *_: self._on_source_search())
        src_search_entry = tk.Entry(
            src_frame, textvariable=self._src_search_var,
            bg=BG_INPUT, fg=FG, insertbackground=FG,
            relief="flat", font=("Consolas", 9))
        src_search_entry.pack(fill="x", padx=4, pady=(0, 2))
        self._src_search_entry = src_search_entry
        self._src_search_after_id = None
        self._src_search_entry.bind("<Escape>", lambda e: self._src_search_clear())
        self._src_search_entry.bind("<FocusIn>", self._src_search_focus_in)
        self._src_search_entry.bind("<FocusOut>", self._src_search_focus_out)
        self._src_search_placeholder = True
        self._src_search_show_placeholder()

        src_tree_frame = ttk.Frame(src_frame)
        src_tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        self.source_tree = ttk.Treeview(src_tree_frame, selectmode="extended")
        self.source_tree.heading("#0", text="", anchor="w")
        src_scroll = ttk.Scrollbar(src_tree_frame, orient="vertical", command=self.source_tree.yview)
        self.source_tree.configure(yscrollcommand=src_scroll.set)
        self.source_tree.pack(side="left", fill="both", expand=True)
        src_scroll.pack(side="right", fill="y")
        self.source_tree.bind("<<TreeviewOpen>>", self._on_source_expand)
        self.source_tree.bind("<Double-1>", self._on_source_double_click)

        # Drag-and-drop bindings (add="+" so default selection still works)
        self.source_tree.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.source_tree.bind("<B1-Motion>", self._drag_motion, add="+")
        self.source_tree.bind("<ButtonRelease-1>", self._drag_drop, add="+")
        self._drag_indicator = None

        # Playlists
        pl_frame = ttk.Frame(left_pw)
        left_pw.add(pl_frame, weight=1)

        pl_header = ttk.Frame(pl_frame)
        pl_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(pl_header, text="PLAYLISTS", style="Section.TLabel").pack(side="left")
        ttk.Button(pl_header, text="+ New", command=self._create_playlist).pack(side="right", padx=(4, 0))
        ttk.Button(pl_header, text=".m3u", command=self._import_m3u_dialog).pack(side="right", padx=(4, 0))
        ttk.Button(pl_header, text="Delete", command=self._delete_playlist).pack(side="right")


        # TODO: star prefix feature disabled — re-enable when stable
        # self._star_var = tk.BooleanVar(value=self.mgr.config.star_prefix)
        # self._star_cb = tk.Checkbutton(pl_header, text="★", variable=self._star_var,
        #                                 command=self._toggle_star_prefix,
        #                                 bg=BG, fg=FG, selectcolor=BG_INPUT,
        #                                 activebackground=BG, activeforeground=FG,
        #                                 font=("Consolas", 11))
        # self._star_cb.pack(side="right", padx=(0, 4))

        pl_tree_frame = ttk.Frame(pl_frame)
        pl_tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.playlist_tree = ttk.Treeview(pl_tree_frame, columns=("status", "name"),
                                           show="tree", selectmode="browse")
        self.playlist_tree.column("#0", width=0, stretch=False)
        self.playlist_tree.column("status", width=20, minwidth=20, stretch=False, anchor="center")
        self.playlist_tree.column("name", width=180)
        self.playlist_tree.pack(side="left", fill="both", expand=True)
        self.playlist_tree.tag_configure("imported", foreground="#dd8833")
        self.playlist_tree.tag_configure("busy", foreground=FG_DIM)
        self.playlist_tree.tag_configure("offloaded", foreground=FG_DIM)
        self.playlist_tree.tag_configure("pending_pl", foreground=PENDING_FG)
        self.playlist_tree.tag_configure("highlight", foreground=YELLOW)
        self.playlist_tree.bind("<<TreeviewSelect>>", self._on_playlist_select)
        self.playlist_tree.bind("<Button-3>", self._playlist_context_menu)

        self._rename_entry = None
        self._busy_playlists: set[str] = set()
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        self._spinner_after_id = None

        # ── RIGHT COLUMN: tracks ──
        right_col = ttk.Frame(main_pw)
        main_pw.add(right_col, weight=2)

        trk_header = ttk.Frame(right_col)
        trk_header.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(trk_header, text="TRACKS", style="Section.TLabel").pack(side="left")
        ttk.Button(trk_header, text="Remove", command=self._remove_track).pack(side="right")
        self.fix_meta_btn = ttk.Button(trk_header, text="Fix metadata",
                                        command=self._fix_metadata_clicked)
        self.undo_btn = ttk.Button(trk_header, text="Undo", command=self._do_undo)
        self.undo_btn.pack(side="right", padx=(0, 4))
        self.undo_lbl = tk.Label(trk_header, text="", font=("Consolas", 8),
                                  bg=BG, fg=FG_DIM, anchor="w")
        self.undo_lbl.pack(side="right", padx=(0, 4))

        trk_tree_frame = ttk.Frame(right_col)
        trk_tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 2))

        cols = ("index", "title", "artist")
        self.track_tree = ttk.Treeview(trk_tree_frame, columns=cols, show="headings",
                                        selectmode="extended")
        self.track_tree.heading("index", text="#", command=lambda: self._sort_tracks("index"))
        self.track_tree.heading("title", text="TITLE", command=lambda: self._sort_tracks("title"))
        self.track_tree.heading("artist", text="ARTIST", command=lambda: self._sort_tracks("artist"))
        self.track_tree.column("index", width=35, minwidth=35, stretch=False)
        self.track_tree.column("title", width=250)
        self.track_tree.column("artist", width=160)

        trk_scroll = ttk.Scrollbar(trk_tree_frame, orient="vertical", command=self.track_tree.yview)
        self.track_tree.configure(yscrollcommand=trk_scroll.set)
        self.track_tree.pack(side="left", fill="both", expand=True)
        trk_scroll.pack(side="right", fill="y")

        # Track tree tags for pending items
        self.track_tree.tag_configure("pending", foreground=PENDING_FG)
        self.track_tree.tag_configure("removed", foreground="#555555")
        self.track_tree.tag_configure("offloaded_track", foreground=FG_DIM)
        self.track_tree.tag_configure("loading", foreground=FG_DIM)

        # Right-click context menu on tracks
        self.track_tree.bind("<Button-3>", self._track_context_menu)

        # Delete/Backspace on track tree removes selected tracks
        self.track_tree.bind("<Delete>", self._remove_track)
        self.track_tree.bind("<BackSpace>", self._remove_track)

        # Drag-to-reorder on track tree
        self._trk_drag_iid = None
        self.track_tree.bind("<ButtonPress-1>", self._trk_drag_start, add="+")
        self.track_tree.bind("<B1-Motion>", self._trk_drag_motion, add="+")
        self.track_tree.bind("<ButtonRelease-1>", self._trk_drag_end, add="+")

        self._build_menu()
        self._refresh_playlists()
        self._update_status()
        self._refresh_expensive_stats()
        self.root.bind("<Control-z>", lambda e: self._do_undo())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        source_root = Path(self.mgr.config.source_root)
        if source_root.exists():
            self._populate_source_tree(source_root)

    def _build_status_bar(self):
        status_outer = tk.Frame(self.root, bg=BG_PANEL, bd=0)
        status_outer.pack(fill="x", side="bottom", padx=4, pady=4)

        inner = tk.Frame(status_outer, bg=BG_PANEL, padx=10, pady=8)
        inner.pack(fill="x")

        # Pending label
        pending_row = tk.Frame(inner, bg=BG_PANEL)
        pending_row.pack(fill="x", pady=(0, 4))
        self.pending_lbl = tk.Label(pending_row, text="", font=("Consolas", 9),
                                     bg=BG_PANEL, fg=PENDING_FG, anchor="center")
        self.pending_lbl.pack(fill="x")

        # SYNC row — button and progress bar share the same space
        self.sync_frame = tk.Frame(inner, bg=BG_PANEL)
        self.sync_frame.pack(fill="x", pady=(0, 8))

        self.sync_btn = ttk.Button(self.sync_frame, text="[  S Y N C  ]", style="Sync.TButton",
                                    command=self._do_sync)
        self.sync_btn.pack(anchor="center", ipadx=20, ipady=4)

        self.sync_progress = ttk.Progressbar(self.sync_frame, length=200, mode="determinate",
                                              style="Sync.Horizontal.TProgressbar")
        self.sync_status_lbl = tk.Label(self.sync_frame, text="", font=("Consolas", 9),
                                         bg=BG_PANEL, fg=FG_DIM, anchor="center")
        self.sync_file_bar = ttk.Progressbar(self.sync_frame, length=200, mode="determinate",
                                              style="Yellow.Horizontal.TProgressbar")

        # Stats
        row1 = tk.Frame(inner, bg=BG_PANEL)
        row1.pack(fill="x", pady=(0, 4))
        self.lbl_playlists = tk.Label(row1, text="PLAYLISTS: 0", font=("Consolas", 10, "bold"),
                                       bg=BG_PANEL, fg=FG, anchor="w")
        self.lbl_playlists.pack(side="left")
        self.lbl_workspace = tk.Label(row1, text="0 bytes", font=("Consolas", 9),
                                       bg=BG_PANEL, fg=FG_DIM, anchor="e")
        self.lbl_workspace.pack(side="right")

        row2 = tk.Frame(inner, bg=BG_PANEL)
        row2.pack(fill="x", pady=2)
        self.lbl_tracks = tk.Label(row2, text=f"TRACKS: 0 / {MAX_TRACKS}",
                                    font=("Consolas", 10, "bold"), bg=BG_PANEL, fg=FG, anchor="w")
        self.lbl_tracks.pack(side="left")
        self.track_pct_lbl = tk.Label(row2, text="0%", font=("Consolas", 9),
                                       bg=BG_PANEL, fg=FG_DIM, anchor="e")
        self.track_pct_lbl.pack(side="right")
        self.track_bar = ttk.Progressbar(inner, length=200, mode="determinate",
                                          style="Green.Horizontal.TProgressbar")
        self.track_bar.pack(fill="x", pady=(0, 6))

        row3 = tk.Frame(inner, bg=BG_PANEL)
        row3.pack(fill="x", pady=2)
        self.lbl_drive = tk.Label(row3, text="DRIVE: 0%", font=("Consolas", 10, "bold"),
                                   bg=BG_PANEL, fg=FG, anchor="w")
        self.lbl_drive.pack(side="left")
        self.drive_pct_lbl = tk.Label(row3, text="", font=("Consolas", 9),
                                       bg=BG_PANEL, fg=FG_DIM, anchor="e")
        self.drive_pct_lbl.pack(side="right")
        self.drive_bar = ttk.Progressbar(inner, length=200, mode="determinate",
                                          style="Green.Horizontal.TProgressbar")
        self.drive_bar.pack(fill="x")

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=BG_PANEL, fg=FG, activebackground=RED_DARK,
                          activeforeground=FG_BRIGHT, borderwidth=0)
        self.root.config(menu=menubar)
        file_menu = tk.Menu(menubar, tearoff=0, bg=BG_PANEL, fg=FG,
                            activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        file_menu.add_command(label="Refresh", command=self._refresh_all, accelerator="F5")
        file_menu.add_separator()
        file_menu.add_command(label="Settings...", command=self._show_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)
        self.root.bind("<F5>", lambda e: self._refresh_all())
        menubar.add_cascade(label="File", menu=file_menu)

        self._playlists_menu = tk.Menu(menubar, tearoff=0, bg=BG_PANEL, fg=FG,
                                        activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        menubar.add_cascade(label="Playlists", menu=self._playlists_menu)
        self._refresh_playlists_menu()

    def _set_ui_locked(self, locked: bool):
        state = "disabled" if locked else "!disabled"
        for w in (self.undo_btn,):
            try:
                w.state([state])
            except Exception:
                pass
        if locked:
            self.playlist_tree.configure(selectmode="none")
            self.source_tree.configure(selectmode="none")
        else:
            self.playlist_tree.configure(selectmode="browse")
            self.source_tree.configure(selectmode="extended")

    def _refresh_playlists_menu(self):
        self._playlists_menu.delete(0, "end")
        if not self.mgr:
            return
        for pid, pl in self.mgr.store.playlists.items():
            sub = tk.Menu(self._playlists_menu, tearoff=0, bg=BG_PANEL, fg=FG,
                          activebackground=RED_DARK, activeforeground=FG_BRIGHT)

            sub.add_command(
                label="Create new restore point",
                command=lambda p=pid: self._create_restore_point(p),
            )
            sub.add_separator()

            backups = self.mgr.list_metadata_backups(pid)
            if backups:
                for b in backups:
                    ts = b["timestamp"]
                    label = _format_backup_timestamp(ts)
                    sub.add_command(
                        label=f"Restore: {label}",
                        command=lambda p=pid, t=ts: self._restore_from_point(p, t),
                    )
            else:
                sub.add_command(label="(no restore points)", state="disabled")

            self._playlists_menu.add_cascade(label=pl["name"], menu=sub)

        deleted = self.mgr.list_deleted_playlists()
        if deleted:
            self._playlists_menu.add_separator()
            for info in deleted:
                pid = info["pid"]
                name = info["name"]
                sub = tk.Menu(self._playlists_menu, tearoff=0, bg=BG_PANEL, fg=FG,
                              activebackground=RED_DARK, activeforeground=FG_BRIGHT)
                for b in info["backups"]:
                    ts = b["timestamp"]
                    label = _format_backup_timestamp(ts)
                    sub.add_command(
                        label=f"Restore: {label}",
                        command=lambda p=pid, t=ts: self._restore_deleted(p, t),
                    )
                sub.add_separator()
                sub.add_command(
                    label="Permanently delete",
                    command=lambda p=pid, n=name: self._permanently_delete_playlist(p, n),
                )
                self._playlists_menu.add_cascade(label=f"{name} (deleted)", menu=sub)

    def _permanently_delete_playlist(self, pid: str, name: str):
        confirm = messagebox.askyesno(
            "Permanently delete",
            f"Permanently delete all restore points for '{name}'?\n\n"
            f"This cannot be undone.",
        )
        if not confirm:
            return
        from .config import delete_all_backups
        delete_all_backups(self.mgr.writer.root, pid)
        # Purge any leftover staged changes for this pid
        self.staging.pending_adds = [a for a in self.staging.pending_adds if a["pid"] != pid]
        self.staging.pending_removes = [r for r in self.staging.pending_removes if r["pid"] != pid]
        self.staging.pending_reorders.pop(pid, None)
        self.staging.save()
        self._refresh_playlists_menu()

    def _create_restore_point(self, pid: str):
        playlist = self.mgr.store.playlists.get(pid)
        if not playlist:
            return
        folder_path = self.mgr.writer.root / playlist["folder"]
        result = self.mgr.backup_playlist_metadata(pid)
        if result:
            self._refresh_playlists_menu()
            messagebox.showinfo(
                "Restore point created",
                f"Saved metadata snapshot for '{playlist['name']}'.\n\n"
                f"Tracks in:\n{folder_path}",
            )
        else:
            messagebox.showinfo(
                "No tracks",
                f"Playlist '{playlist['name']}' has no tracks to back up.",
            )

    def _restore_from_point(self, pid: str, timestamp: str):
        playlist = self.mgr.store.playlists.get(pid)
        if not playlist:
            return
        label = _format_backup_timestamp(timestamp)
        confirm = messagebox.askyesno(
            "Restore to point",
            f"Restore playlist '{playlist['name']}' to '{label}'?\n\n"
            f"This will restore the playlist to exactly how it was at that point:\n"
            f"- Tracks added since then will be removed\n"
            f"- Tracks deleted since then will be re-staged for sync\n"
            f"- Metadata will be restored on remaining tracks\n\n"
            f"A restore point of the current state will be saved first.\n\n"
            f"Continue?",
        )
        if not confirm:
            return
        name = playlist["name"]

        def restore():
            return self.mgr.restore_playlist_to_point(pid, timestamp)

        def on_done(result):
            source_root = Path(self.mgr.config.source_root)
            missing = []
            for s in result["to_stage"]:
                src_path = s["src_path"]
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                else:
                    missing.append(src_path)

            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._refresh_tracks()
            self._update_status()

            parts = []
            if result["restored"]:
                parts.append(f"Restored metadata on {result['restored']} track(s).")
            if result["removed"]:
                parts.append(f"Removed {result['removed']} track(s) added after this point.")
            if result["to_stage"]:
                staged = len(result["to_stage"]) - len(missing)
                if staged:
                    parts.append(f"Staged {staged} track(s) for re-syncing.")
            if missing:
                parts.append(f"{len(missing)} source file(s) could not be found.")
            messagebox.showinfo("Playlist restored", "\n".join(parts) if parts else "No changes needed.")

        self._run_playlist_op(pid, name, restore, on_done)

    def _restore_deleted(self, pid: str, timestamp: str):
        label = _format_backup_timestamp(timestamp)
        from .config import load_backup
        data = load_backup(self.mgr.writer.root, pid, timestamp)
        if not data:
            messagebox.showerror("Error", "Could not load backup data.")
            return
        name = data.get("playlist_name", pid)
        tracks = data.get("tracks", [])
        src_count = sum(1 for t in tracks if t.get("src_path"))

        confirm = messagebox.askyesno(
            "Restore deleted playlist",
            f"Restore playlist '{name}' from '{label}'?\n\n"
            f"{src_count} track(s) will be staged for syncing.\n\n"
            f"Press Sync after restoring to copy the tracks back to the device.",
        )
        if not confirm:
            return

        self.playlist_tree.insert("", "end", iid=pid, values=("⟳", name))

        def restore():
            return self.mgr.restore_deleted_playlist(pid, timestamp)

        def on_done(sources):
            source_root = Path(self.mgr.config.source_root)
            missing = []
            for s in sources:
                src_path = s["src_path"]
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                else:
                    missing.append(src_path)

            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._update_status()

            if missing:
                n = len(missing)
                sample = "\n".join(f"  - {s}" for s in missing[:10])
                if n > 10:
                    sample += f"\n  ... and {n - 10} more"
                messagebox.showwarning(
                    "Some source files missing",
                    f"{n} track(s) could not be found in the source library "
                    f"and were skipped:\n\n{sample}\n\n"
                    f"The remaining tracks are staged and ready to sync.",
                )

        self._run_playlist_op(pid, name, restore, on_done)

    # ── Source tree ──

    def _populate_source_tree(self, root_path: Path):
        self.source_tree.delete(*self.source_tree.get_children())
        self._insert_children("", root_path)

    def _insert_children(self, parent_iid: str, path: Path):
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name.lower() in _HIDDEN_DIRS:
                continue
            if entry.is_file() and entry.suffix.lower() not in AUDIO_EXTS | M3U_EXTS:
                continue
            display = entry.name + ("/" if entry.is_dir() else "")
            iid = self.source_tree.insert(parent_iid, "end", text=display, values=(str(entry),))
            if entry.is_dir():
                self.source_tree.insert(iid, "end", text="...")

    def _on_source_expand(self, event):
        iid = self.source_tree.focus()
        children = self.source_tree.get_children(iid)
        if len(children) == 1 and self.source_tree.item(children[0], "text") == "...":
            self.source_tree.delete(children[0])
            path = Path(self.source_tree.item(iid, "values")[0])
            self._insert_children(iid, path)

    def _on_source_double_click(self, event):
        iid = self.source_tree.focus()
        if not iid:
            return
        values = self.source_tree.item(iid, "values")
        if not values:
            return
        path = Path(values[0])
        if path.is_file():
            if path.suffix.lower() in M3U_EXTS:
                self._import_m3u_file(path)
            else:
                self._stage_add_files([path])

    # ── Source search ──

    def _src_search_show_placeholder(self):
        self._src_search_placeholder = True
        self._src_search_entry.config(fg=FG_DIM)
        self._src_search_var.set("")
        self._src_search_entry.insert(0, "Search...")

    def _src_search_focus_in(self, event):
        if self._src_search_placeholder:
            self._src_search_entry.delete(0, "end")
            self._src_search_entry.config(fg=FG)
            self._src_search_placeholder = False

    def _src_search_focus_out(self, event):
        if not self._src_search_var.get().strip():
            self._src_search_show_placeholder()

    def _src_search_clear(self):
        self._src_search_entry.delete(0, "end")
        self._src_search_show_placeholder()
        self.root.focus_set()
        source_root = Path(self.mgr.config.source_root)
        if source_root.exists():
            self._populate_source_tree(source_root)

    def _on_source_search(self):
        if self._src_search_placeholder:
            return
        if self._src_search_after_id:
            self.root.after_cancel(self._src_search_after_id)
        self._src_search_after_id = self.root.after(150, self._do_source_search)

    def _do_source_search(self):
        self._src_search_after_id = None
        query = self._src_search_var.get().strip().lower()
        if not query:
            source_root = Path(self.mgr.config.source_root)
            if source_root.exists():
                self._populate_source_tree(source_root)
            return
        source_root = Path(self.mgr.config.source_root)
        if not source_root.exists():
            return
        self.source_tree.delete(*self.source_tree.get_children())
        matches = []
        limit = 200
        for p in source_root.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS | M3U_EXTS:
                if query in p.name.lower():
                    matches.append(p)
                    if len(matches) >= limit:
                        break
        for p in matches:
            try:
                rel = p.relative_to(source_root)
                display = f"{p.name}  ({rel.parent})"
            except ValueError:
                display = p.name
            self.source_tree.insert("", "end", text=display, values=(str(p),))

    # ── Track context menu ──

    def _current_playlist_offloaded(self) -> bool:
        if not self.current_pid:
            return False
        pl = self.mgr.store.playlists.get(self.current_pid)
        return bool(pl and pl.get("offloaded"))

    def _show_popup_menu(self, menu, event):
        """Show a context menu and ensure any previous one is dismissed."""
        if hasattr(self, "_popup_menu") and self._popup_menu:
            try:
                self._popup_menu.unpost()
                self._popup_menu.destroy()
            except Exception:
                pass
        self._popup_menu = menu
        menu.tk_popup(event.x_root, event.y_root)

    def _track_context_menu(self, event):
        if self._current_playlist_offloaded():
            return
        iid = self.track_tree.identify_row(event.y)
        if not iid:
            return
        self.track_tree.selection_set(iid)
        idx = self.track_tree.index(iid)
        if idx >= len(self._track_data):
            return
        track = self._track_data[idx]
        src_path = track.get("src_path", "")

        menu = tk.Menu(self.root, tearoff=0, bg=BG_PANEL, fg=FG,
                       activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        menu.add_command(label="Show in source",
                         command=lambda: self._show_track_in_source(src_path),
                         state="normal" if src_path else "disabled")
        menu.add_command(label="Show in playlists",
                         command=lambda: self._show_track_in_playlists(src_path),
                         state="normal" if src_path else "disabled")
        self._show_popup_menu(menu, event)

    def _show_track_in_source(self, src_path: str):
        if not src_path:
            return
        source_root = Path(self.mgr.config.source_root)
        full = _resolve_source_file(src_path, source_root)
        if not full:
            messagebox.showinfo("Not found", f"Source file not found:\n{src_path}")
            return
        # Clear any active search
        if not self._src_search_placeholder:
            self._src_search_clear()
        try:
            rel = full.relative_to(source_root)
        except ValueError:
            messagebox.showinfo("Not found", f"File is outside source root:\n{full}")
            return
        parts = list(rel.parts)
        parent_iid = ""
        for i, part in enumerate(parts):
            children = self.source_tree.get_children(parent_iid)
            # Expand lazy placeholder if present
            if len(children) == 1 and self.source_tree.item(children[0], "text") == "...":
                self.source_tree.delete(children[0])
                parent_path = source_root / Path(*parts[:i]) if i > 0 else source_root
                self._insert_children(parent_iid, parent_path)
                children = self.source_tree.get_children(parent_iid)
            # Search for this path component among children
            found = None
            for child in children:
                child_path = Path(self.source_tree.item(child, "values")[0])
                if child_path.name == part:
                    found = child
                    break
            if not found:
                # Node not in tree yet — load it from disk
                node_path = source_root / Path(*parts[:i + 1])
                if not node_path.exists():
                    return
                is_dir = node_path.is_dir()
                display = part + ("/" if is_dir else "")
                found = self.source_tree.insert(parent_iid, "end", text=display,
                                                values=(str(node_path),))
                if is_dir:
                    self._insert_children(found, node_path)
            elif i < len(parts) - 1:
                self.source_tree.item(found, open=True)
                sub_children = self.source_tree.get_children(found)
                if len(sub_children) == 1 and self.source_tree.item(sub_children[0], "text") == "...":
                    self.source_tree.delete(sub_children[0])
                    child_path = Path(self.source_tree.item(found, "values")[0])
                    self._insert_children(found, child_path)
            parent_iid = found
        self.source_tree.selection_set(found)
        self.source_tree.see(found)
        self.source_tree.focus(found)

    def _show_track_in_playlists(self, src_path: str):
        if not src_path:
            return
        matching_pids = []
        for pid, pl in self.mgr.store.playlists.items():
            for t in pl["tracks"]:
                if t.get("src_path") == src_path:
                    matching_pids.append(pid)
                    break
        if not matching_pids:
            messagebox.showinfo("Not found", "This track is not in any other playlist.")
            return
        for pid in matching_pids:
            try:
                self.playlist_tree.item(pid, tags=("highlight",))
            except Exception:
                pass
        self.root.after(2000, lambda: self._clear_playlist_highlight(matching_pids))

    def _clear_playlist_highlight(self, pids: list[str]):
        for pid in pids:
            try:
                pl = self.mgr.store.playlists.get(pid)
                if pl:
                    tags = ("offloaded",) if pl.get("offloaded") else ()
                    self.playlist_tree.item(pid, tags=tags)
            except Exception:
                pass

    # ── Playlist context menu (offload/onload) ──

    def _playlist_context_menu(self, event):
        iid = self.playlist_tree.identify_row(event.y)
        if not iid or iid.startswith("_imported:"):
            return
        self.playlist_tree.selection_set(iid)
        pl = self.mgr.store.playlists.get(iid)
        if not pl:
            return

        is_offloaded = pl.get("offloaded", False)
        has_pending = self._playlist_has_pending(iid)
        has_orphans = any(not t.get("src_path") for t in pl.get("tracks", []))

        menu = tk.Menu(self.root, tearoff=0, bg=BG_PANEL, fg=FG,
                       activebackground=RED_DARK, activeforeground=FG_BRIGHT)
        if is_offloaded:
            menu.add_command(label="Onload (restore to device)",
                            command=lambda: self._onload_playlist(iid))
        else:
            menu.add_command(label="Reindex",
                            command=lambda: self._reindex_playlist(iid),
                            state="normal" if pl.get("tracks") and not is_offloaded else "disabled")
            can_offload = not has_pending and not has_orphans
            offload_label = ("Offload (has tracks without source)"
                             if has_orphans else "Offload (remove from device)")
            menu.add_command(label=offload_label,
                            command=lambda: self._offload_playlist(iid),
                            state="normal" if can_offload else "disabled")
        menu.add_separator()
        menu.add_command(label="Rename", command=lambda: self._start_inline_rename(iid))
        menu.add_command(label="Delete", command=self._delete_playlist)
        self._show_popup_menu(menu, event)

    def _reindex_playlist(self, pid: str):
        pl = self.mgr.store.playlists.get(pid)
        if not pl or not pl.get("tracks"):
            return
        order = [{"key": f"c:{t['index']}"} for t in pl["tracks"]]
        self.staging.set_reorder(pid, order)
        self._refresh_playlists()
        self._refresh_tracks()
        self._update_status()

    def _offload_playlist(self, pid: str):
        pl = self.mgr.store.playlists.get(pid)
        if not pl:
            return
        if any(not t.get("src_path") for t in pl.get("tracks", [])):
            return
        name = pl["name"]
        confirm = messagebox.askyesno(
            "Offload playlist",
            f"Offload '{name}'?\n\n"
            f"This will remove {len(pl['tracks'])} track(s) from the device "
            f"to free up space. A restore point is saved automatically.\n\n"
            f"You can onload it back later to re-sync the tracks.",
        )
        if not confirm:
            return

        def offload():
            self.mgr.backup_playlist_metadata(pid)
            folder = pl["folder"]
            try:
                self.mgr.writer.delete(folder)
            except Exception:
                pass
            pl["tracks"] = []
            pl["offloaded"] = True
            self.mgr.store.save()

        def on_done(_):
            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._refresh_tracks()
            self._update_status()

        self._run_playlist_op(pid, name, offload, on_done)

    def _onload_playlist(self, pid: str):
        pl = self.mgr.store.playlists.get(pid)
        if not pl:
            return
        from .config import load_backup, list_backups
        backups = list_backups(self.mgr.writer.root, pid)
        if not backups:
            messagebox.showerror("No backup", "No restore point found for this playlist.")
            return

        name = pl["name"]

        def onload():
            data = load_backup(self.mgr.writer.root, pid, backups[0]["timestamp"])
            if not data:
                raise ValueError("Could not load backup data")
            return data.get("tracks", [])

        def on_done(tracks):
            pl["offloaded"] = False
            pl["tracks"] = []
            self.mgr.store.save()

            # Recreate the folder so sync can write into it
            folder = pl["folder"]
            try:
                (self.mgr.writer.root / folder).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            # Stage all tracks for re-copying from source
            source_root = Path(self.mgr.config.source_root)
            staged = 0
            missing = []
            for entry in tracks:
                src_path = entry.get("src_path", "")
                if not src_path:
                    missing.append(entry.get("copy_name", "(unknown)"))
                    continue
                full = _resolve_source_file(src_path, source_root)
                if full:
                    title, artist = _read_tags_from_file(full)
                    self.staging.stage_add(pid, str(full), title, artist)
                    staged += 1
                else:
                    missing.append(src_path)

            self._invalidate_caches(pid)
            self._refresh_playlists()
            self._refresh_tracks()
            self._update_status()
            parts = []
            if staged:
                parts.append(f"Staged {staged} track(s) for syncing.")
            if missing:
                parts.append(f"{len(missing)} source file(s) not found.")
            parts.append("Press Sync to copy tracks back to the device.")
            messagebox.showinfo("Playlist onloaded", "\n".join(parts))

        self._run_playlist_op(pid, name, onload, on_done)

    # ── Drag and drop ──

    def _drag_start(self, event):
        iid = self.source_tree.identify_row(event.y)
        if not iid:
            self._drag_data = None
            return
        # Capture selection now, before the default handler clears it to one item
        saved = self.source_tree.selection()
        if iid not in saved:
            # Clicking a new item — drag just that one, not the old selection
            saved = (iid,)
        self._drag_data = {
            "x": event.x_root, "y": event.y_root, "started": False,
            "selection": saved,
        }
        # Restore multi-selection after the default handler deselects
        if len(saved) > 1:
            self.root.after_idle(lambda: self.source_tree.selection_set(saved))

    def _drag_motion(self, event):
        if not self._drag_data:
            return
        dx = abs(event.x_root - self._drag_data["x"])
        dy = abs(event.y_root - self._drag_data["y"])
        if not self._drag_data["started"] and (dx > 8 or dy > 8):
            self._drag_data["started"] = True
            selected = self._drag_data.get("selection", ())
            if not selected:
                self._drag_data = None
                return
            n = len(selected)
            if self._drag_indicator:
                self._drag_indicator.destroy()
            self._drag_indicator = tk.Label(self.root, text=f"+ {n} file{'s' if n != 1 else ''}",
                                             bg=RED_DARK, fg=FG_BRIGHT,
                                             font=("Consolas", 8), padx=4, pady=2)

        if self._drag_data and self._drag_data.get("started") and self._drag_indicator:
            rx = event.x_root - self.root.winfo_rootx()
            ry = event.y_root - self.root.winfo_rooty()
            self._drag_indicator.place(x=rx + 14, y=ry + 14)
            self._drag_indicator.lift()

    def _drag_drop(self, event):
        was_dragging = self._drag_data and self._drag_data.get("started")
        saved_selection = self._drag_data.get("selection", ()) if self._drag_data else ()

        if self._drag_indicator:
            self._drag_indicator.destroy()
            self._drag_indicator = None
        self._drag_data = None

        if not was_dragging:
            return

        # Check if dropped over the right column (tracks area or its parent)
        rx = event.x_root
        ry = event.y_root
        try:
            tx = self.track_tree.winfo_rootx()
            ty = self.track_tree.winfo_rooty()
            tw = self.track_tree.winfo_width()
            th = self.track_tree.winfo_height()
            # generous drop zone: anywhere on the right half of the window
            win_mid = self.root.winfo_rootx() + self.root.winfo_width() // 2
            if rx >= win_mid or (tx <= rx <= tx + tw and ty <= ry <= ty + th):
                self._add_selected_from_source(saved_selection)
        except Exception:
            pass

    def _add_selected_from_source(self, selected=None):
        if selected is None:
            selected = self.source_tree.selection()
        if not selected:
            return

        m3u_files = []
        audio_paths = []
        for iid in selected:
            values = self.source_tree.item(iid, "values")
            if values:
                p = Path(values[0])
                if p.is_file():
                    if p.suffix.lower() in M3U_EXTS:
                        m3u_files.append(p)
                    else:
                        audio_paths.append(p)
                elif p.is_dir():
                    audio_paths.extend(sorted(
                        f for f in p.rglob("*")
                        if f.is_file() and not f.name.startswith(".")
                        and f.suffix.lower() in AUDIO_EXTS
                    ))

        for m3u in m3u_files:
            self._import_m3u_file(m3u)

        if audio_paths:
            if not self.current_pid:
                messagebox.showinfo("No playlist", "Select or create a playlist first.")
                return
            self._stage_add_files(audio_paths)

    # ── Staging operations ──

    def _stage_add_files(self, paths: list[Path]):
        if self._syncing or self._current_playlist_offloaded():
            return
        if not self.current_pid:
            messagebox.showinfo("No playlist", "Select or create a playlist first.")
            return

        # Collect existing source paths for duplicate check
        existing = set()
        source_root = Path(self.mgr.config.source_root)
        playlist = self.mgr.store.playlists.get(self.current_pid, {})
        for t in playlist.get("tracks", []):
            sp = Path(t.get("src_path", ""))
            if not sp.is_absolute():
                sp = source_root / sp
            existing.add(str(sp.resolve()))
        for a in self.staging.pending_adds:
            if a["pid"] == self.current_pid:
                existing.add(str(Path(a["src"]).resolve()))

        added_indices = []
        skipped = []
        for p in paths:
            resolved = str(p.resolve())
            if resolved in existing:
                skipped.append(p.name)
                continue
            existing.add(resolved)
            title, artist = _read_tags_from_file(p)
            self.staging.stage_add(self.current_pid, str(p), title, artist)
            added_indices.append(len(self.staging.pending_adds) - 1)

        if added_indices:
            n = len(added_indices)
            self._undo_stack.append({
                "type": "add",
                "indices": added_indices,
                "desc": f"Add {n} track{'s' if n != 1 else ''} to {self.current_pid}",
            })
        if skipped:
            messagebox.showinfo("Duplicates skipped",
                                f"{len(skipped)} track(s) already in playlist:\n" +
                                "\n".join(skipped[:10]))
        self._refresh_tracks()
        self._update_status()

    def _stage_remove_track(self, pid: str, index: int, copy_name: str):
        self.staging.stage_remove(pid, index, copy_name)
        self._undo_stack.append({
            "type": "remove",
            "desc": f"Remove #{index} from {pid}",
        })
        self._refresh_tracks()
        self._update_status()

    def _do_undo(self):
        if not self._undo_stack:
            return
        action = self._undo_stack.pop()

        if action["type"] == "add":
            for i in sorted(action["indices"], reverse=True):
                if i < len(self.staging.pending_adds):
                    self.staging.pending_adds.pop(i)
            self.staging.save()
        elif action["type"] == "remove":
            n = action.get("count", 1)
            for _ in range(n):
                if self.staging.pending_removes:
                    self.staging.pending_removes.pop()
            self.staging.save()
        elif action["type"] == "unstage_add":
            pass  # can't re-add unstaged items
        elif action["type"] == "reorder":
            pid = action.get("pid", "")
            if pid in self.staging.pending_reorders:
                del self.staging.pending_reorders[pid]
                self.staging.save()

        self._refresh_tracks()
        self._update_status()

    def _on_close(self):
        if self.staging and self.staging.has_pending:
            n = self.staging.total_ops
            result = messagebox.askyesnocancel(
                "Unsaved changes",
                f"You have {n} pending change{'s' if n != 1 else ''}.\n\n"
                "Yes = Sync now, then exit\n"
                "No = Exit (changes are saved, sync next time)\n"
                "Cancel = Stay",
            )
            if result is None:
                return
            if result:
                self._do_sync_blocking()
        self._alive = False
        if self.mgr:
            self.mgr.release_lock()
        t = getattr(self, "_stats_thread", None)
        if t:
            t.join(timeout=5)
        self.root.destroy()

    def _backup_before_sync(self):
        """Create a restore point for affected playlists, respecting backup_interval."""
        if not self.mgr.config.should_backup():
            self.mgr.config.increment_sync(self.mgr.writer)
            return
        affected = set()
        for a in self.staging.pending_adds:
            affected.add(a["pid"])
        for r in self.staging.pending_removes:
            affected.add(r["pid"])
        for pid in self.staging.pending_reorders:
            affected.add(pid)
        for pid in affected:
            if pid in self.mgr.store.playlists:
                try:
                    self.mgr.backup_playlist_metadata(pid)
                except Exception:
                    pass
        self.mgr.config.increment_sync(self.mgr.writer)

    def _do_sync_blocking(self):
        """Synchronous sync for use during close."""
        self._backup_before_sync()
        removes = sorted(self.staging.pending_removes, key=lambda r: r["index"], reverse=True)
        adds = list(self.staging.pending_adds)
        journal = SyncJournal.begin(removes, adds, self.staging.pending_reorders)
        idx = 0
        for r in removes:
            journal.mark_current(idx)
            try:
                self.mgr.remove_track(r["pid"], r["index"])
            except Exception:
                pass
            journal.mark_done(idx)
            idx += 1
        for a in adds:
            journal.mark_current(idx)
            try:
                self.mgr.add_track(a["pid"], a["src"])
            except Exception:
                pass
            journal.mark_done(idx)
            idx += 1
        self._apply_reorders()
        for i in range(idx, len(journal.actions)):
            journal.mark_done(i)
        journal.complete()
        self.staging.clear()
        self._undo_stack.clear()
        try:
            self.mgr.save_snapshot()
        except Exception:
            pass

    def _apply_reorders(self):
        """Re-tag and rename committed tracks to match the staged reorder."""
        from .tags import apply_playlist_tags
        from .naming import track_filename
        import mutagen

        for pid, order in self.staging.pending_reorders.items():
            if pid not in self.mgr.store.playlists:
                continue
            playlist = self.mgr.store.playlists[pid]
            folder = playlist["folder"]

            committed_by_key = {}
            for t in playlist["tracks"]:
                committed_by_key[f"c:{t['index']}"] = t

            # Build the new order with new indices
            reorder_plan = []
            new_idx = 1
            for entry in order:
                key = entry["key"]
                if key in committed_by_key:
                    t = committed_by_key[key]
                    old_name = t["copy_name"]
                    # Read title from the file to build the new name
                    title = t.get("copy_name", "").split(" - ", 1)[-1].rsplit(".", 1)[0]
                    try:
                        m = mutagen.File(self.mgr.writer.root / folder / old_name, easy=True)
                        if m and "title" in m:
                            title = m["title"][0]
                    except Exception:
                        pass
                    ext = Path(old_name).suffix
                    pad = 3 if len(order) > 99 else 2
                    new_name = track_filename(new_idx, title, ext, pad)
                    reorder_plan.append((t, new_idx, old_name, new_name))
                    new_idx += 1

            # Also pick up any tracks not in the reorder list
            for t in playlist["tracks"]:
                key = f"c:{t['index']}"
                if key in committed_by_key and t not in [r[0] for r in reorder_plan]:
                    old_name = t["copy_name"]
                    ext = Path(old_name).suffix
                    title = old_name.split(" - ", 1)[-1].rsplit(".", 1)[0]
                    pad = 3 if len(order) > 99 else 2
                    new_name = track_filename(new_idx, title, ext, pad)
                    reorder_plan.append((t, new_idx, old_name, new_name))
                    new_idx += 1

            # Two-pass rename to avoid collisions
            # Pass 1: rename to temporary names
            temp_names = []
            for t, idx, old_name, new_name in reorder_plan:
                if old_name != new_name:
                    tmp_name = f"_echolist_tmp_{idx}_{old_name}"
                    old_rel = f"{folder}/{old_name}"
                    tmp_rel = f"{folder}/{tmp_name}"
                    try:
                        self.mgr.writer.rename(old_rel, tmp_rel)
                    except Exception:
                        pass
                    temp_names.append((t, idx, tmp_name, new_name))
                else:
                    temp_names.append((t, idx, old_name, new_name))

            # Pass 2: rename from temp to final
            for t, idx, current_name, new_name in temp_names:
                if current_name != new_name:
                    tmp_rel = f"{folder}/{current_name}"
                    new_rel = f"{folder}/{new_name}"
                    try:
                        self.mgr.writer.rename(tmp_rel, new_rel)
                    except Exception:
                        pass

            # Update store and re-tag
            new_tracks = []
            for t, idx, old_name, new_name in reorder_plan:
                t["index"] = idx
                t["copy_name"] = new_name
                try:
                    path = self.mgr.writer.resolve(f"{folder}/{new_name}")
                    album = self.mgr.config.album_prefix + playlist["name"]
                    apply_playlist_tags(
                        path, self.mgr.config.node_name, album,
                        idx, t.get("src_path", ""), pid,
                    )
                except Exception:
                    pass
                new_tracks.append(t)

            playlist["tracks"] = new_tracks
            self.mgr.store.save()

    # ── SYNC ──

    def _do_sync(self):
        if not self.staging.has_pending or self._syncing:
            return

        self._syncing = True
        self._set_ui_locked(True)
        self._backup_before_sync()
        total = self.staging.total_ops

        # Swap button for progress bars
        self.sync_btn.pack_forget()
        self.sync_status_lbl.pack(fill="x")
        self.sync_progress.pack(fill="x", pady=(2, 0))
        self.sync_file_bar.pack(fill="x", pady=(2, 0))
        self.sync_progress["maximum"] = total
        self.sync_progress["value"] = 0
        self.sync_file_bar["maximum"] = 100
        self.sync_file_bar["value"] = 0

        adds = list(self.staging.pending_adds)
        removes = sorted(self.staging.pending_removes, key=lambda r: r["index"], reverse=True)
        journal = SyncJournal.begin(removes, adds, self.staging.pending_reorders)

        def worker():
            done = 0
            errors = []
            j_idx = 0

            def _ui(fn):
                self._schedule_callback(fn)

            for r in removes:
                journal.mark_current(j_idx)
                done += 1
                _ui(lambda d=done: _update_progress(d, "Removing..."))
                try:
                    self.mgr.remove_track(r["pid"], r["index"])
                except Exception as e:
                    errors.append(f"Remove #{r['index']}: {e}")
                journal.mark_done(j_idx)
                j_idx += 1

            for i, a in enumerate(adds):
                journal.mark_current(j_idx)
                done += 1
                title = a.get("title", Path(a["src"]).stem)
                _ui(lambda d=done, t=title: _update_progress(d, t))
                _ui(lambda: _update_file_progress(0))

                copy_done = [False]

                def on_copy_progress(copied, file_total):
                    pct = int(copied / file_total * 100) if file_total else 100
                    if pct >= 100 and not copy_done[0]:
                        copy_done[0] = True
                        _ui(lambda d=done, t=title: _update_progress(d, f"Tagging {t}"))
                        _ui(lambda: _update_file_progress(0))
                    else:
                        _ui(lambda p=pct: _update_file_progress(p))

                try:
                    self.mgr.add_track(a["pid"], a["src"], progress_cb=on_copy_progress)
                except Exception as e:
                    errors.append(f"{Path(a['src']).name}: {e}")
                journal.mark_done(j_idx)
                j_idx += 1

            self._apply_reorders()
            for i in range(j_idx, len(journal.actions)):
                journal.mark_done(i)
            journal.complete()
            self._schedule_callback(lambda: _finish(errors))

        def _update_progress(done, label=""):
            self.sync_progress["value"] = done
            self.sync_status_lbl.config(text=f"Syncing {done}/{total}  {label}")

        def _update_file_progress(pct):
            self.sync_file_bar["value"] = pct

        def _finish(errors):
            self._syncing = False
            self._set_ui_locked(False)
            self.staging.clear()
            self._undo_stack.clear()
            self._invalidate_caches()
            self.sync_progress.pack_forget()
            self.sync_status_lbl.pack_forget()
            self.sync_file_bar.pack_forget()
            self.sync_btn.pack(anchor="center", ipadx=20, ipady=4)
            self._refresh_playlists()
            self._update_status()
            self._refresh_expensive_stats()
            try:
                self.mgr.save_snapshot()
            except Exception:
                pass
            if errors:
                messagebox.showwarning("Sync errors",
                                        f"Completed with errors:\n" + "\n".join(errors[:15]))
            else:
                self.pending_lbl.config(text="  ✓ Sync complete", fg=GREEN)
                self.root.after(4000, lambda: self.pending_lbl.config(text="", fg=PENDING_FG))

        Thread(target=worker, daemon=True).start()

    # ── Playlists ──

    def _refresh_all(self):
        """Reload source tree, playlists, tracks, and stats."""
        if self._syncing:
            return
        source_root = Path(self.mgr.config.source_root)
        if source_root.exists():
            self._populate_source_tree(source_root)
        for pid in list(self.mgr.store.playlists):
            self.mgr.rescan_playlist(pid)
        self._refresh_playlists()
        self._refresh_tracks()
        self._update_status()
        self._refresh_expensive_stats()

    def _playlist_status_icon(self, pid: str, pl: dict) -> str:
        if pl.get("offloaded"):
            return "◌"
        return "⏏"

    def _playlist_has_pending(self, pid: str) -> bool:
        return any(
            a["pid"] == pid for a in self.staging.pending_adds
        ) or any(
            r["pid"] == pid for r in self.staging.pending_removes
        ) or pid in self.staging.pending_reorders

    def _refresh_playlists(self):
        self.playlist_tree.delete(*self.playlist_tree.get_children())
        for pid, pl in self.mgr.store.playlists.items():
            icon = self._playlist_status_icon(pid, pl)
            if pl.get("offloaded"):
                tags = ("offloaded",)
            elif self._playlist_has_pending(pid):
                tags = ("pending_pl",)
            else:
                tags = ()
            self.playlist_tree.insert("", "end", iid=pid, values=(icon, pl["name"]),
                                      tags=tags)
        self._untracked_playlists = {}
        try:
            for info in self.mgr.detect_untracked_playlists():
                iid = f"_imported:{info['folder']}"
                self.playlist_tree.insert(
                    "", "end", iid=iid,
                    values=("", f"{info['folder']} ({info['track_count']} tracks)"),
                    tags=("imported",),
                )
                self._untracked_playlists[iid] = info
        except Exception:
            pass
        children = self.playlist_tree.get_children()
        if children:
            if self.current_pid and self.current_pid in children:
                self.playlist_tree.selection_set(self.current_pid)
            else:
                self.playlist_tree.selection_set(children[0])
            self._on_playlist_select(None)
        if hasattr(self, "_playlists_menu"):
            self._refresh_playlists_menu()

    def _run_playlist_op(self, pid: str, display_name: str, operation, on_done=None):
        """Run a blocking playlist operation in a background thread with spinner."""
        self._busy_playlists.add(pid)
        try:
            self.playlist_tree.item(pid, values=("⟳", f"{display_name} {self._spinner_frames[0]}"),
                                    tags=("busy",))
        except Exception:
            pass
        self._start_spinner()

        def worker():
            error = None
            result = None
            try:
                result = operation()
            except Exception as e:
                error = e
            self._schedule_callback(lambda: finish(result, error))

        def finish(result, error):
            self._busy_playlists.discard(pid)
            if not self._busy_playlists:
                self._stop_spinner()
            try:
                if pid in [c for c in self.playlist_tree.get_children()]:
                    pl = self.mgr.store.playlists.get(pid)
                    name = pl["name"] if pl else display_name
                    icon = self._playlist_status_icon(pid, pl) if pl else "⏏"
                    tags = ()
                    if pl and pl.get("offloaded"):
                        tags = ("offloaded",)
                    elif pl and self._playlist_has_pending(pid):
                        tags = ("pending_pl",)
                    self.playlist_tree.item(pid, values=(icon, name), tags=tags)
            except Exception:
                pass
            if error:
                messagebox.showerror("Error", str(error))
            elif on_done:
                on_done(result)

        Thread(target=worker, daemon=True).start()

    def _start_spinner(self):
        if self._spinner_after_id is not None:
            return
        self._tick_spinner()

    def _stop_spinner(self):
        if self._spinner_after_id is not None:
            self.root.after_cancel(self._spinner_after_id)
            self._spinner_after_id = None

    def _tick_spinner(self):
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        frame = self._spinner_frames[self._spinner_idx]
        for pid in list(self._busy_playlists):
            try:
                old = self.playlist_tree.item(pid, "values")
                if old and len(old) >= 2:
                    base = old[1].rsplit(" ", 1)[0]
                    self.playlist_tree.item(pid, values=("⟳", f"{base} {frame}"))
            except Exception:
                pass
        if self._busy_playlists:
            self._spinner_after_id = self.root.after(80, self._tick_spinner)
        else:
            self._spinner_after_id = None

    def _on_playlist_select(self, event):
        sel = self.playlist_tree.selection()
        if not sel:
            self.current_pid = None
            self.track_tree.delete(*self.track_tree.get_children())
            return
        selected = sel[0]
        if selected in self._busy_playlists:
            self.playlist_tree.selection_remove(selected)
            if self.current_pid and self.current_pid not in self._busy_playlists:
                try:
                    self.playlist_tree.selection_set(self.current_pid)
                except Exception:
                    pass
            return
        if selected.startswith("_imported:"):
            self.current_pid = None
            self.track_tree.delete(*self.track_tree.get_children())
            self.fix_meta_btn.pack_forget()
            info = self._untracked_playlists.get(selected)
            if info:
                self._offer_adopt_playlist(info)
            return
        self.current_pid = selected
        pl = self.mgr.store.playlists.get(selected)
        if pl and pl.get("offloaded"):
            self._show_offloaded_tracks(selected)
            self.fix_meta_btn.pack_forget()
            return
        self._refresh_tracks()
        self._check_metadata_sync()

    def _check_metadata_sync(self):
        if not self.current_pid:
            self.fix_meta_btn.pack_forget()
            return

        pid = self.current_pid
        cached = self._audit_cache.get(pid)
        if cached is not None:
            self._apply_audit_result(pid, cached)
            return

        def _audit():
            issues = self.mgr.audit_playlist_metadata(pid)
            self._audit_cache[pid] = issues
            if not self._alive:
                return
            self._schedule_callback(lambda: self._apply_audit_result(pid, issues))

        Thread(target=_audit, daemon=True).start()

    def _apply_audit_result(self, pid: str, issues: list[dict]):
        if self.current_pid != pid:
            return
        if issues:
            n = len(set(i["copy_name"] for i in issues))
            self.fix_meta_btn.config(text=f"Fix metadata ({n})")
            self.fix_meta_btn.pack(side="right", padx=(0, 4))
            self._set_fix_meta_tooltip(issues)
        else:
            self.fix_meta_btn.pack_forget()
            self._clear_fix_meta_tooltip()

    def _set_fix_meta_tooltip(self, issues: list[dict]):
        by_file: dict[str, list[str]] = {}
        for i in issues:
            by_file.setdefault(i["copy_name"], []).append(
                f"{i['field']}: '{i['actual']}' → '{i['expected']}'"
            )
        lines = []
        for name, fixes in list(by_file.items())[:8]:
            lines.append(name)
            for f in fixes:
                lines.append(f"  {f}")
        if len(by_file) > 8:
            lines.append(f"... and {len(by_file) - 8} more file(s)")
        tip_text = "\n".join(lines)

        tip_win = [None]

        def show(event):
            tw = tk.Toplevel(self.root)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
            lbl = tk.Label(tw, text=tip_text, justify="left",
                           bg="#333333", fg="#eeeeee", font=("Consolas", 9),
                           padx=6, pady=4, wraplength=500)
            lbl.pack()
            tip_win[0] = tw

        def hide(event):
            if tip_win[0]:
                tip_win[0].destroy()
                tip_win[0] = None

        self.fix_meta_btn.bind("<Enter>", show)
        self.fix_meta_btn.bind("<Leave>", hide)

    def _clear_fix_meta_tooltip(self):
        self.fix_meta_btn.unbind("<Enter>")
        self.fix_meta_btn.unbind("<Leave>")

    def _fix_metadata_clicked(self):
        if not self.current_pid:
            return
        issues = self.mgr.audit_playlist_metadata(self.current_pid)
        if not issues:
            self.fix_meta_btn.pack_forget()
            return

        affected_files = sorted(set(i["copy_name"] for i in issues))
        playlist = self.mgr.store.playlists[self.current_pid]
        folder_path = self.mgr.writer.root / playlist["folder"]

        file_list = "\n".join(f"  - {f}" for f in affected_files[:10])
        if len(affected_files) > 10:
            file_list += f"\n  ... and {len(affected_files) - 10} more"

        result = messagebox.askyesno(
            "Metadata out of sync",
            f"{len(affected_files)} track(s) in this playlist have metadata that "
            f"doesn't match what EchoList expects.\n\n"
            f"Folder:\n{folder_path}\n\n"
            f"Affected files:\n{file_list}\n\n"
            f"EchoList needs to update the album artist, album name, and track "
            f"numbers on these files so they appear correctly on the device.\n\n"
            f"A backup of the original metadata will be saved so you can "
            f"restore it later (Playlists menu > playlist name > Restore).\n\n"
            f"Make sure this is not your only copy of these files.\n\n"
            f"Update metadata now?",
        )
        if result:
            pid = self.current_pid
            pl = self.mgr.store.playlists[pid]

            def fix():
                return self.mgr.fix_playlist_metadata(pid)

            def on_done(fixed):
                self._invalidate_caches(pid)
                self._refresh_playlists_menu()
                self.fix_meta_btn.pack_forget()
                self._refresh_tracks()
                if fixed:
                    messagebox.showinfo(
                        "Metadata updated",
                        f"Updated metadata on {fixed} track(s).\n\n"
                        f"A restore point has been saved. Use the Playlists menu "
                        f"to restore if needed.",
                    )

            self._run_playlist_op(pid, pl["name"], fix, on_done)

    def _offer_adopt_playlist(self, info: dict):
        folder = info["folder"]
        count = info["track_count"]
        folder_path = self.mgr.writer.root / folder
        result = messagebox.askyesno(
            "Imported playlist detected",
            f"The folder '{folder}' contains {count} audio file(s) but wasn't "
            f"created by EchoList.\n\n"
            f"Location:\n{folder_path}\n\n"
            f"To make this playlist work on the device, EchoList needs to "
            f"modify the metadata (album artist, album, track numbers) on "
            f"these files.\n\n"
            f"A restore point will be saved with the original metadata "
            f"so you can undo this later.\n\n"
            f"Adopt this playlist?",
        )
        if not result:
            return

        pid = playlist_id(folder)
        iid = f"_imported:{folder}"
        try:
            self.playlist_tree.delete(iid)
        except Exception:
            pass
        self.playlist_tree.insert("", "end", iid=pid, values=("⟳", folder))

        def adopt():
            return self.mgr.adopt_playlist(folder)

        def on_done(_result):
            self.current_pid = pid
            self._refresh_playlists()
            self._update_status()
            messagebox.showinfo(
                "Playlist adopted",
                f"'{folder}' is now managed by EchoList.\n\n"
                f"A 'before_echolist' restore point has been saved in the "
                f"Playlists menu.",
            )

        self._run_playlist_op(pid, folder, adopt, on_done)

    def _create_playlist(self):
        if self._syncing:
            return
        temp_name = "New Playlist"
        try:
            pid = self.mgr.create_playlist(temp_name)
        except ValueError:
            n = 2
            while True:
                try:
                    temp_name = f"New Playlist {n}"
                    pid = self.mgr.create_playlist(temp_name)
                    break
                except ValueError:
                    n += 1
                    if n > 100:
                        messagebox.showerror("Error", "Could not create playlist.")
                        return

        self._refresh_playlists()
        self._update_status()
        self.playlist_tree.selection_set(pid)
        self.playlist_tree.see(pid)
        self.current_pid = pid
        self._refresh_tracks()
        self.root.after(50, lambda: self._start_inline_rename(pid))

    # ── .m3u import ──

    def _import_m3u_dialog(self):
        """Open a file picker to import .m3u/.m3u8 playlists."""
        if self._syncing:
            return
        files = filedialog.askopenfilenames(
            title="Import .m3u playlists",
            filetypes=[("M3U playlists", "*.m3u *.m3u8"), ("All files", "*.*")],
            initialdir=self.source or str(Path.home()),
        )
        if not files:
            return
        for f in files:
            self._import_m3u_file(Path(f))

    def _import_m3u_file(self, m3u_path: Path):
        """Parse one .m3u file, create a playlist, and stage found tracks."""
        source_root = Path(self.mgr.config.source_root)
        result = parse_m3u(m3u_path, source_root=source_root)

        existing_pids = set(self.mgr.store.playlists.keys())
        name = curate_playlist_name(result["name"], existing_pids)
        tracks = result["tracks"]
        missing = result["missing"]

        if not tracks and not missing:
            messagebox.showinfo("Empty playlist",
                                f"'{m3u_path.name}' contains no track entries.")
            return

        if not tracks and missing:
            messagebox.showwarning(
                "No tracks found",
                f"'{m3u_path.name}' has {len(missing)} entry(ies) but none "
                f"could be found on disk.\n\n"
                f"Make sure the source library path is correct in Settings.",
            )
            return

        try:
            pid = self.mgr.create_playlist(name)
        except ValueError:
            messagebox.showerror("Error", f"Could not create playlist '{name}'.")
            return

        for track_path in tracks:
            title, artist = _read_tags_from_file(track_path)
            self.staging.stage_add(pid, str(track_path), title, artist)

        self._refresh_playlists()
        self.playlist_tree.selection_set(pid)
        self.current_pid = pid
        self._refresh_tracks()
        self._update_status()

        parts = [f"Created playlist '{name}' from {m3u_path.name}",
                 f"{len(tracks)} track(s) staged for sync."]
        if missing:
            parts.append(f"\n{len(missing)} track(s) could not be found:")
            for m in missing[:10]:
                parts.append(f"  - {m}")
            if len(missing) > 10:
                parts.append(f"  ... and {len(missing) - 10} more")
        messagebox.showinfo("Playlist imported", "\n".join(parts))

    def _start_inline_rename(self, pid):
        if pid not in self.mgr.store.playlists:
            return
        self.playlist_tree.update_idletasks()
        try:
            bbox = self.playlist_tree.bbox(pid, column="name")
        except Exception:
            return
        if not bbox:
            return

        x, y, w, h = bbox
        entry = tk.Entry(self.playlist_tree, bg=BG_INPUT, fg=FG_BRIGHT,
                         insertbackground=FG_BRIGHT, font=("Consolas", 9),
                         relief="flat", highlightthickness=1, highlightcolor=RED)
        entry.place(x=x, y=y, width=w, height=h)

        current_name = self.mgr.store.playlists[pid]["name"]
        entry.insert(0, current_name)
        entry.select_range(0, "end")
        entry.focus_set()

        def commit(event=None):
            new_name = entry.get().strip()
            entry.destroy()
            self._rename_entry = None
            if not new_name or new_name == current_name:
                return
            new_pid = playlist_id(new_name)
            if new_pid != pid and new_pid in self.mgr.store.playlists:
                messagebox.showerror("Error", f"Playlist '{new_name}' already exists.")
                return
            pl = self.mgr.store.playlists[pid]
            old_folder = pl["folder"]
            new_folder = sanitize(new_name)

            # Rename folder on disk first — abort if it fails
            if old_folder != new_folder:
                try:
                    self.mgr.writer.rename(old_folder, new_folder)
                except Exception as e:
                    messagebox.showerror("Rename failed",
                                         f"Could not rename folder on device:\n{e}")
                    return

            pl["name"] = new_name
            pl["folder"] = new_folder
            if new_pid != pid:
                from .config import rename_backup_pid
                self.mgr.store.playlists[new_pid] = pl
                del self.mgr.store.playlists[pid]
                self.current_pid = new_pid
                rename_backup_pid(self.mgr.writer.root, pid, new_pid, new_folder)
            self._invalidate_caches(new_pid if new_pid != pid else pid)
            self.mgr.store.save()
            self._refresh_playlists()
            self._update_status()

        def cancel(event=None):
            entry.destroy()
            self._rename_entry = None

        entry.bind("<Return>", commit)
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", commit)
        self._rename_entry = entry

    def _delete_playlist(self):
        if self._syncing:
            return
        if not self.current_pid:
            return
        pl = self.mgr.store.playlists.get(self.current_pid)
        if not pl:
            return
        if not messagebox.askyesno("Delete playlist",
                                   f"Delete playlist '{pl['name']}'?\n\n"
                                   "This will remove the playlist folder and all the\n"
                                   "tracks that were copied into it.\n"
                                   "Your original music files are never touched.\n\n"
                                   "A restore point will be saved so you can bring it back."):
            return
        pid = self.current_pid
        name = pl["name"]
        self.current_pid = None

        def delete():
            try:
                self.mgr.backup_playlist_metadata(pid)
            except Exception:
                pass
            try:
                self.mgr.writer.delete(pl["folder"])
            except Exception:
                pass
            del self.mgr.store.playlists[pid]
            self.mgr.store.save()

        def on_done(_result):
            self._refresh_playlists()
            self._update_status()

        self._run_playlist_op(pid, name, delete, on_done)

    # TODO: star prefix feature disabled — re-enable when stable
    # def _toggle_star_prefix(self):
    #     enabled = self._star_var.get()
    #     self._star_cb.config(state="disabled", fg=FG_DIM)
    #     self.mgr.set_star_prefix(enabled)
    #     self._refresh_playlists()
    #     self._update_status()
    #     self._poll_star_rename()

    # def _poll_star_rename(self):
    #     thread = getattr(self.mgr, "_star_rename_thread", None)
    #     if thread and thread.is_alive():
    #         self.root.after(50, self._poll_star_rename)
    #     else:
    #         self._star_cb.config(state="normal", fg=FG)

    # ── Tracks ──

    def _show_offloaded_tracks(self, pid: str):
        """Display tracks from a backup as grey read-only list for offloaded playlists."""
        self.track_tree.delete(*self.track_tree.get_children())
        self._track_data = []
        self._removed_data = []
        from .config import list_backups, load_backup
        backups = list_backups(self.mgr.writer.root, pid)
        if not backups:
            self.track_tree.insert("", "end", values=("", "(offloaded — no backup found)", ""),
                                   tags=("offloaded_track",))
            return
        data = load_backup(self.mgr.writer.root, pid, backups[0]["timestamp"])
        if not data:
            return
        for entry in data.get("tracks", []):
            tags_data = entry.get("tags", {})
            title = tags_data.get("title", entry.get("copy_name", ""))
            artist = tags_data.get("artist", "")
            idx = entry.get("index", "")
            self.track_tree.insert("", "end", values=(idx, title, artist),
                                   tags=("offloaded_track",))

    def _refresh_tracks(self):
        self.track_tree.delete(*self.track_tree.get_children())
        if not self.current_pid or self.current_pid not in self.mgr.store.playlists:
            self._track_data = []
            self._removed_data = []
            return
        pid = self.current_pid
        playlist = self.mgr.store.playlists[pid]
        folder = playlist["folder"]

        # Check if all committed track tags are cached — if so, skip the thread
        committed = [t for t in playlist["tracks"]
                     if t.get("copy_name")]
        all_cached = all(
            f"{folder}/{t['copy_name']}" in self._tag_cache
            for t in committed
        )

        self._tracks_gen += 1
        gen = self._tracks_gen

        if all_cached:
            self._load_tracks_sync(pid, gen)
        else:
            self._tracks_loading = True
            self.track_tree.insert("", "end", iid="_loading",
                                   values=("", "Loading...", ""), tags=("loading",))
            Thread(target=self._load_tracks_bg, args=(pid, gen), daemon=True).start()

    def _build_track_data(self, pid: str):
        """Build track/removed data lists. Called from any thread."""
        self.mgr.rescan_playlist(pid)
        playlist = self.mgr.store.playlists.get(pid)
        if not playlist:
            return [], []
        active, removed = self.staging.virtual_tracks(pid, playlist["tracks"])

        track_data = []
        for t in active:
            if t.get("_pending"):
                title = t.get("title", "")
                artist = t.get("artist", "")
            else:
                title, artist = self._read_track_tags(
                    playlist["folder"], t.get("copy_name", ""), t.get("src_path", "")
                )
            track_data.append({
                "index": t["index"],
                "title": title,
                "artist": artist,
                "pending": t.get("_pending", False),
                "copy_name": t.get("copy_name", ""),
                "src_path": t.get("src_path", ""),
                "key": t.get("_key", ""),
            })

        removed_data = []
        for t in removed:
            title, artist = self._read_track_tags(
                playlist["folder"], t.get("copy_name", ""), t.get("src_path", "")
            )
            removed_data.append({
                "index": t["index"],
                "title": title,
                "artist": artist,
                "copy_name": t.get("copy_name", ""),
                "key": t.get("_key", ""),
            })

        return track_data, removed_data

    def _load_tracks_sync(self, pid: str, gen: int):
        track_data, removed_data = self._build_track_data(pid)
        if self._tracks_gen != gen:
            return
        self._track_data = track_data
        self._removed_data = removed_data
        self._display_tracks()

    def _load_tracks_bg(self, pid: str, gen: int):
        try:
            track_data, removed_data = self._build_track_data(pid)
        except Exception:
            self._tracks_loading = False
            return
        if not self._alive or self._tracks_gen != gen:
            self._tracks_loading = False
            return

        def _apply():
            self._tracks_loading = False
            if self._tracks_gen != gen:
                return
            self._track_data = track_data
            self._removed_data = removed_data
            self._display_tracks()

        self._schedule_callback(_apply)

    def _display_tracks(self):
        self.track_tree.delete(*self.track_tree.get_children())
        for row in self._track_data:
            tag = ("pending",) if row["pending"] else ()
            prefix = "~ " if row["pending"] else ""
            self.track_tree.insert("", "end", values=(row["index"], prefix + row["title"], row["artist"]),
                                    tags=tag)
        for row in self._removed_data:
            self.track_tree.insert("", "end",
                                    values=(row["index"], row["title"], row["artist"]),
                                    tags=("removed",))

    def _sort_tracks(self, col):
        if self._sort_col == col:
            if self._sort_reverse:
                self._sort_col = None
                self._sort_reverse = False
                self._track_data.sort(key=lambda r: r["index"])
                self._display_tracks()
                for c in ("index", "title", "artist"):
                    label = {"index": "#", "title": "TITLE", "artist": "ARTIST"}[c]
                    self.track_tree.heading(c, text=label)
                return
            else:
                self._sort_reverse = True
        else:
            self._sort_col = col
            self._sort_reverse = False

        def sort_key(row):
            val = row[col]
            if col == "index":
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return 0
            return str(val).lower()

        self._track_data.sort(key=sort_key, reverse=self._sort_reverse)
        self._display_tracks()

        arrow = " ▼" if self._sort_reverse else " ▲"
        for c in ("index", "title", "artist"):
            label = {"index": "#", "title": "TITLE", "artist": "ARTIST"}[c]
            self.track_tree.heading(c, text=label + (arrow if c == col else ""))

    # ── Track reorder drag ──

    def _trk_drag_start(self, event):
        if self._current_playlist_offloaded():
            self._trk_drag_iid = None
            return
        iid = self.track_tree.identify_row(event.y)
        region = self.track_tree.identify_region(event.x, event.y)
        if region == "heading":
            self._trk_drag_iid = None
            return
        self._trk_drag_iid = iid
        self._trk_drag_origin_y = event.y

    def _trk_drag_motion(self, event):
        if not self._trk_drag_iid:
            return
        if abs(event.y - self._trk_drag_origin_y) < 5:
            return
        target = self.track_tree.identify_row(event.y)
        if target and target != self._trk_drag_iid:
            try:
                bbox = self.track_tree.bbox(target)
            except Exception:
                return
            if not bbox:
                return
            mid = bbox[1] + bbox[3] // 2
            if event.y < mid:
                pos = self.track_tree.index(target)
            else:
                pos = self.track_tree.index(target) + 1
            self.track_tree.move(self._trk_drag_iid, "", pos)

    def _trk_drag_end(self, event):
        if not self._trk_drag_iid:
            return
        self._trk_drag_iid = None

        # Read new order from tree, rebuild _track_data and save reorder
        all_iids = self.track_tree.get_children()
        if not all_iids or not self._track_data or not self.current_pid:
            return

        # Build a map from displayed values to track_data row
        old_data = list(self._track_data)

        # Map tree row positions to old _track_data entries by matching display values
        # Since display may have been sorted, we match by the current tree content
        old_by_display = {}
        for row in old_data:
            prefix = "~ " if row["pending"] else ""
            disp_key = (str(row["index"]), prefix + row["title"], row["artist"])
            old_by_display[disp_key] = row

        new_order = []
        for iid in all_iids:
            vals = self.track_tree.item(iid, "values")
            disp_key = (str(vals[0]), str(vals[1]), str(vals[2]))
            if disp_key in old_by_display:
                new_order.append(old_by_display[disp_key])

        if len(new_order) != len(old_data):
            return

        # Check if order actually changed
        if all(n["key"] == o["key"] for n, o in zip(new_order, old_data)):
            return

        # Renumber indices
        for i, row in enumerate(new_order, 1):
            row["index"] = i

        self._track_data = new_order
        self._display_tracks()

        # Save reorder to staging
        reorder_list = [{"key": row["key"]} for row in new_order]
        self.staging.set_reorder(self.current_pid, reorder_list)

        self._undo_stack.append({
            "type": "reorder",
            "pid": self.current_pid,
            "desc": f"Reorder tracks in {self.current_pid}",
        })
        self._update_status()

    def _read_track_tags(self, folder: str, copy_name: str, src_path: str = "") -> tuple[str, str]:
        if copy_name:
            cache_key = f"{folder}/{copy_name}"
            cached = self._tag_cache.get(cache_key)
            if cached is not None:
                return cached

            try:
                import mutagen
                path = self.mgr.writer.root / folder / copy_name
                m = mutagen.File(path, easy=True)
                if m:
                    title = m.get("title", [""])[0]
                    artist = m.get("artist", [""])[0]
                    if title or artist:
                        self._tag_cache[cache_key] = (title, artist)
                        return title, artist
            except Exception:
                pass

        if src_path:
            source_root = Path(self.mgr.config.source_root)
            src_full = _resolve_source_file(src_path, source_root)
            if src_full:
                result = _read_tags_from_file(src_full)
                if copy_name:
                    self._tag_cache[f"{folder}/{copy_name}"] = result
                return result

        return copy_name or "", ""

    def _remove_track(self, event=None):
        if self._syncing or self._current_playlist_offloaded():
            return
        if not self.current_pid:
            return
        selected = self.track_tree.selection()
        if not selected:
            return

        all_iids = self.track_tree.get_children()
        selected_rows = []
        for iid in selected:
            row_idx = all_iids.index(iid)
            if row_idx < len(self._track_data):
                selected_rows.append(self._track_data[row_idx])

        pending_keys = []
        committed_keys = []
        for row in selected_rows:
            if row["pending"]:
                pending_keys.append(row["key"])
            else:
                committed_keys.append(row["key"])

        if pending_keys:
            pending_offsets = set()
            for k in pending_keys:
                # key format is "p:N"
                pending_offsets.add(int(k.split(":")[1]))
            indices_to_pop = []
            count = 0
            for i, a in enumerate(self.staging.pending_adds):
                if a["pid"] == self.current_pid:
                    if count in pending_offsets:
                        indices_to_pop.append(i)
                    count += 1
            for i in sorted(indices_to_pop, reverse=True):
                self.staging.pending_adds.pop(i)
            self.staging.save()
            self._undo_stack.append({
                "type": "unstage_add",
                "desc": f"Remove {len(indices_to_pop)} pending track(s)",
            })

        if committed_keys:
            playlist = self.mgr.store.playlists[self.current_pid]
            orphan_count = 0
            for k in committed_keys:
                orig_idx = int(k.split(":")[1])
                for t in playlist["tracks"]:
                    if t["index"] == orig_idx and not t.get("src_path"):
                        orphan_count += 1
            if orphan_count:
                proceed = messagebox.askyesno(
                    "Track has no source",
                    f"{orphan_count} selected track(s) have no source file "
                    f"(e.g. from an adopted folder).\n\n"
                    f"Removing them is permanent — they can't be re-added "
                    f"without the original file.\n\n"
                    f"Continue?",
                )
                if not proceed:
                    return
            for k in committed_keys:
                orig_idx = int(k.split(":")[1])
                copy_name = ""
                for t in playlist["tracks"]:
                    if t["index"] == orig_idx:
                        copy_name = t["copy_name"]
                        break
                self.staging.stage_remove(self.current_pid, orig_idx, copy_name)
            self._undo_stack.append({
                "type": "remove",
                "count": len(committed_keys),
                "desc": f"Remove {len(committed_keys)} track(s) from {self.current_pid}",
            })

        # Clear reorder for this playlist since indices changed
        if self.current_pid in self.staging.pending_reorders:
            del self.staging.pending_reorders[self.current_pid]
            self.staging.save()

        self._refresh_tracks()
        self._update_status()

    def _add_files_dialog(self):
        folder = filedialog.askdirectory(
            title="Add folder to source browser",
            initialdir=self.source,
        )
        if not folder:
            return
        folder_path = Path(folder)
        display = folder_path.name + "/"
        iid = self.source_tree.insert("", "end", text=display, values=(str(folder_path),))
        self.source_tree.insert(iid, "end", text="...")

    # ── Status ──

    def _refresh_expensive_stats(self):
        if self._stats_pending:
            return
        self._stats_pending = True

        def worker():
            try:
                dt, wb = self.mgr.compute_expensive_stats()
            except Exception:
                dt, wb = 0, 0
            if self._alive:
                self._schedule_callback(lambda: self._on_expensive_stats(dt, wb))

        t = Thread(target=worker, daemon=True)
        t.start()
        self._stats_thread = t

    def _on_expensive_stats(self, device_tracks, workspace_bytes):
        self._cached_device_tracks = device_tracks
        self._cached_workspace_bytes = workspace_bytes
        self._stats_pending = False
        self._update_status()

    def _update_status(self):
        try:
            s = self.mgr.stats(
                cached_device_tracks=self._cached_device_tracks,
                cached_workspace_bytes=self._cached_workspace_bytes,
            )
        except Exception:
            return

        self.lbl_playlists.config(text=f"PLAYLISTS: {s['playlists']}")
        self.lbl_workspace.config(text=f"{s['workspace_bytes']:,} bytes")

        pending_delta = len(self.staging.pending_adds) - len(self.staging.pending_removes)
        virtual_count = s.get("device_tracks", 0) + pending_delta
        track_pct = round(virtual_count / MAX_TRACKS * 100, 1)
        self.lbl_tracks.config(text=f"TRACKS: {virtual_count} / {MAX_TRACKS}")
        self.track_pct_lbl.config(text=f"{track_pct}%")
        self.track_bar["value"] = track_pct
        self._color_bar(self.track_bar, track_pct)
        self.lbl_tracks.config(fg=RED_BRIGHT if track_pct >= 90 else FG)

        drive_pct = s["drive_used_pct"]
        self.lbl_drive.config(text=f"DRIVE: {drive_pct}%")
        self.drive_bar["value"] = drive_pct
        self._color_bar(self.drive_bar, drive_pct)
        self.lbl_drive.config(fg=RED_BRIGHT if drive_pct >= 90 else FG)

        if self.staging.has_pending:
            n = self.staging.total_ops
            self.pending_lbl.config(text=f"{n} pending change{'s' if n != 1 else ''}")
        else:
            self.pending_lbl.config(text="")

        if self._undo_stack:
            self.undo_btn.state(["!disabled"])
            self.undo_lbl.config(text=self._undo_stack[-1]["desc"])
        else:
            self.undo_btn.state(["disabled"])
            self.undo_lbl.config(text="")

    def _color_bar(self, bar, pct):
        if pct >= 90:
            bar.configure(style="Red.Horizontal.TProgressbar")
        elif pct >= 70:
            bar.configure(style="Yellow.Horizontal.TProgressbar")
        else:
            bar.configure(style="Green.Horizontal.TProgressbar")


def run():
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    app = App()
    app.run()


if __name__ == "__main__":
    run()
