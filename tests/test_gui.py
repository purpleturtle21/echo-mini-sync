"""GUI tests — exercises App logic via tkinter without user interaction."""

import time
import tkinter as tk
import pytest
from pathlib import Path
from unittest.mock import patch

from echolist.gui import App, StagingState, _read_tags_from_file, PENDING_FILE
from echolist.manager import PlaylistManager
from conftest import _make_flac, assert_originals_untouched


def _flush_bg_ops(app, timeout=10):
    """Wait for all background playlist ops to finish and process callbacks."""
    deadline = time.monotonic() + timeout
    while app._busy_playlists and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)
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

        with patch("echolist.gui.messagebox.showinfo"):
            app._stage_add_files([src])
        assert len(app.staging.pending_adds) == 0

    def test_removed_track_shown_greyed(self, app, gui_env):
        app._create_playlist()
        src = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        app.mgr.add_track(app.current_pid, src)
        app._refresh_tracks()

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
        reset_keys = [row["key"] for row in app._track_data]
        assert reset_keys == original_keys

    def test_reorder_committed_tracks_on_sync(self, app, gui_env):
        app._create_playlist()
        src1 = gui_env["src"] / "ArtistA" / "Album1" / "01 Song One.flac"
        src2 = gui_env["src"] / "ArtistB" / "Album2" / "03 Song Two.flac"
        app.mgr.add_track(app.current_pid, src1)
        app.mgr.add_track(app.current_pid, src2)
        app._refresh_tracks()

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
