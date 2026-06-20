"""Tests for .m3u parsing and playlist name curation."""

from pathlib import Path

import pytest

from echolist.m3u import parse_m3u, curate_playlist_name
from conftest import _make_flac


@pytest.fixture
def music_lib(tmp_path):
    """Create a small music library for m3u resolution."""
    lib = tmp_path / "library"
    _make_flac(lib / "ArtistA" / "Album1" / "01 Song One.flac", "ArtistA", "Song One")
    _make_flac(lib / "ArtistB" / "Album2" / "03 Song Two.flac", "ArtistB", "Song Two")
    _make_flac(lib / "ArtistC" / "Album3" / "05 Song Three.flac", "ArtistC", "Song Three")
    return lib


# ── Parsing ──

def test_parse_basic_m3u(music_lib, tmp_path):
    m3u = tmp_path / "Workout.m3u"
    m3u.write_text(
        "#EXTM3U\n"
        "#EXTINF:240,ArtistA - Song One\n"
        "ArtistA/Album1/01 Song One.flac\n"
        "#EXTINF:195,ArtistB - Song Two\n"
        "ArtistB/Album2/03 Song Two.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u, source_root=music_lib)
    assert result["name"] == "Workout"
    assert len(result["tracks"]) == 2
    assert len(result["missing"]) == 0
    assert result["tracks"][0].name == "01 Song One.flac"
    assert result["tracks"][1].name == "03 Song Two.flac"


def test_parse_m3u_with_missing_tracks(music_lib, tmp_path):
    m3u = tmp_path / "Broken.m3u"
    m3u.write_text(
        "#EXTM3U\n"
        "nonexistent/missing.flac\n"
        "ArtistA/Album1/01 Song One.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u, source_root=music_lib)
    assert len(result["tracks"]) == 1
    assert len(result["missing"]) == 1
    assert result["missing"][0] == "nonexistent/missing.flac"


def test_parse_m3u_backslash_paths(music_lib, tmp_path):
    """Windows-style backslash paths in .m3u should resolve correctly."""
    m3u = tmp_path / "WinStyle.m3u"
    m3u.write_text(
        "ArtistA\\Album1\\01 Song One.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u, source_root=music_lib)
    assert len(result["tracks"]) == 1


def test_parse_m3u_relative_to_m3u_dir(music_lib):
    """Tracks should resolve relative to the .m3u file's directory."""
    m3u = music_lib / "playlist.m3u"
    m3u.write_text(
        "ArtistC/Album3/05 Song Three.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u)
    assert len(result["tracks"]) == 1


def test_parse_m3u_skips_comments_and_blanks(music_lib, tmp_path):
    m3u = tmp_path / "Comments.m3u"
    m3u.write_text(
        "#EXTM3U\n"
        "# This is a comment\n"
        "\n"
        "   \n"
        "#EXTINF:240,Title\n"
        "ArtistA/Album1/01 Song One.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u, source_root=music_lib)
    assert len(result["tracks"]) == 1
    assert len(result["missing"]) == 0


def test_parse_m3u8_utf8(music_lib, tmp_path):
    m3u = tmp_path / "Unicode.m3u8"
    m3u.write_text(
        "#EXTM3U\n"
        "ArtistA/Album1/01 Song One.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u, source_root=music_lib)
    assert len(result["tracks"]) == 1


def test_parse_empty_m3u(tmp_path):
    m3u = tmp_path / "Empty.m3u"
    m3u.write_text("#EXTM3U\n", encoding="utf-8")
    result = parse_m3u(m3u)
    assert result["name"] == "Empty"
    assert len(result["tracks"]) == 0
    assert len(result["missing"]) == 0


def test_parse_all_missing(tmp_path):
    m3u = tmp_path / "AllGone.m3u"
    m3u.write_text(
        "nope/a.flac\n"
        "nope/b.flac\n",
        encoding="utf-8",
    )
    result = parse_m3u(m3u)
    assert len(result["tracks"]) == 0
    assert len(result["missing"]) == 2


def test_parse_absolute_paths(music_lib, tmp_path):
    """Absolute paths in .m3u should resolve directly."""
    track = music_lib / "ArtistA" / "Album1" / "01 Song One.flac"
    m3u = tmp_path / "Absolute.m3u"
    m3u.write_text(str(track) + "\n", encoding="utf-8")
    result = parse_m3u(m3u)
    assert len(result["tracks"]) == 1


def test_parse_name_from_filename(tmp_path):
    m3u = tmp_path / "My Cool Playlist.m3u"
    m3u.write_text("#EXTM3U\n", encoding="utf-8")
    result = parse_m3u(m3u)
    assert result["name"] == "My Cool Playlist"


# ── Name curation ──

def test_curate_simple_name():
    name = curate_playlist_name("Workout Mix", set())
    assert name == "Workout Mix"


def test_curate_name_sanitizes_illegal_chars():
    name = curate_playlist_name('My:Playlist<>"', set())
    assert ":" not in name
    assert "<" not in name


def test_curate_name_avoids_collision():
    existing = {"workout_mix"}
    name = curate_playlist_name("Workout Mix", existing)
    assert name == "Workout Mix (2)"


def test_curate_name_avoids_multiple_collisions():
    existing = {"workout_mix", "workout_mix_(2)", "workout_mix_(3)"}
    name = curate_playlist_name("Workout Mix", existing)
    assert name == "Workout Mix (4)"


def test_curate_name_no_collision_different_pid():
    existing = {"chill_vibes"}
    name = curate_playlist_name("Workout Mix", existing)
    assert name == "Workout Mix"
