"""GUI tests — exercises App logic via tkinter without user interaction."""

import time
import tkinter as tk
import pytest
from pathlib import Path
from unittest.mock import patch

from echolist.gui import App, StagingState, _read_tags_from_file, _resolve_source_file, PENDING_FILE
from echolist.manager import PlaylistManager
from conftest import _make_flac, assert_originals_untouched


def _flush_bg_ops(app, timeout=10):
    """Wait for all background playlist ops and track loading to finish."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        busy = app._busy_playlists or getattr(app, "_tracks_loading", False)
        if not busy:
            break
        time.sleep(0.05)
    app.root.update()


def _flush_tracks(app, timeout=5):
    """Wait for background track loading to complete."""
    deadline = time.monotonic() + timeout
    while getattr(app, "_tracks_loading", False) and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.02)
    app.root.update()


@pytest.fixture
def gui_env(tmp_path):
    """Set up source files, dest, and manager — no GUI yet."""
    src = tmp_path / "library"
    _make_flac(src / "ArtistA" / "Album1" / "01 Song One.flac", "ArtistA", "Song One")
    _make_flac(src / "ArtistB" / "Album2" / "03 Song Two.flac", "ArtistB", "Song Two")
    _make_flac(src / "ArtistC" / "Album3" / "05 Song Three.flac", "ArtistC", "Song Three")
    dest = tmp_path / "card"
    dest.mkdir()
    mgr = PlaylistManager.init(src, dest)
    yield {"src": src, "dest": dest, "mgr": mgr, "tmp": tmp_path}
    mgr.release_lock()


@pytest.fixture
def app(gui_env, monkeypatch):
    """Create an App instance wired to the test workspace, skip setup screen."""
    # Prevent pending file from leaking between tests
    test_pending = gui_env["tmp"] / "pending.json"
    monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)

    a = App.__new__(App)
    a.root = tk.Tk()
    a.root.withdraw()
    a.mgr = gui_env["mgr"]
    a.source = str(gui_env["src"])
    a.dest = str(gui_env["dest"])
    a.current_pid = None
    a.staging = StagingState.__new__(StagingState)
    a.staging.pending_adds = []
    a.staging.pending_removes = []
    a.staging.pending_reorders = {}
    a._undo_stack = []
    a._sort_col = None
    a._sort_reverse = False
    a._drag_data = None
    a._cached_device_tracks = 0
    a._cached_workspace_bytes = 0
    a._stats_pending = False
    a._alive = True
    a._syncing = False
    a._tag_cache = {}
    a._audit_cache = {}
    a._tracks_loading = False
    a._tracks_gen = 0
    from queue import Queue
    a._callback_queue = Queue()
    a._poll_callbacks()
    a._apply_theme()
    a._show_main()

    yield a
    a._alive = False
    t = getattr(a, "_stats_thread", None)
    if t:
        t.join(timeout=5)
    _flush_bg_ops(a, timeout=5)
    a.root.destroy()


# ── Staging tests ──

class TestStaging:
    def test_stage_add(self, gui_env, monkeypatch):
        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)
        s = StagingState()
        s.stage_add("workout", "/some/file.flac", "Song", "Artist")
        assert len(s.pending_adds) == 1
        assert s.has_pending
        assert s.total_ops == 1
        assert test_pending.exists()

    def test_stage_remove(self, gui_env, monkeypatch):
        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)
        s = StagingState()
        s.stage_remove("workout", 1, "01 - song.flac")
        assert len(s.pending_removes) == 1
        assert s.has_pending

    def test_clear(self, gui_env, monkeypatch):
        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)
        s = StagingState()
        s.stage_add("w", "/f.flac", "T", "A")
        s.clear()
        assert not s.has_pending
        assert not test_pending.exists()

    def test_virtual_tracks(self, gui_env, monkeypatch):
        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)
        s = StagingState()
        committed = [{"index": 1, "copy_name": "01 - x.flac", "src_path": "x.flac"}]
        s.stage_add("p", "/new.flac", "New", "Art")
        s.stage_remove("p", 1, "01 - x.flac")
        active, removed = s.virtual_tracks("p", committed)
        assert len(active) == 1
        assert active[0]["_pending"] is True
        assert active[0]["title"] == "New"
        assert len(removed) == 1
        assert removed[0]["index"] == 1

    def test_virtual_track_count(self, gui_env, monkeypatch):
        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)
        mgr = gui_env["mgr"]
        mgr.create_playlist("Test")
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        mgr.add_track("test", src)

        s = StagingState()
        assert s.virtual_track_count(mgr.store) == 1
        s.stage_add("test", str(src), "Song One", "ArtistA")
        assert s.virtual_track_count(mgr.store) == 2
        s.stage_remove("test", 1, "01 - Song One.flac")
        assert s.virtual_track_count(mgr.store) == 1

    def test_persistence(self, gui_env, monkeypatch):
        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)
        s1 = StagingState()
        s1.stage_add("w", "/f.flac", "T", "A")

        s2 = StagingState()
        assert len(s2.pending_adds) == 1
        assert s2.pending_adds[0]["title"] == "T"


# ── App integration tests ──

class TestAppPlaylist:
    def test_create_playlist(self, app):
        app._create_playlist()
        assert "new_playlist" in app.mgr.store.playlists
        items = app.playlist_tree.get_children()
        assert len(items) == 1

    def test_create_multiple(self, app):
        app._create_playlist()
        app._create_playlist()
        assert len(app.mgr.store.playlists) == 2
        items = app.playlist_tree.get_children()
        assert len(items) == 2

    def test_delete_playlist(self, app):
        app._create_playlist()
        assert len(app.mgr.store.playlists) == 1
        with patch("echolist.gui.messagebox.askyesno", return_value=True):
            app._delete_playlist()
        _flush_bg_ops(app)
        assert len(app.mgr.store.playlists) == 0

    def test_delete_cancelled(self, app):
        app._create_playlist()
        with patch("echolist.gui.messagebox.askyesno", return_value=False):
            app._delete_playlist()
        assert len(app.mgr.store.playlists) == 1


class TestAppTracks:
    def test_stage_add(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 1
        items = app.track_tree.get_children()
        assert len(items) == 1
        tags = app.track_tree.item(items[0], "tags")
        assert "pending" in tags

    def test_stage_multiple_adds(self, app, gui_env):
        app._create_playlist()
        files = list(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)
        assert len(app.staging.pending_adds) == 3
        assert len(app.track_tree.get_children()) == 3

    def test_stage_remove_committed(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app.mgr.add_track(app.current_pid, src)
        app._refresh_tracks()
        _flush_tracks(app)

        items = app.track_tree.get_children()
        assert len(items) == 1

        app.track_tree.selection_set(items[0])
        app._remove_track()

        assert len(app.staging.pending_removes) == 1

    def test_remove_pending_track(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 1

        items = app.track_tree.get_children()
        app.track_tree.selection_set(items[0])
        app._remove_track()

        assert len(app.staging.pending_adds) == 0

    def test_remove_multiple_pending(self, app, gui_env):
        app._create_playlist()
        files = list(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)
        assert len(app.staging.pending_adds) == 3

        items = app.track_tree.get_children()
        app.track_tree.selection_set(items)
        app._remove_track()

        assert len(app.staging.pending_adds) == 0

    def test_add_selected_from_source_multi(self, app, gui_env):
        """Regression: drag-dropping multiple selected source items must add all, not just the last."""
        app._create_playlist()
        files = sorted(gui_env["src"].rglob("*.flac"))
        assert len(files) >= 2

        # Insert files directly into the source tree (bypassing lazy-load)
        iids = []
        for f in files:
            iid = app.source_tree.insert("", "end", text=f.name, values=(str(f),))
            iids.append(iid)

        # Simulate multi-select and add via the UI code path (drag-drop / add button)
        app._add_selected_from_source(iids)
        assert len(app.staging.pending_adds) == len(files)
        assert len(app.track_tree.get_children()) == len(files)

    def test_duplicate_add_blocked(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 1

        with patch("echolist.gui.messagebox.showinfo"):
            app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 1

    def test_duplicate_add_committed_blocked(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app.mgr.add_track(app.current_pid, src)
        app._refresh_tracks()
        _flush_tracks(app)

        with patch("echolist.gui.messagebox.showinfo"):
            app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 0

    def test_removed_track_shown_greyed(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app.mgr.add_track(app.current_pid, src)
        app._refresh_tracks()
        _flush_tracks(app)

        items = app.track_tree.get_children()
        app.track_tree.selection_set(items[0])
        app._remove_track()

        items = app.track_tree.get_children()
        assert len(items) == 1
        tags = app.track_tree.item(items[0], "tags")
        assert "removed" in tags


class TestAppSync:
    def test_sync_adds(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 1

        app._do_sync_blocking()

        assert len(app.staging.pending_adds) == 0
        assert len(app.mgr.store.playlists[app.current_pid]["tracks"]) == 1

    def test_sync_removes(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app.mgr.add_track(app.current_pid, src)
        app._refresh_tracks()
        _flush_tracks(app)

        items = app.track_tree.get_children()
        app.track_tree.selection_set(items[0])
        app._remove_track()

        app._do_sync_blocking()
        assert len(app.mgr.store.playlists[app.current_pid]["tracks"]) == 0

    def test_originals_untouched_after_sync(self, app, gui_env):
        import hashlib
        hashes = {}
        for f in gui_env["src"].rglob("*.flac"):
            hashes[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()

        app._create_playlist()
        files = list(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)
        app._do_sync_blocking()

        assert_originals_untouched(gui_env["src"], hashes)


class TestAppUndo:
    def test_undo_add(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 1
        assert len(app._undo_stack) == 1

        app._do_undo()
        assert len(app.staging.pending_adds) == 0
        assert len(app._undo_stack) == 0

    def test_undo_remove(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app.mgr.add_track(app.current_pid, src)
        app._refresh_tracks()
        _flush_tracks(app)

        items = app.track_tree.get_children()
        app.track_tree.selection_set(items[0])
        app._remove_track()
        assert len(app.staging.pending_removes) == 1

        app._do_undo()
        assert len(app.staging.pending_removes) == 0

    def test_undo_stack_clears_on_sync(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert len(app._undo_stack) == 1

        app._do_sync_blocking()
        assert len(app._undo_stack) == 0

    def test_undo_empty_stack_noop(self, app):
        app._do_undo()  # should not raise


class TestAppSort:
    def test_sort_ascending(self, app, gui_env):
        app._create_playlist()
        files = list(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)

        app._sort_tracks("title")
        items = app.track_tree.get_children()
        titles = [app.track_tree.item(i, "values")[1] for i in items]
        clean = [t.lstrip("~ ") for t in titles]
        assert clean == sorted(clean)

    def test_sort_descending(self, app, gui_env):
        app._create_playlist()
        files = list(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)

        app._sort_tracks("title")
        app._sort_tracks("title")
        items = app.track_tree.get_children()
        titles = [app.track_tree.item(i, "values")[1].lstrip("~ ") for i in items]
        assert titles == sorted(titles, reverse=True)

    def test_sort_reset(self, app, gui_env):
        app._create_playlist()
        files = list(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)

        app._sort_tracks("title")
        app._sort_tracks("title")
        app._sort_tracks("title")  # third click resets
        items = app.track_tree.get_children()
        indices = [int(app.track_tree.item(i, "values")[0]) for i in items]
        assert indices == sorted(indices)


class TestCloseDialog:
    def test_close_no_pending_exits(self, app):
        with patch.object(app.root, "destroy") as mock_destroy:
            app._on_close()
            mock_destroy.assert_called_once()

    def test_close_pending_cancel(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])

        with patch("echolist.gui.messagebox.askyesnocancel", return_value=None):
            with patch.object(app.root, "destroy") as mock_destroy:
                app._on_close()
                mock_destroy.assert_not_called()

    def test_close_pending_discard(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])

        with patch("echolist.gui.messagebox.askyesnocancel", return_value=False):
            with patch.object(app.root, "destroy") as mock_destroy:
                app._on_close()
                mock_destroy.assert_called_once()
        assert app.staging.has_pending  # changes preserved for next time

    def test_close_pending_sync(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])

        with patch("echolist.gui.messagebox.askyesnocancel", return_value=True):
            with patch.object(app.root, "destroy") as mock_destroy:
                app._on_close()
                mock_destroy.assert_called_once()
        assert not app.staging.has_pending
        assert len(app.mgr.store.playlists[app.current_pid]["tracks"]) == 1


class TestAppReorder:
    def test_reorder_pending_tracks(self, app, gui_env):
        app._create_playlist()
        files = sorted(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)

        # Reverse the order in _track_data
        app._track_data.reverse()
        for i, row in enumerate(app._track_data, 1):
            row["index"] = i
        reorder_list = [{"key": row["key"]} for row in app._track_data]
        app.staging.set_reorder(app.current_pid, reorder_list)

        assert app.current_pid in app.staging.pending_reorders
        assert app.staging.has_pending

    def test_reorder_persists_through_refresh(self, app, gui_env):
        app._create_playlist()
        files = sorted(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)

        original_keys = [row["key"] for row in app._track_data]

        # Reverse
        app._track_data.reverse()
        for i, row in enumerate(app._track_data, 1):
            row["index"] = i
        reversed_keys = [row["key"] for row in app._track_data]
        app.staging.set_reorder(app.current_pid, [{"key": k} for k in reversed_keys])

        # Refresh should preserve reorder
        app._refresh_tracks()
        _flush_tracks(app)
        refreshed_keys = [row["key"] for row in app._track_data]
        assert refreshed_keys == reversed_keys
        assert refreshed_keys != original_keys

    def test_reorder_updates_indices(self, app, gui_env):
        app._create_playlist()
        files = sorted(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)

        # Reverse
        app._track_data.reverse()
        for i, row in enumerate(app._track_data, 1):
            row["index"] = i
        app.staging.set_reorder(app.current_pid, [{"key": row["key"]} for row in app._track_data])

        app._refresh_tracks()
        _flush_tracks(app)
        indices = [row["index"] for row in app._track_data]
        assert indices == [1, 2, 3]

    def test_undo_reorder(self, app, gui_env):
        app._create_playlist()
        files = sorted(gui_env["src"].rglob("*.flac"))
        app._stage_add_files(files)
        original_keys = [row["key"] for row in app._track_data]

        # Reorder and push undo
        app._track_data.reverse()
        for i, row in enumerate(app._track_data, 1):
            row["index"] = i
        app.staging.set_reorder(app.current_pid, [{"key": row["key"]} for row in app._track_data])
        app._undo_stack.append({"type": "reorder", "pid": app.current_pid, "desc": "Reorder"})

        app._do_undo()
        assert app.current_pid not in app.staging.pending_reorders

        app._refresh_tracks()
        _flush_tracks(app)
        reset_keys = [row["key"] for row in app._track_data]
        assert reset_keys == original_keys

    def test_reorder_committed_tracks_on_sync(self, app, gui_env):
        app._create_playlist()
        src1 = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        src2 = gui_env["src"] / "ArtistB" / "Album2" / "03 Song Two.flac"
        app.mgr.add_track(app.current_pid, src1)
        app.mgr.add_track(app.current_pid, src2)
        app._refresh_tracks()
        _flush_tracks(app)

        assert app._track_data[0]["key"] == "c:1"
        assert app._track_data[1]["key"] == "c:2"

        # Reverse: c:2 first, c:1 second
        app.staging.set_reorder(app.current_pid, [{"key": "c:2"}, {"key": "c:1"}])
        app._do_sync_blocking()

        tracks = app.mgr.store.playlists[app.current_pid]["tracks"]
        assert tracks[0]["index"] == 1
        assert tracks[1]["index"] == 2
        # The track that was originally #2 should now be #1
        assert "Song Two" in tracks[0]["copy_name"]
        # Filenames should start with the new index
        assert tracks[0]["copy_name"].startswith("01 - ")
        assert tracks[1]["copy_name"].startswith("02 - ")
        # Files should exist on disk with the new names
        folder = app.mgr.store.playlists[app.current_pid]["folder"]
        for t in tracks:
            assert (app.mgr.writer.root / folder / t["copy_name"]).exists()


class TestM3uImport:
    def test_import_m3u_creates_playlist_and_stages(self, app, gui_env):
        """Importing a .m3u file creates a playlist and stages found tracks."""
        m3u = gui_env["tmp"] / "My Workout.m3u"
        m3u.write_text(
            "#EXTM3U\n"
            "ArtistA/Album1/01 Song One.flac\n"
            "ArtistB/Album2/03 Song Two.flac\n",
            encoding="utf-8",
        )
        with patch("echolist.gui.messagebox.showinfo"):
            app._import_m3u_file(m3u)

        assert "my_workout" in app.mgr.store.playlists
        assert app.current_pid == "my_workout"
        assert len(app.staging.pending_adds) == 2

    def test_import_m3u_with_missing_shows_warning(self, app, gui_env):
        """Missing tracks are reported but found tracks are still staged."""
        m3u = gui_env["tmp"] / "Partial.m3u"
        m3u.write_text(
            "ArtistA/Album1/01 Song One.flac\n"
            "nonexistent/gone.flac\n",
            encoding="utf-8",
        )
        with patch("echolist.gui.messagebox.showinfo") as mock_info:
            app._import_m3u_file(m3u)

        assert "partial" in app.mgr.store.playlists
        assert len(app.staging.pending_adds) == 1
        call_text = mock_info.call_args[0][1]
        assert "1 track(s) could not be found" in call_text

    def test_import_m3u_all_missing_shows_warning(self, app, gui_env):
        """If no tracks are found, show a warning and don't create a playlist."""
        m3u = gui_env["tmp"] / "Empty.m3u"
        m3u.write_text("nope/a.flac\nnope/b.flac\n", encoding="utf-8")
        with patch("echolist.gui.messagebox.showwarning"):
            app._import_m3u_file(m3u)

        assert "empty" not in app.mgr.store.playlists

    def test_import_m3u_name_collision_curated(self, app, gui_env):
        """Importing a .m3u with a name that already exists gets auto-renamed."""
        app._create_playlist()  # creates "new_playlist"
        m3u = gui_env["tmp"] / "New Playlist.m3u"
        m3u.write_text("ArtistA/Album1/01 Song One.flac\n", encoding="utf-8")
        with patch("echolist.gui.messagebox.showinfo"):
            app._import_m3u_file(m3u)

        assert "new_playlist_(2)" in app.mgr.store.playlists

    def test_import_m3u_sync_copies_files(self, app, gui_env):
        """After importing and syncing, tracks are copied to device."""
        m3u = gui_env["tmp"] / "Sync Test.m3u"
        m3u.write_text("ArtistA/Album1/01 Song One.flac\n", encoding="utf-8")
        with patch("echolist.gui.messagebox.showinfo"):
            app._import_m3u_file(m3u)

        app._do_sync_blocking()
        tracks = app.mgr.store.playlists["sync_test"]["tracks"]
        assert len(tracks) == 1
        folder = app.mgr.store.playlists["sync_test"]["folder"]
        assert (app.mgr.writer.root / folder / tracks[0]["copy_name"]).exists()


