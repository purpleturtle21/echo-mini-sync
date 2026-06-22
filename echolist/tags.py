"""Write playlist tags to copies (never originals)."""

from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4


def _normalize_tracknumber(raw: str) -> str:
    """Strip 'N/M' or 'N\\M' total-tracks suffix, returning just the track number."""
    for sep in ("/", "\\"):
        if sep in raw:
            return raw.split(sep, 1)[0].strip()
    return raw.strip()


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
            result["tracknumber"] = _normalize_tracknumber((f.get("TRACKNUMBER") or [""])[0])
            result["title"] = (f.get("TITLE") or [""])[0]
            result["artist"] = (f.get("ARTIST") or [""])[0]
            result["echolist_role"] = (f.get("ECHOLIST_ROLE") or [""])[0]
        elif suffix == ".mp3":
            tags = EasyID3(path)
            result["albumartist"] = (tags.get("albumartist") or [""])[0]
            result["album"] = (tags.get("album") or [""])[0]
            result["tracknumber"] = _normalize_tracknumber((tags.get("tracknumber") or [""])[0])
            result["title"] = (tags.get("title") or [""])[0]
            result["artist"] = (tags.get("artist") or [""])[0]
        elif suffix in (".m4a", ".mp4", ".aac"):
            tags = EasyMP4(path)
            result["albumartist"] = (tags.get("albumartist") or [""])[0]
            result["album"] = (tags.get("album") or [""])[0]
            result["tracknumber"] = _normalize_tracknumber((tags.get("tracknumber") or [""])[0])
            result["title"] = (tags.get("title") or [""])[0]
            result["artist"] = (tags.get("artist") or [""])[0]
    except Exception:
        pass
    return result


def restore_tags(path: Path, tags: dict) -> None:
    """Write saved tag values back to a file (for metadata restore points)."""
    suffix = path.suffix.lower()
    cleaned = dict(tags)
    if cleaned.get("tracknumber"):
        cleaned["tracknumber"] = _normalize_tracknumber(cleaned["tracknumber"])
    try:
        if suffix == ".flac":
            f = FLAC(path)
            for field in ("albumartist", "album", "tracknumber", "title", "artist"):
                flac_key = field.upper()
                if cleaned.get(field):
                    f[flac_key] = cleaned[field]
                elif flac_key in f:
                    del f[flac_key]
            f.save()
        elif suffix == ".mp3":
            t = EasyID3(path)
            for field in ("albumartist", "album", "tracknumber", "title", "artist"):
                if cleaned.get(field):
                    t[field] = cleaned[field]
                elif field in t:
                    del t[field]
            t.save(path)
        elif suffix in (".m4a", ".mp4", ".aac"):
            t = EasyMP4(path)
            for field in ("albumartist", "album", "tracknumber", "title", "artist"):
                if cleaned.get(field):
                    t[field] = cleaned[field]
                elif field in t:
                    del t[field]
            t.save(path)
    except Exception:
        pass


def _extract_year(raw: str) -> str:
    """Extract a 4-digit year from any date string (e.g. '2023-10-19', '10/19/2023', '2023')."""
    import re
    m = re.search(r'\b(\d{4})\b', raw)
    return m.group(1) if m else raw


# TODO: ALBUMARTISTSORT overrides device grouping — the device uses it instead
# of ALBUMARTIST. We strip it now, but this could be a feature: setting
# ALBUMARTISTSORT intentionally could let us control how playlists appear
# in the device's artist/album browser. Needs testing on Echo Mini.
_FLAC_STRIP_TAGS = frozenset({
    "ALBUMARTISTSORT", "ARTISTSORT", "ALBUMARTISTS", "ALBUMARTISTS_SORT",
    "ALBUMARTISTS_CREDIT", "ALBUMARTIST_CREDIT",
    "ARTISTS", "ARTISTS_SORT", "ARTISTS_CREDIT", "ARTIST_CREDIT",
    "TOTALTRACKS", "TOTALDISCS", "TRACKTOTAL", "DISCTOTAL",
    "TRACKC", "DISCC", "TRACK", "DISC",
    "RELEASETYPE", "RELEASESTATUS", "RELEASECOUNTRY",
    "ALBUMARTISTSORT", "COMPOSERSORT",
    "MAIN_ARTIST", "ALBUM ARTIST", "ALBUM_ARTIST", "ALBUM_ARTISTS",
    "ORIGINALDATE", "ORIGINALYEAR",
    "MUSICBRAINZ_ALBUMARTISTID", "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_RELEASETRACKID", "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ_ALBUMSTATUS", "MUSICBRAINZ_ALBUMTYPE",
    "MUSICBRAINZ_ALBUMCOMMENT", "MUSICBRAINZ_DISCID",
    "MUSICBRAINZ_WORKID",
    "COMPILATION", "MEDIA", "SCRIPT", "BARCODE", "CATALOGNUMBER",
    "ASIN", "LABEL", "PUBLISHER", "ORGANIZATION",
    "AUTHOR", "LYRICIST", "ARRANGER", "CONDUCTOR",
    "ENCODEDBY", "ENCODER", "LANGUAGE", "BPM",
    "ACOUSTID_ID", "ACOUSTID_FINGERPRINT",
    "ITUNESADVISORY", "LENGTH",
    "DISCSUBTITLE", "GROUPING", "DESCRIPTION",
})


def _tag_flac(
    path: Path, node_name: str, album: str, index: int, src_rel: str, pid: str
) -> None:
    f = FLAC(path)
    for tag in _FLAC_STRIP_TAGS:
        if tag in f:
            del f[tag]
    f["ALBUMARTIST"] = node_name
    f["ALBUM"] = album
    f["TRACKNUMBER"] = str(index)
    f["DISCNUMBER"] = "1"
    date = (f.get("DATE") or [""])[0]
    if date:
        f["DATE"] = _extract_year(date)
    f["ECHOLIST_ROLE"] = "playlist-copy"
    f["ECHOLIST_PLAYLIST"] = pid
    f["ECHOLIST_INDEX"] = str(index)
    f["ECHOLIST_SRC"] = src_rel
    f.save()


_EASY_STRIP_TAGS = frozenset({
    "albumartistsort", "artistsort", "composersort",
    "compilation", "media", "barcode", "catalognumber",
    "asin", "organization",
    "musicbrainz_albumartistid", "musicbrainz_albumid",
    "musicbrainz_artistid", "musicbrainz_trackid",
    "musicbrainz_releasetrackid", "musicbrainz_releasegroupid",
    "musicbrainz_albumstatus", "musicbrainz_albumtype",
})


def _tag_easy(easy_cls, path: Path, node_name: str, album: str, index: int) -> None:
    try:
        tags = easy_cls(path)
    except mutagen.MutagenError:
        tags = easy_cls()
        tags.filename = str(path)
    for tag in _EASY_STRIP_TAGS:
        if tag in tags:
            del tags[tag]
    tags["albumartist"] = node_name
    tags["album"] = album
    tags["tracknumber"] = str(index)
    tags["discnumber"] = "1"
    date = (tags.get("date") or [""])[0]
    if date:
        tags["date"] = _extract_year(date)
    tags.save(path)
