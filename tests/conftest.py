"""Test fixtures: embedded FLAC, dummy source tree, manager."""

import base64
import hashlib
import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from echolist.manager import PlaylistManager
import echolist.config as _config

# Minimal valid FLAC (1 sample, 44100 Hz, 16-bit mono) — no ffmpeg needed.
TINY_FLAC = base64.b64decode(
    "ZkxhQwAAACIAAQABAAAAAAAACsRA8AAAAAHEED8SLSdnfJ2xRMrhOUpmhAAADQUAAABTaWRlQgAAAAD/+GkIAAAdAAAAoCc="
)


def _make_flac(path: Path, artist: str, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_FLAC)
    f = FLAC(path)
    f["ARTIST"] = artist
    f["TITLE"] = title
    f["ALBUM"] = "Test Album"
    f["TRACKNUMBER"] = "1"
    f.save()


@pytest.fixture(autouse=True)
def _isolate_backups(tmp_path, monkeypatch):
    """Redirect BACKUPS_ROOT to a temp dir so tests don't pollute ~/.echolist/."""
    monkeypatch.setattr(_config, "BACKUPS_ROOT", tmp_path / "backups")


@pytest.fixture
def source(tmp_path):
    lib = tmp_path / "library"
    _make_flac(lib / "ArtistA" / "Album1" / "01 Song One.flac", "ArtistA", "Song One")
    _make_flac(lib / "ArtistB" / "Album2" / "03 Song Two.flac", "ArtistB", "Song Two")
    _make_flac(lib / "ArtistC" / "Album3" / "05 Song Three.flac", "ArtistC", "Song Three")
    return lib


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "card"
    d.mkdir()
    return d


@pytest.fixture
def manager(source, dest):
    mgr = PlaylistManager.init(source, dest)
    yield mgr
    mgr.release_lock()


def _hash_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def hashes(source):
    return {str(f): _hash_file(f) for f in source.rglob("*") if f.is_file()}


def assert_originals_untouched(source, hashes):
    for path_str, expected in hashes.items():
        p = Path(path_str)
        assert p.exists(), f"original deleted: {p}"
        assert _hash_file(p) == expected, f"original modified: {p}"