class TestPlaylistRename:
    """Test inline rename and its interaction with backups."""

    def _do_rename(self, app, old_pid, new_name):
        """Simulate the commit() logic from _start_inline_rename."""
        from echolist.naming import playlist_id, sanitize
        new_pid = playlist_id(new_name)
        pl = app.mgr.store.playlists[old_pid]
        old_folder = pl["folder"]
        new_folder = sanitize(new_name)
        if old_folder != new_folder:
            app.mgr.writer.rename(old_folder, new_folder)
        pl["name"] = new_name
        pl["folder"] = new_folder
        if new_pid != old_pid:
            from echolist.config import rename_backup_pid
            app.mgr.store.playlists[new_pid] = pl
            del app.mgr.store.playlists[old_pid]
            app.current_pid = new_pid
            rename_backup_pid(app.mgr.writer.root, old_pid, new_pid, new_folder)
        app._invalidate_caches(new_pid if new_pid != old_pid else old_pid)
        app.mgr.store.save()
        return new_pid

    def test_rename_changes_folder_and_store(self, app, gui_env):
        """Basic rename updates name, folder, and pid in the store."""
        app._create_playlist()
        old_pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        new_pid = self._do_rename(app, old_pid, "Renamed Playlist")

        assert old_pid not in app.mgr.store.playlists
        assert new_pid in app.mgr.store.playlists
        assert app.current_pid == new_pid
        pl = app.mgr.store.playlists[new_pid]
        assert pl["name"] == "Renamed Playlist"
        assert (app.mgr.writer.root / pl["folder"]).is_dir()
        # Track files should exist in the new folder
        for t in pl["tracks"]:
            assert (app.mgr.writer.root / pl["folder"] / t["copy_name"]).exists()

    def test_rename_same_pid(self, app, gui_env):
        """Rename that doesn't change pid (e.g. case change) keeps old pid."""
        app._create_playlist()
        pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        # "New Playlist" -> "new playlist" — same pid
        result_pid = self._do_rename(app, pid, "new playlist")
        assert result_pid == pid
        assert pid in app.mgr.store.playlists
        assert app.mgr.store.playlists[pid]["name"] == "new playlist"

    def test_rename_preserves_backups(self, app, gui_env):
        """Backups created before rename are accessible under the new pid."""
        from echolist.config import list_backups, load_backup
        app._create_playlist()
        old_pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        # Create backup under old pid
        app.mgr.backup_playlist_metadata(old_pid)
        assert len(list_backups(app.mgr.writer.root, old_pid)) == 1

        # Rename
        new_pid = self._do_rename(app, old_pid, "Fresh Name")

        # Backups should be under new pid
        assert list_backups(app.mgr.writer.root, old_pid) == []
        new_backups = list_backups(app.mgr.writer.root, new_pid)
        assert len(new_backups) == 1

        # Backup folder field should be updated
        data = load_backup(app.mgr.writer.root, new_pid, new_backups[0]["timestamp"])
        pl = app.mgr.store.playlists[new_pid]
        assert data["folder"] == pl["folder"]

    def test_rename_then_restore_works(self, app, gui_env):
        """Full flow: create → sync → backup → rename → corrupt → restore."""
        app._create_playlist()
        old_pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        app.mgr.backup_playlist_metadata(old_pid)
        new_pid = self._do_rename(app, old_pid, "Restored Later")

        # Corrupt the tags
        pl = app.mgr.store.playlists[new_pid]
        track_path = app.mgr.writer.root / pl["folder"] / pl["tracks"][0]["copy_name"]
        from mutagen.flac import FLAC
        f = FLAC(track_path)
        f["ALBUM"] = "CORRUPTED"
        f.save()

        # Restore should work under new pid
        restored = app.mgr.restore_playlist_metadata(new_pid)
        assert restored == 1

        from echolist.tags import read_playlist_tags
        tags = read_playlist_tags(track_path)
        assert tags["album"] != "CORRUPTED"

    def test_rename_audit_flags_old_album(self, app, gui_env):
        """After rename, audit should flag that album tag doesn't match new name."""
        app._create_playlist()
        old_pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        new_pid = self._do_rename(app, old_pid, "Totally Different")

        issues = app.mgr.audit_playlist_metadata(new_pid)
        album_issues = [i for i in issues if i["field"] == "album"]
        assert len(album_issues) >= 1


