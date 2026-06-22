"""Parse .m3u / .m3u8 playlist files into track lists."""

from __future__ import annotations

from pathlib import Path


def parse_m3u(m3u_path: Path, source_root: Path | None = None) -> dict:
    """Parse an .m3u or .m3u8 file.

    Returns {"name": str, "tracks": [Path, ...], "missing": [str, ...]}
    where tracks are resolved paths that exist and missing are raw entries
    that couldn't be found.
    """
    m3u_path = Path(m3u_path)
    name = m3u_path.stem

    encoding = "utf-8" if m3u_path.suffix.lower() == ".m3u8" else "utf-8-sig"
    try:
        lines = m3u_path.read_text(encoding=encoding).splitlines()
    except UnicodeDecodeError:
        lines = m3u_path.read_text(encoding="latin-1").splitlines()

    tracks: list[Path] = []
    missing: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("#"):
            if line.upper().startswith("#PLAYLIST:"):
                name = line.split(":", 1)[1].strip() or name
            continue
        if line.startswith(("http://", "https://", "mms://", "rtsp://")):
            continue

        entry = line.translate({ord("\\"): "/"})
        resolved = _resolve_entry(entry, m3u_path.parent, source_root)
        if resolved and resolved.is_file():
            tracks.append(resolved)
        else:
            missing.append(line)

    return {"name": name, "tracks": tracks, "missing": missing}


def _resolve_entry(entry: str, m3u_dir: Path, source_root: Path | None) -> Path | None:
    p = Path(entry)
    if p.is_absolute() and p.exists():
        return p.resolve()

    relative = m3u_dir / entry
    if relative.exists():
        return relative.resolve()

    if source_root:
        from_source = source_root / entry
        if from_source.exists():
            return from_source.resolve()

    return None


def curate_playlist_name(raw_name: str, existing_names: set[str]) -> str:
    """Clean up a playlist name from a filename, avoiding collisions."""
    from .naming import sanitize, playlist_id

    name = sanitize(raw_name)
    pid = playlist_id(name)
    if pid not in existing_names:
        return name

    n = 2
    while True:
        candidate = f"{name} ({n})"
        if playlist_id(candidate) not in existing_names:
            return candidate
        n += 1
        if n > 100:
            return f"{name} ({id(raw_name) % 10000})"
