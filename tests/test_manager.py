"""M3 tests — operations + originals-untouched assertion."""

import threading

import pytest
from pathlib import Path
from mutagen.flac import FLAC

from echolist.manager import PlaylistManager
from echolist.safe_write import UnsafeWriteError
from conftest import assert_originals_untouched


## TODO: star prefix feature disabled — re-enable when stable
# def _wait_star_rename(manager, timeout=10):
#     thread = getattr(manager, "_star_rename_thread", None)
#     if thread:
#         thread.join(timeout=timeout)
#         assert not thread.is_alive(), "star rename thread did not finish in time"


def test_create_playlist(manager):
    pid = manager.create_playlist("Workout")
    assert pid == "workout"
    assert (manager.writer.root / "Workout").is_dir()
    assert "workout" in manager.store.playlists


def test_create_duplicate_raises(manager):
    manager.create_playlist("Workout")
    with pytest.raises(ValueError):
        manager.create_playlist("Workout")


def test_add_track(manager, source):
    pid = manager.create_playlist("Workout")
    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    rel = manager.add_track(pid, src)

    copy_path = manager.writer.root / rel
    assert copy_path.exists()

    f = FLAC(copy_path)
    assert f["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f["ALBUM"] == ["Workout"]
    assert f["TRACKNUMBER"] == ["1"]
    assert f["ECHOLIST_ROLE"] == ["playlist-copy"]
    assert f["ARTIST"] == ["ArtistA"]
    assert f["TITLE"] == ["Song One"]


def test_add_two_tracks(manager, source):
    pid = manager.create_playlist("Road Trip")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    rel1 = manager.add_track(pid, src1)
    rel2 = manager.add_track(pid, src2)

    assert "01 - " in rel1
    assert "02 - " in rel2

    f1 = FLAC(manager.writer.root / rel1)
    f2 = FLAC(manager.writer.root / rel2)
    assert f1["TRACKNUMBER"] == ["1"]
    assert f2["TRACKNUMBER"] == ["2"]


def test_originals_untouched(manager, source, hashes):
    pid = manager.create_playlist("Test")
    for flac in sorted(source.rglob("*.flac")):
        manager.add_track(pid, flac)
    manager.remove_track(pid, 1)
    assert_originals_untouched(source, hashes)


def test_remove_track(manager, source):
    pid = manager.create_playlist("Mix")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    rel1 = manager.add_track(pid, src1)
    rel2 = manager.add_track(pid, src2)

    manager.remove_track(pid, 1)

    assert not (manager.writer.root / rel1).exists()
    tracks = manager.store.playlists[pid]["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["index"] == 1
    # Track was renumbered: 02 -> 01
    assert tracks[0]["copy_name"].startswith("01 - ")
    assert (manager.writer.root / "Mix" / tracks[0]["copy_name"]).exists()


def test_stats(manager, source):
    pid = manager.create_playlist("S")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    device_tracks, workspace_bytes = manager.compute_expensive_stats()
    s = manager.stats(cached_device_tracks=device_tracks, cached_workspace_bytes=workspace_bytes)
    assert s["playlists"] == 1
    assert s["tracks"] == 1
    assert s["workspace_bytes"] > 0


def test_overlap_source_inside_workspace(tmp_path):
    """Source inside workspace must be rejected — workspace could overwrite originals."""
    workspace_parent = tmp_path / "card"
    workspace_parent.mkdir()
    src = workspace_parent / "Playlists" / "sneaky"
    src.mkdir(parents=True)
    with pytest.raises(UnsafeWriteError):
        PlaylistManager.init(src, workspace_parent)


def test_same_dir_overlap(tmp_path):
    d = tmp_path / "same"
    d.mkdir()
    with pytest.raises(UnsafeWriteError):
        PlaylistManager.init(d / "Playlists", d)


def test_workspace_inside_source_allowed(tmp_path):
    """Common case: source=/sd_card, dest=/sd_card — workspace is /sd_card/Playlists."""
    src = tmp_path / "sd_card"
    src.mkdir()
    mgr = PlaylistManager.init(src, src)
    assert mgr.writer.root == (src / "Playlists").resolve()


# ── Star prefix tests ──

# TODO: star prefix feature disabled — re-enable when stable
# def test_star_prefix_creates_starred_folder(manager):
#     manager.config.star_prefix = True
#     pid = manager.create_playlist("Chill")
#     folder = manager.store.playlists[pid]["folder"]
#     assert folder.startswith("★ ")
#     assert (manager.writer.root / folder).is_dir()
#
#
# def test_star_prefix_toggle_renames_folders(manager, source):
#     pid = manager.create_playlist("Workout")
#     manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
#     assert (manager.writer.root / "Workout").is_dir()
#
#     manager.set_star_prefix(True)
#     _wait_star_rename(manager)
#     assert manager.store.playlists[pid]["folder"] == "★ Workout"
#     assert (manager.writer.root / "★ Workout").is_dir()
#     assert not (manager.writer.root / "Workout").exists()
#
#     manager.set_star_prefix(False)
#     _wait_star_rename(manager)
#     assert manager.store.playlists[pid]["folder"] == "Workout"
#     assert (manager.writer.root / "Workout").is_dir()
#     assert not (manager.writer.root / "★ Workout").exists()
#
#
# def test_star_prefix_no_double_star(manager):
#     manager.config.star_prefix = True
#     pid = manager.create_playlist("Mix")
#     manager.set_star_prefix(True)
#     _wait_star_rename(manager)
#     folder = manager.store.playlists[pid]["folder"]
#     assert not folder.startswith("★ ★ ")


# ── Rescan from drive tests ──

def test_rescan_detects_manual_reorder(manager, source):
    pid = manager.create_playlist("Mix")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    manager.add_track(pid, src1)
    manager.add_track(pid, src2)

    folder = manager.writer.root / "Mix"
    old_track1 = manager.store.playlists[pid]["tracks"][0]["copy_name"]
    old_track2 = manager.store.playlists[pid]["tracks"][1]["copy_name"]
    new_name_for_track1 = old_track1.replace("01 - ", "02 - ")
    new_name_for_track2 = old_track2.replace("02 - ", "01 - ")

    # 3-step swap to avoid collisions on Windows
    tmp = folder / "_swap.flac"
    (folder / old_track1).rename(tmp)
    (folder / old_track2).rename(folder / new_name_for_track2)
    tmp.rename(folder / new_name_for_track1)

    changed = manager.rescan_playlist(pid)
    assert changed
    tracks = manager.store.playlists[pid]["tracks"]
    assert tracks[0]["copy_name"] == new_name_for_track2
    assert tracks[1]["copy_name"] == new_name_for_track1
    assert tracks[0]["index"] == 1
    assert tracks[1]["index"] == 2


def test_rescan_detects_deleted_file(manager, source):
    pid = manager.create_playlist("Mix")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    src2 = source / "ArtistB" / "Album2" / "03 Song Two.flac"
    manager.add_track(pid, src1)
    manager.add_track(pid, src2)

    folder = manager.writer.root / "Mix"
    track1_name = manager.store.playlists[pid]["tracks"][0]["copy_name"]
    (folder / track1_name).unlink()

    changed = manager.rescan_playlist(pid)
    assert changed
    tracks = manager.store.playlists[pid]["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["index"] == 1


def test_rescan_no_change_returns_false(manager, source):
    pid = manager.create_playlist("Mix")
    src1 = source / "ArtistA" / "Album1" / "01 Song One.flac"
    manager.add_track(pid, src1)

    changed = manager.rescan_playlist(pid)
    assert not changed


# ── Metadata audit tests ──

def test_audit_clean_playlist_has_no_issues(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    issues = manager.audit_playlist_metadata(pid)
    assert issues == []


def test_audit_detects_wrong_albumartist(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]
    f = FLAC(track_path)
    f["ALBUMARTIST"] = "Wrong Artist"
    f.save()

    issues = manager.audit_playlist_metadata(pid)
    albumartist_issues = [i for i in issues if i["field"] == "albumartist"]
    assert len(albumartist_issues) == 1
    assert albumartist_issues[0]["expected"] == "* PLAYLISTS *"
    assert albumartist_issues[0]["actual"] == "Wrong Artist"


def test_audit_detects_wrong_album(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]
    f = FLAC(track_path)
    f["ALBUM"] = "Some Other Album"
    f.save()

    issues = manager.audit_playlist_metadata(pid)
    album_issues = [i for i in issues if i["field"] == "album"]
    assert len(album_issues) == 1
    assert album_issues[0]["expected"] == "Workout"
    assert album_issues[0]["actual"] == "Some Other Album"


def test_audit_detects_wrong_tracknumber(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]
    f = FLAC(track_path)
    f["TRACKNUMBER"] = "99"
    f.save()

    issues = manager.audit_playlist_metadata(pid)
    tn_issues = [i for i in issues if i["field"] == "tracknumber"]
    assert len(tn_issues) == 1
    assert tn_issues[0]["expected"] == "1"
    assert tn_issues[0]["actual"] == "99"


def test_fix_metadata_corrects_issues(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]
    f = FLAC(track_path)
    f["ALBUMARTIST"] = "Wrong"
    f["ALBUM"] = "Wrong Album"
    f.save()

    fixed = manager.fix_playlist_metadata(pid)
    assert fixed == 1

    f2 = FLAC(track_path)
    assert f2["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f2["ALBUM"] == ["Workout"]

    assert manager.audit_playlist_metadata(pid) == []


def test_fix_creates_backup(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]
    f = FLAC(track_path)
    f["ALBUMARTIST"] = "Original Artist Node"
    f["ALBUM"] = "Original Album"
    f.save()

    manager.fix_playlist_metadata(pid)
    assert manager.has_metadata_backup(pid)

    backups = manager.list_metadata_backups(pid)
    assert len(backups) == 1

    from echolist.config import load_backup
    data = load_backup(manager.writer.root, pid, backups[0]["timestamp"])
    assert data["playlist_name"] == "Workout"
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["tags"]["albumartist"] == "Original Artist Node"
    assert data["tracks"][0]["tags"]["album"] == "Original Album"


def test_restore_metadata_from_backup(manager, source):
    """Restoring uses tags (state at backup time), not original_tags."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]

    manager.backup_playlist_metadata(pid)

    f = FLAC(track_path)
    f["ALBUMARTIST"] = "Manually Changed"
    f.save()

    restored = manager.restore_playlist_metadata(pid)
    assert restored == 1

    f2 = FLAC(track_path)
    assert f2["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f2["ALBUM"] == ["Workout"]


def test_restore_returns_zero_without_backup(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    assert manager.restore_playlist_metadata(pid) == 0


def test_fix_no_issues_returns_zero(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    assert manager.fix_playlist_metadata(pid) == 0


# ── External import detection ──

def test_check_external_import_false_after_playlist_created(manager):
    """After creating a playlist, .echolist exists so external import is False."""
    manager.create_playlist("Test")
    assert not manager.check_external_import()


def test_check_external_import_true(tmp_path):
    """Simulates a Playlists folder without .echolist marker."""
    from echolist.safe_write import SafeWriter
    from echolist.config import Config
    from echolist.store import Store

    dest = tmp_path / "card"
    dest.mkdir()
    playlists = dest / "Playlists"
    playlists.mkdir()

    writer = SafeWriter(playlists)
    config = Config(source_root=str(tmp_path / "library"))
    store = Store(writer, {"schema": 1, "playlists": {}})
    mgr = PlaylistManager(writer, config, store)
    assert mgr.check_external_import()


def test_manually_added_track_detected_and_fixed(manager, source):
    """A track manually copied into a playlist folder gets detected and fixed."""
    import shutil
    pid = manager.create_playlist("Workout")

    src = source / "ArtistA" / "Album1" / "01 Song One.flac"
    folder_path = manager.writer.root / "Workout"
    manual_copy = folder_path / "01 - Song One.flac"
    shutil.copy2(src, manual_copy)

    manager.rescan_playlist(pid)

    issues = manager.audit_playlist_metadata(pid)
    assert len(issues) > 0
    albumartist_issues = [i for i in issues if i["field"] == "albumartist"]
    assert any(i["actual"] != "* PLAYLISTS *" for i in albumartist_issues)

    fixed = manager.fix_playlist_metadata(pid)
    assert fixed >= 1

    f = FLAC(manual_copy)
    assert f["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f["ALBUM"] == ["Workout"]


# ── Playlist snapshot tests ──

def test_save_snapshot(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    path = manager.save_snapshot()
    assert path.exists()

    from echolist.config import load_playlist_snapshot
    snap = load_playlist_snapshot(manager.writer.root)
    assert snap is not None
    assert "workout" in snap["store"]["playlists"]
    assert snap["config"]["node_name"] == "* PLAYLISTS *"
    tracks = snap["store"]["playlists"]["workout"]["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["src_path"]


def test_find_snapshot_after_save(manager, source, dest):
    pid = manager.create_playlist("Mix")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.save_snapshot()

    snap = PlaylistManager.find_snapshot(dest)
    assert snap is not None
    assert "mix" in snap["store"]["playlists"]


def test_find_snapshot_returns_none_without_save(dest):
    snap = PlaylistManager.find_snapshot(dest)
    assert snap is None


def test_snapshot_preserves_multiple_playlists(manager, source):
    manager.create_playlist("Workout")
    manager.add_track("workout", source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.create_playlist("Chill")
    manager.add_track("chill", source / "ArtistB" / "Album2" / "03 Song Two.flac")
    manager.save_snapshot()

    from echolist.config import load_playlist_snapshot
    snap = load_playlist_snapshot(manager.writer.root)
    assert len(snap["store"]["playlists"]) == 2
    assert "workout" in snap["store"]["playlists"]
    assert "chill" in snap["store"]["playlists"]


def test_snapshot_restore_creates_playlists(manager, source, dest):
    """After snapshot + wipe, re-init from snapshot recreates playlist structure."""
    import shutil
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.save_snapshot()

    # Wipe the device
    shutil.rmtree(manager.writer.root)

    # Find snapshot and re-init
    snap = PlaylistManager.find_snapshot(dest)
    assert snap is not None

    snap_config = snap["config"]
    mgr2 = PlaylistManager.init(
        snap_config["source_root"], dest,
        node_name=snap_config.get("node_name", "* PLAYLISTS *"),
    )

    # Re-create playlists from snapshot
    for pid, pl in snap["store"]["playlists"].items():
        mgr2.create_playlist(pl["name"])

    assert "workout" in mgr2.store.playlists
    assert mgr2.store.playlists["workout"]["name"] == "Workout"

    # Re-add tracks from source paths
    source_root = Path(snap_config["source_root"])
    for pid, pl in snap["store"]["playlists"].items():
        for t in pl["tracks"]:
            src = source_root / t["src_path"]
            if src.exists():
                mgr2.add_track(pid, src)

    assert len(mgr2.store.playlists["workout"]["tracks"]) == 1


# ── Deleted playlist restore tests ──

def test_backup_includes_source_info(manager, source):
    """Backup stores src_path and src_hash alongside tags."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")

    from echolist.config import load_backup
    data = load_backup(manager.writer.root, pid, "20260101_120000")
    entry = data["tracks"][0]
    assert entry["src_path"]
    assert entry["src_hash"]
    assert entry["index"] == 1
    assert "tags" in entry


def test_backup_dedup_skips_identical(manager, source):
    """If nothing changed, a second backup should not create a new file."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    p1 = manager.backup_playlist_metadata(pid, timestamp="20260101_120000")
    p2 = manager.backup_playlist_metadata(pid, timestamp="20260101_120100")

    assert p1 == p2
    from echolist.config import list_backups
    backups = list_backups(manager.writer.root, pid)
    assert len(backups) == 1
    assert backups[0]["timestamp"] == "20260101_120000"


def test_backup_dedup_saves_when_changed(manager, source):
    """After a metadata change, a new backup should be created."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    p1 = manager.backup_playlist_metadata(pid, timestamp="20260101_120000")

    track_path = manager.writer.root / "Workout" / manager.store.playlists[pid]["tracks"][0]["copy_name"]
    f = FLAC(track_path)
    f["ALBUMARTIST"] = "Changed"
    f.save()

    p2 = manager.backup_playlist_metadata(pid, timestamp="20260101_120100")
    assert p1 != p2

    from echolist.config import list_backups
    backups = list_backups(manager.writer.root, pid)
    assert len(backups) == 2


def test_list_deleted_playlists_empty(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    assert manager.list_deleted_playlists() == []


def test_list_deleted_playlists_after_delete(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid)

    manager.writer.delete("Workout")
    del manager.store.playlists[pid]
    manager.store.save()

    deleted = manager.list_deleted_playlists()
    assert len(deleted) == 1
    assert deleted[0]["pid"] == "workout"
    assert deleted[0]["name"] == "Workout"


def test_restore_deleted_playlist(manager, source):
    """Delete a playlist and restore it — should recreate and return sources for staging."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid)

    manager.writer.delete("Workout")
    del manager.store.playlists[pid]
    manager.store.save()

    sources = manager.restore_deleted_playlist(pid)
    assert pid in manager.store.playlists
    assert manager.store.playlists[pid]["name"] == "Workout"
    assert len(sources) == 1
    assert sources[0]["src_path"]


def test_restore_deleted_playlist_stages_for_sync(manager, source):
    """After restoring, the source paths can be used to re-add tracks."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid)

    manager.writer.delete("Workout")
    del manager.store.playlists[pid]
    manager.store.save()

    sources = manager.restore_deleted_playlist(pid)
    source_root = Path(manager.config.source_root)
    for s in sources:
        full = source_root / s["src_path"]
        assert full.exists()
        manager.add_track(pid, full)

    assert len(manager.store.playlists[pid]["tracks"]) == 1


def test_restore_deleted_raises_if_exists(manager, source):
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid)

    with pytest.raises(ValueError, match="already exists"):
        manager.restore_deleted_playlist(pid)


def test_restore_deleted_raises_without_backup(manager):
    with pytest.raises(KeyError, match="no backups"):
        manager.restore_deleted_playlist("nonexistent")


# ── Restore to point tests ──

def test_restore_to_point_removes_added_tracks(manager, source):
    """Tracks added after the restore point should be removed."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_100000")

    manager.add_track(pid, source / "ArtistB" / "Album2" / "03 Song Two.flac")
    assert len(manager.store.playlists[pid]["tracks"]) == 2

    result = manager.restore_playlist_to_point(pid, "20260101_100000")
    assert result["removed"] == 1
    assert result["restored"] == 1
    assert len(result["to_stage"]) == 0
    assert len(manager.store.playlists[pid]["tracks"]) == 1


def test_restore_to_point_restages_deleted_tracks(manager, source):
    """Tracks deleted after the restore point should be re-staged."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.add_track(pid, source / "ArtistB" / "Album2" / "03 Song Two.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_100000")

    manager.remove_track(pid, 2)
    assert len(manager.store.playlists[pid]["tracks"]) == 1

    result = manager.restore_playlist_to_point(pid, "20260101_100000")
    assert len(result["to_stage"]) == 1
    assert result["to_stage"][0]["src_path"]
    assert result["restored"] == 1


def test_restore_to_point_creates_safety_backup(manager, source):
    """Restoring to a point should first create a backup of the current state."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_100000")

    manager.add_track(pid, source / "ArtistB" / "Album2" / "03 Song Two.flac")

    backups_before = len(manager.list_metadata_backups(pid))
    manager.restore_playlist_to_point(pid, "20260101_100000")
    backups_after = len(manager.list_metadata_backups(pid))
    assert backups_after == backups_before + 1


def test_restore_to_point_no_change(manager, source):
    """If playlist hasn't changed since the restore point, no tracks are removed or staged."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_100000")

    result = manager.restore_playlist_to_point(pid, "20260101_100000")
    assert result["removed"] == 0
    assert result["restored"] == 1
    assert len(result["to_stage"]) == 0


# ── Original tags tests ──

def test_add_track_stores_original_tags(manager, source):
    """add_track should save the source file's original tags in the track entry."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")

    track = manager.store.playlists[pid]["tracks"][0]
    assert "original_tags" in track
    assert track["original_tags"]["artist"] == "ArtistA"
    assert track["original_tags"]["title"] == "Song One"
    assert track["original_tags"]["album"] == "Test Album"


def test_backup_includes_original_tags(manager, source):
    """Backup should contain original_tags from the track entry."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")

    from echolist.config import load_backup
    data = load_backup(manager.writer.root, pid, "20260101_120000")
    entry = data["tracks"][0]
    assert entry["original_tags"]["artist"] == "ArtistA"
    assert entry["original_tags"]["album"] == "Test Album"
    assert entry["tags"]["albumartist"] == "* PLAYLISTS *"


def test_original_tags_stored_as_reference(manager, source):
    """original_tags are saved in backup as a reference but not used for restore.
    Restore uses tags (the state at backup time)."""
    pid = manager.create_playlist("Workout")
    manager.add_track(pid, source / "ArtistA" / "Album1" / "01 Song One.flac")
    manager.backup_playlist_metadata(pid, timestamp="20260101_120000")

    from echolist.config import load_backup
    data = load_backup(manager.writer.root, pid, "20260101_120000")
    entry = data["tracks"][0]
    assert entry["original_tags"]["artist"] == "ArtistA"
    assert entry["tags"]["albumartist"] == "* PLAYLISTS *"

    track = manager.store.playlists[pid]["tracks"][0]
    track_path = manager.writer.root / "Workout" / track["copy_name"]

    restored = manager.restore_playlist_metadata(pid, "20260101_120000")
    assert restored == 1

    f = FLAC(track_path)
    assert f["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f["ALBUM"] == ["Workout"]


# ── Imported playlist adoption tests ──

def test_detect_untracked_playlists(manager, source):
    """Folders with audio not in the store should be detected."""
    import shutil
    ext_folder = manager.writer.root / "Brian"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "Track.flac")

    untracked = manager.detect_untracked_playlists()
    assert len(untracked) == 1
    assert untracked[0]["folder"] == "Brian"
    assert untracked[0]["track_count"] == 1


def test_detect_untracked_ignores_tracked(manager, source):
    """Tracked playlists should not appear in untracked list."""
    manager.create_playlist("Workout")
    manager.add_track("workout", source / "ArtistA" / "Album1" / "01 Song One.flac")

    untracked = manager.detect_untracked_playlists()
    assert all(u["folder"] != "Workout" for u in untracked)


def test_adopt_playlist_creates_before_echolist_backup(manager, source):
    """Adopting should create a 'before_echolist' backup with original tags."""
    import shutil
    ext_folder = manager.writer.root / "Brian"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "Track.flac")

    pid = manager.adopt_playlist("Brian")

    backups = manager.list_metadata_backups(pid)
    assert any(b["timestamp"] == "before_echolist" for b in backups)

    from echolist.config import load_backup
    data = load_backup(manager.writer.root, pid, "before_echolist")
    entry = data["tracks"][0]
    assert entry["original_tags"]["artist"] == "ArtistA"
    assert entry["original_tags"]["album"] == "Test Album"


def test_adopt_playlist_modifies_metadata(manager, source):
    """After adoption, files should have EchoList metadata."""
    import shutil
    ext_folder = manager.writer.root / "Brian"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "Track.flac")

    pid = manager.adopt_playlist("Brian")

    track_name = manager.store.playlists[pid]["tracks"][0]["copy_name"]
    f = FLAC(ext_folder / track_name)
    assert f["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f["ALBUM"] == ["Brian"]
    assert f["TRACKNUMBER"] == ["1"]


def test_adopt_playlist_adds_to_store(manager, source):
    """Adopted playlist should appear in the store with correct track count."""
    import shutil
    ext_folder = manager.writer.root / "Brian"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "Song1.flac")
    shutil.copy2(source / "ArtistB" / "Album2" / "03 Song Two.flac", ext_folder / "Song2.flac")

    pid = manager.adopt_playlist("Brian")
    assert pid in manager.store.playlists
    assert len(manager.store.playlists[pid]["tracks"]) == 2
    assert manager.store.playlists[pid]["tracks"][0]["original_tags"]["artist"] in ("ArtistA", "ArtistB")


def test_adopt_playlist_restore_gives_original_tags(manager, source):
    """Restoring from before_echolist should give back the original metadata."""
    import shutil
    ext_folder = manager.writer.root / "Brian"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "Track.flac")

    pid = manager.adopt_playlist("Brian")
    track_name = manager.store.playlists[pid]["tracks"][0]["copy_name"]
    track_path = ext_folder / track_name

    f = FLAC(track_path)
    assert f["ALBUMARTIST"] == ["* PLAYLISTS *"]

    restored = manager.restore_playlist_metadata(pid, "before_echolist")
    assert restored == 1

    f2 = FLAC(track_path)
    assert f2["ARTIST"] == ["ArtistA"]
    assert f2["ALBUM"] == ["Test Album"]