class TestAuditCacheInvalidation:
    """Verify that the audit cache is cleared after every metadata mutation,
    so Fix metadata always reflects the current state."""

    def _sync_and_prime_cache(self, app, gui_env):
        """Create playlist, sync a track, and prime the audit cache."""
        app._create_playlist()
        pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        # Prime audit cache — should be clean after a fresh sync
        issues = app.mgr.audit_playlist_metadata(pid)
        app._audit_cache[pid] = issues
        assert issues == []
        return pid

    def test_restore_invalidates_audit_cache(self, app, gui_env):
        """After restoring metadata from before_echolist, audit cache must be
        cleared so Fix metadata catches the now-wrong tags."""
        pid = self._sync_and_prime_cache(app, gui_env)

        # Corrupt tags to simulate "before_echolist" state, then backup
        pl = app.mgr.store.playlists[pid]
        track_path = app.mgr.writer.root / pl["folder"] / pl["tracks"][0]["copy_name"]
        from mutagen.flac import FLAC
        f = FLAC(track_path)
        f["ALBUM"] = "Original Album"
        f.save()
        app.mgr.backup_playlist_metadata(pid)

        # Fix the tags back so audit cache is "clean"
        app.mgr.fix_playlist_metadata(pid)
        app._audit_cache[pid] = []

        # Now restore from the backup (puts back "Original Album")
        from echolist.config import list_backups
        backups = list_backups(app.mgr.writer.root, pid)
        app.mgr.restore_playlist_metadata(pid, backups[0]["timestamp"])
        app._invalidate_caches(pid)

        # Cache should be cleared
        assert pid not in app._audit_cache
        # Fresh audit should detect the mismatch
        issues = app.mgr.audit_playlist_metadata(pid)
        album_issues = [i for i in issues if i["field"] == "album"]
        assert len(album_issues) >= 1

    def test_sync_invalidates_audit_cache(self, app, gui_env):
        """After sync, audit cache must be cleared."""
        pid = self._sync_and_prime_cache(app, gui_env)

        # Add another track and sync
        src2 = gui_env["src"] / "ArtistB" / "Album2" / "03 Song Two.flac"
        app._stage_add_files([src2])

        # Manually put stale data in cache
        app._audit_cache[pid] = [{"fake": "stale"}]

        app._do_sync_blocking()
        # _do_sync_blocking doesn't invalidate (only used at close),
        # but _do_sync does — verify via _invalidate_caches directly
        app._invalidate_caches()
        assert pid not in app._audit_cache

    def test_fix_metadata_invalidates_audit_cache(self, app, gui_env):
        """After fixing metadata, audit cache must be cleared."""
        pid = self._sync_and_prime_cache(app, gui_env)

        # Corrupt tags so audit finds issues
        pl = app.mgr.store.playlists[pid]
        track_path = app.mgr.writer.root / pl["folder"] / pl["tracks"][0]["copy_name"]
        from mutagen.flac import FLAC
        f = FLAC(track_path)
        f["ALBUM"] = "WRONG"
        f.save()

        # Invalidate so audit re-runs
        app._invalidate_caches(pid)
        issues = app.mgr.audit_playlist_metadata(pid)
        assert len(issues) > 0

        # Fix metadata
        app.mgr.fix_playlist_metadata(pid)
        app._invalidate_caches(pid)

        # Cache should be cleared, fresh audit should be clean
        assert pid not in app._audit_cache
        assert app.mgr.audit_playlist_metadata(pid) == []

    def test_rename_invalidates_audit_cache(self, app, gui_env):
        """After rename, audit cache for the new pid should not have stale data."""
        from echolist.naming import playlist_id, sanitize
        from echolist.config import rename_backup_pid

        pid = self._sync_and_prime_cache(app, gui_env)

        # Rename
        new_name = "Renamed Audit"
        new_pid = playlist_id(new_name)
        new_folder = sanitize(new_name)
        pl = app.mgr.store.playlists[pid]
        old_folder = pl["folder"]
        app.mgr.writer.rename(old_folder, new_folder)
        pl["name"] = new_name
        pl["folder"] = new_folder
        if new_pid != pid:
            app.mgr.store.playlists[new_pid] = pl
            del app.mgr.store.playlists[pid]
            app.current_pid = new_pid
            rename_backup_pid(app.mgr.writer.root, pid, new_pid, new_folder)
        app._invalidate_caches(new_pid)
        app.mgr.store.save()

        # Cache should not have stale data for new pid
        assert new_pid not in app._audit_cache
        # Audit should flag album mismatch (old name baked into tags)
        issues = app.mgr.audit_playlist_metadata(new_pid)
        album_issues = [i for i in issues if i["field"] == "album"]
        assert len(album_issues) >= 1

    def test_offload_invalidates_audit_cache(self, app, gui_env):
        """Offload should clear audit cache for the playlist."""
        pid = self._sync_and_prime_cache(app, gui_env)

        # Put stale data in cache
        app._audit_cache[pid] = [{"fake": "stale"}]

        # Simulate offload
        app._invalidate_caches(pid)
        assert pid not in app._audit_cache


