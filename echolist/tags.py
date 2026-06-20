"""Write playlist tags to copies (never originals)."""

from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4


def apply_playlist_tags(
    path: Path, node_name: str, album: str, index: int, src_rel: str, playlist_id: str
) -> None:
    suffix = path.suffix.lower()
    if suffix == ".flac":
        _tag_flac(path, node_name, album, index, src_rel, playlist_id)
    elif suffix == ".mp3":
        _tag_easy(EasyID3, path, node_name, album, index)
    elif suffix in (".m4a", ".mp4", ".aac"):
        _tag_easy(EasyMP4, path, node_name, album, index)


def read_playlist_tags(path: Path) -> dict:
    """Read the metadata fields EchoList cares about from a track file."""
    result = {"albumartist": "", "album": "", "tracknumber": "", "title": "", "artist": ""}
    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            f = FLAC(path)
            result["albumartist"] = (f.get("ALBUMARTIST") or [""])[0]
            result["album"] = (f.get("ALBUM") or [""])[0]
            result["tracknumber"] = (f.get("TRACKNUMBER") or [""])[0]
            result["title"] = (f.get("TITLE") or [""])[0]
            result["artist"] = (f.get("ARTIST") or [""])[0]
            result["echolist_role"] = (f.get("ECHOLIST_ROLE") or [""])[0]
        elif suffix == ".mp3":
            tags = EasyID3(path)
            result["albumartist"] = (tags.get("albumartist") or [""])[0]
            result["album"] = (tags.get("album") or [""])[0]
            result["tracknumber"] = (tags.get("tracknumber") or [""])[0]
            result["title"] = (tags.get("title") or [""])[0]
            result["artist"] = (tags.get("artist") or [""])[0]
        elif suffix in (".m4a", ".mp4", ".aac"):
            tags = EasyMP4(path)
            result["albumartist"] = (tags.get("albumartist") or [""])[0]
            result["album"] = (tags.get("album") or [""])[0]
            result["tracknumber"] = (tags.get("tracknumber") or [""])[0]
            result["title"] = (tags.get("title") or [""])[0]
            result["artist"] = (tags.get("artist") or [""])[0]
    except Exception:
        pass
    return result


def restore_tags(path: Path, tags: dict) -> None:
    """Write saved tag values back to a file (for metadata restore points)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            f = FLAC(path)
            for field in ("albumartist", "album", "tracknumber", "title", "artist"):
                flac_key = field.upper()
                if tags.get(field):
                    f[flac_key] = tags[field]
                elif flac_key in f:
                    del f[flac_key]
            f.save()
        elif suffix == ".mp3":
            t = EasyID3(path)
            for field in ("albumartist", "album", "tracknumber", "title", "artist"):
                if tags.get(field):
                    t[field] = tags[field]
                elif field in t:
                    del t[field]
            t.save(path)
        elif suffix in (".m4a", ".mp4", ".aac"):
            t = EasyMP4(path)
            for field in ("albumartist", "album", "tracknumber", "title", "artist"):
                if tags.get(field):
                    t[field] = tags[field]
                elif field in t:
                    del t[field]
            t.save(path)
    except Exception:
        pass


def _tag_flac(
    path: Path, node_name: str, album: str, index: int, src_rel: str, pid: str
) -> None:
    f = FLAC(path)
    f["ALBUMARTIST"] = node_name
    f["ALBUM"] = album
    f["TRACKNUMBER"] = str(index)
    f["DISCNUMBER"] = "1"
    f["ECHOLIST_ROLE"] = "playlist-copy"
    f["ECHOLIST_PLAYLIST"] = pid
    f["ECHOLIST_INDEX"] = str(index)
    f["ECHOLIST_SRC"] = src_rel
    f.save()


def _tag_easy(easy_cls, path: Path, node_name: str, album: str, index: int) -> None:
    try:
        tags = easy_cls(path)
    except mutagen.MutagenError:
        tags = easy_cls()
        tags.filename = str(path)
    tags["albumartist"] = node_name
    tags["album"] = album
    tags["tracknumber"] = str(index)
    tags["discnumber"] = "1"
    tags.save(path)