def test_adopt_restore_then_fix_reapplies_echolist_tags(manager, source):
    """Adopt (A→B), restore before_echolist (→A), fix metadata (→B)."""
    import shutil
    ext_folder = manager.writer.root / "Brian"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "Track.flac")

    pid = manager.adopt_playlist("Brian")
    track_name = manager.store.playlists[pid]["tracks"][0]["copy_name"]
    track_path = ext_folder / track_name

    f_adopted = FLAC(track_path)
    assert f_adopted["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f_adopted["ALBUM"] == ["Brian"]

    manager.restore_playlist_metadata(pid, "before_echolist")

    f_restored = FLAC(track_path)
    assert f_restored["ARTIST"] == ["ArtistA"]
    assert f_restored["ALBUM"] == ["Test Album"]

    fixed = manager.fix_playlist_metadata(pid)
    assert fixed == 1

    f_fixed = FLAC(track_path)
    assert f_fixed["ALBUMARTIST"] == ["* PLAYLISTS *"]
    assert f_fixed["ALBUM"] == ["Brian"]

    from echolist.config import list_backups
    backups = list_backups(manager.writer.root, pid)
    assert any(b["timestamp"] == "before_echolist" for b in backups)


def test_adopt_reorder_restore_preserves_files(manager, source):
    """Adopt, reorder (triggers renaming), then restore before_echolist.
    Files must NOT be deleted — they should be renamed back and tags restored."""
    import shutil
    ext_folder = manager.writer.root / "Reggae"
    ext_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ArtistA" / "Album1" / "01 Song One.flac", ext_folder / "01. Song One.flac")
    shutil.copy2(source / "ArtistB" / "Album2" / "03 Song Two.flac", ext_folder / "03 Song Two.flac")
    shutil.copy2(source / "ArtistC" / "Album3" / "05 Song Three.flac", ext_folder / "05 Song Three.flac")

    pid = manager.adopt_playlist("Reggae")
    tracks = manager.store.playlists[pid]["tracks"]
    assert len(tracks) == 3

    original_names = [t["copy_name"] for t in tracks]

    manager.remove_track(pid, 2)
    tracks_after = manager.store.playlists[pid]["tracks"]
    assert len(tracks_after) == 2
    renamed_names = [t["copy_name"] for t in tracks_after]
    assert renamed_names != original_names[:2]

    result = manager.restore_playlist_to_point(pid, "before_echolist")

    assert result["removed"] == 0
    assert result["restored"] >= 2

    restored_tracks = manager.store.playlists[pid]["tracks"]
    assert len(restored_tracks) >= 2

    for t in restored_tracks:
        fpath = ext_folder / t["copy_name"]
        assert fpath.exists(), f"file deleted during restore: {t['copy_name']}"

    f1 = FLAC(ext_folder / restored_tracks[0]["copy_name"])
    assert f1["ARTIST"] == ["ArtistA"]
    assert f1["ALBUM"] == ["Test Album"]


def test_adopt_raises_if_already_tracked(manager, source):
    manager.create_playlist("Workout")
    with pytest.raises(ValueError, match="already exists"):
        manager.adopt_playlist("Workout")