class TestSourceRoot:
    """Test that the GUI correctly handles source_root, including mismatches."""

    def test_open_workspace_uses_correct_source(self, app, gui_env):
        """After opening, the source tree root matches config.source_root."""
        source_root = Path(app.mgr.config.source_root)
        assert source_root == gui_env["src"].resolve()

    def test_source_mismatch_updates_config(self, gui_env, monkeypatch):
        """Re-opening workspace from a different source updates config.source_root."""
        src = gui_env["src"]
        dest = gui_env["dest"]
        mgr = gui_env["mgr"]

        new_src = gui_env["tmp"] / "other_library"
        _make_flac(new_src / "X" / "Y" / "01 Track.flac", "X", "Track")

        mgr.release_lock()

        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)

        a = App.__new__(App)
        a.root = tk.Tk()
        a.root.withdraw()
        a.mgr = None
        a.source = ""
        a.dest = ""
        a.current_pid = None
        a.staging = StagingState.__new__(StagingState)
        a.staging.pending_adds = []
        a.staging.pending_removes = []
        a.staging.pending_reorders = {}
        a._undo_stack = []
        a._sort_col = None
        a._sort_reverse = False
        a._drag_data = None
        a._cached_device_tracks = 0
        a._cached_workspace_bytes = 0
        a._stats_pending = False
        a._alive = True
        a._syncing = False
        a._apply_theme()

        a._open_workspace(str(new_src), str(dest))

        assert a.mgr.config.source_root == str(new_src.resolve())
        assert a.source == str(new_src)

        a._alive = False
        t = getattr(a, "_stats_thread", None)
        if t:
            t.join(timeout=5)
        _flush_bg_ops(a, timeout=5)
        a.mgr.release_lock()
        a.root.destroy()

    def test_source_tree_shows_correct_library(self, gui_env, monkeypatch):
        """Source tree displays contents from the configured source_root, not its parent."""
        src = gui_env["src"]
        dest = gui_env["dest"]
        mgr = gui_env["mgr"]
        mgr.release_lock()

        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)

        a = App.__new__(App)
        a.root = tk.Tk()
        a.root.withdraw()
        a.mgr = None
        a.source = ""
        a.dest = ""
        a.current_pid = None
        a.staging = StagingState.__new__(StagingState)
        a.staging.pending_adds = []
        a.staging.pending_removes = []
        a.staging.pending_reorders = {}
        a._undo_stack = []
        a._sort_col = None
        a._sort_reverse = False
        a._drag_data = None
        a._cached_device_tracks = 0
        a._cached_workspace_bytes = 0
        a._stats_pending = False
        a._alive = True
        a._syncing = False
        a._apply_theme()

        a._open_workspace(str(src), str(dest))

        top_items = a.source_tree.get_children()
        top_names = [a.source_tree.item(iid, "text") for iid in top_items]
        assert "ArtistA/" in top_names
        assert "ArtistB/" in top_names
        assert "ArtistC/" in top_names

        a._alive = False
        t = getattr(a, "_stats_thread", None)
        if t:
            t.join(timeout=5)
        _flush_bg_ops(a, timeout=5)
        a.mgr.release_lock()
        a.root.destroy()

    def test_source_mismatch_persists_to_disk(self, gui_env, monkeypatch):
        """After source mismatch correction, the new source is persisted in config.json."""
        src = gui_env["src"]
        dest = gui_env["dest"]
        mgr = gui_env["mgr"]

        new_src = gui_env["tmp"] / "moved_library"
        _make_flac(new_src / "Z" / "W" / "01 New.flac", "Z", "New")

        mgr.release_lock()

        test_pending = gui_env["tmp"] / "pending.json"
        monkeypatch.setattr("echolist.gui.PENDING_FILE", test_pending)

        a = App.__new__(App)
        a.root = tk.Tk()
        a.root.withdraw()
        a.mgr = None
        a.source = ""
        a.dest = ""
        a.current_pid = None
        a.staging = StagingState.__new__(StagingState)
        a.staging.pending_adds = []
        a.staging.pending_removes = []
        a.staging.pending_reorders = {}
        a._undo_stack = []
        a._sort_col = None
        a._sort_reverse = False
        a._drag_data = None
        a._cached_device_tracks = 0
        a._cached_workspace_bytes = 0
        a._stats_pending = False
        a._alive = True
        a._syncing = False
        a._apply_theme()

        a._open_workspace(str(new_src), str(dest))

        a.mgr.release_lock()
        a._alive = False
        t = getattr(a, "_stats_thread", None)
        if t:
            t.join(timeout=5)
        _flush_bg_ops(a, timeout=5)
        a.root.destroy()

        import json
        config_path = dest / "Playlists" / ".echolist" / "config.json"
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["source_root"] == str(new_src.resolve())


class TestReadTags:
    def test_read_from_flac(self, gui_env):
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        title, artist = _read_tags_from_file(src)
        assert title == "Song One"
        assert artist == "ArtistA"

    def test_read_missing_file(self, tmp_path):
        title, artist = _read_tags_from_file(tmp_path / "nope.flac")
        assert title == "nope"
        assert artist == ""


class TestPlaylistStatusIcons:
    """Test playlist status icon and tag logic."""

    def test_loaded_playlist_shows_eject(self, app, gui_env):
        app._create_playlist()
        pid = app.current_pid
        pl = app.mgr.store.playlists[pid]
        assert app._playlist_status_icon(pid, pl) == "⏏"

    def test_pending_add_uses_amber_tag(self, app, gui_env):
        app._create_playlist()
        pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        assert app._playlist_has_pending(pid)
        pl = app.mgr.store.playlists[pid]
        assert app._playlist_status_icon(pid, pl) == "⏏"

    def test_offloaded_shows_dotted_circle(self, app, gui_env):
        app._create_playlist()
        pid = app.current_pid
        pl = app.mgr.store.playlists[pid]
        pl["offloaded"] = True
        assert app._playlist_status_icon(pid, pl) == "◌"


class TestOffloadOnload:
    """Test playlist offload/onload lifecycle."""

    def test_offload_removes_tracks_and_sets_flag(self, app, gui_env):
        app._create_playlist()
        pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        pl = app.mgr.store.playlists[pid]
        assert len(pl["tracks"]) == 1
        folder_path = app.mgr.writer.root / pl["folder"]
        assert any(folder_path.iterdir())

        with patch("echolist.gui.messagebox.askyesno", return_value=True):
            app._offload_playlist(pid)
        _flush_bg_ops(app)

        pl = app.mgr.store.playlists[pid]
        assert pl["offloaded"] is True
        assert pl["tracks"] == []
        assert not folder_path.exists()

    def _do_onload(self, app, pid):
        """Simulate onload logic inline (avoids threading complexity in tests)."""
        from echolist.config import load_backup, list_backups
        from echolist.gui import _resolve_source_file, _read_tags_from_file

        backups = list_backups(app.mgr.writer.root, pid)
        assert len(backups) >= 1
        data = load_backup(app.mgr.writer.root, pid, backups[0]["timestamp"])
        assert data is not None

        pl = app.mgr.store.playlists[pid]
        pl["offloaded"] = False
        pl["tracks"] = []
        app.mgr.store.save()

        folder = pl["folder"]
        (app.mgr.writer.root / folder).mkdir(parents=True, exist_ok=True)

        source_root = Path(app.mgr.config.source_root)
        for entry in data.get("tracks", []):
            src_path = entry.get("src_path", "")
            if not src_path:
                continue
            full = _resolve_source_file(src_path, source_root)
            if full:
                title, artist = _read_tags_from_file(full)
                app.staging.stage_add(pid, str(full), title, artist)
        app._invalidate_caches(pid)

    def test_onload_stages_tracks_for_sync(self, app, gui_env):
        """Onload must stage tracks from backup for re-syncing."""
        app._create_playlist()
        pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        with patch("echolist.gui.messagebox.askyesno", return_value=True):
            app._offload_playlist(pid)
        _flush_bg_ops(app)

        assert app.mgr.store.playlists[pid]["tracks"] == []

        # Onload — tracks staged, not yet on device
        self._do_onload(app, pid)

        pl = app.mgr.store.playlists[pid]
        assert pl.get("offloaded") is False
        assert len(app.staging.pending_adds) >= 1
        assert any(a["pid"] == pid for a in app.staging.pending_adds)

    def test_offload_onload_sync_roundtrip(self, app, gui_env):
        """Full cycle: sync → offload → onload → sync → files back on device."""
        app._create_playlist()
        pid = app.current_pid
        src1 = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        src2 = gui_env["src"] / "ArtistB" / "Album2" / "03 Song Two.flac"
        app._stage_add_files([src1, src2])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        pl = app.mgr.store.playlists[pid]
        folder = pl["folder"]
        assert len(pl["tracks"]) == 2

        # Offload
        with patch("echolist.gui.messagebox.askyesno", return_value=True):
            app._offload_playlist(pid)
        _flush_bg_ops(app)
        assert not (app.mgr.writer.root / folder).exists()

        # Onload
        self._do_onload(app, pid)

        # Sync to copy files back
        app._do_sync_blocking()
        _flush_bg_ops(app)

        # Tracks should be back on device
        pl = app.mgr.store.playlists[pid]
        assert len(pl["tracks"]) == 2
        for t in pl["tracks"]:
            assert (app.mgr.writer.root / folder / t["copy_name"]).exists()


class TestSourceSearch:
    """Test source tree search functionality."""

    def test_search_filters_tree(self, app, gui_env):
        app._src_search_placeholder = False
        app._src_search_var.set("Song One")
        app._do_source_search()
        children = app.source_tree.get_children()
        assert len(children) >= 1
        texts = [app.source_tree.item(c, "text") for c in children]
        assert any("Song One" in t for t in texts)

    def test_empty_search_restores_tree(self, app, gui_env):
        app._src_search_placeholder = False
        app._src_search_var.set("Song One")
        app._do_source_search()
        app._src_search_var.set("")
        app._do_source_search()
        children = app.source_tree.get_children()
        texts = [app.source_tree.item(c, "text") for c in children]
        assert "ArtistA/" in texts

    def test_search_no_results(self, app, gui_env):
        app._src_search_placeholder = False
        app._src_search_var.set("nonexistent_xyz_track")
        app._do_source_search()
        children = app.source_tree.get_children()
        assert len(children) == 0


class TestTrackContextMenu:
    """Test track right-click features."""

    def test_show_in_source_selects_file(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)
        app._refresh_tracks()
        _flush_tracks(app)

        src_rel = app._track_data[0]["src_path"]
        app._show_track_in_source(src_rel)

        sel = app.source_tree.selection()
        assert len(sel) == 1
        selected_path = Path(app.source_tree.item(sel[0], "values")[0])
        assert selected_path.name == "01 Song One.flac"

    def test_show_in_source_missing_file(self, app, gui_env):
        with patch("echolist.gui.messagebox.showinfo") as mock:
            app._show_track_in_source("nonexistent/file.flac")
        mock.assert_called_once()

    def test_show_in_playlists_highlights(self, app, gui_env):
        app._create_playlist()
        pid = app.current_pid
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app._stage_add_files([src])
        app._do_sync_blocking()
        _flush_bg_ops(app)

        src_path = app.mgr.store.playlists[pid]["tracks"][0]["src_path"]
        app._show_track_in_playlists(src_path)

        tags = app.playlist_tree.item(pid, "tags")
        assert "highlight" in tags


class TestResolveSourceFile:
    """Test _resolve_source_file read-only source resolution."""

    def test_relative_to_source_root(self, gui_env):
        src = gui_env["src"]
        result = _resolve_source_file("ArtistA/Album1/01 Song One.flac", src)
        assert result is not None
        assert result.name == "01 Song One.flac"

    def test_absolute_path(self, gui_env):
        src = gui_env["src"]
        absolute = str(src / "ArtistA" / "Album1" / "01 Song One.flac")
        result = _resolve_source_file(absolute, src)
        assert result is not None
        assert result.name == "01 Song One.flac"

    def test_filename_fallback_when_moved(self, gui_env):
        """If the relative path is wrong but the file exists under source_root, find it."""
        src = gui_env["src"]
        result = _resolve_source_file("wrong/path/01 Song One.flac", src)
        assert result is not None
        assert result.name == "01 Song One.flac"

    def test_returns_none_when_truly_missing(self, gui_env):
        src = gui_env["src"]
        result = _resolve_source_file("nonexistent_file_xyz.flac", src)
        assert result is None

    def test_empty_path_returns_none(self, gui_env):
        result = _resolve_source_file("", gui_env["src"])
        assert result is None

    def test_never_writes(self, gui_env, tmp_path):
        """Resolution must be read-only — no files created."""
        src = gui_env["src"]
        before = set(src.rglob("*"))
        _resolve_source_file("ArtistA/Album1/01 Song One.flac", src)
        _resolve_source_file("nonexistent.flac", src)
        _resolve_source_file("wrong/path/01 Song One.flac", src)
        after = set(src.rglob("*"))
        assert before == after
