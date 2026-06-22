"""SafeWriter — the single chokepoint for all filesystem mutations."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path


class UnsafeWriteError(Exception):
    ...


class SafeWriter:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, rel_or_abs) -> Path:
        p = Path(rel_or_abs)
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if not p.is_relative_to(self.root):
            raise UnsafeWriteError(f"outside workspace: {p}")
        return p

    def makedirs(self, rel) -> Path:
        p = self._resolve(rel)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_text(self, rel, text) -> Path:
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, text.encode("utf-8"))
        return p

    def write_bytes(self, rel, data) -> Path:
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, data)
        return p

    def copy_in(self, src, rel, progress_cb=None, compute_hash=False):
        dest = self._resolve(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if progress_cb or compute_hash:
            src = Path(src)
            total = src.stat().st_size
            copied = 0
            h = hashlib.blake2b() if compute_hash else None
            with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
                while True:
                    buf = fsrc.read(1024 * 256)
                    if not buf:
                        break
                    fdst.write(buf)
                    if h:
                        h.update(buf)
                    copied += len(buf)
                    if progress_cb:
                        progress_cb(copied, total)
            shutil.copystat(src, dest)
            return dest, h.hexdigest() if h else None
        else:
            shutil.copy2(src, dest)
            return dest, None

    def delete(self, rel) -> None:
        p = self._resolve(rel)
        if p == self.root:
            raise UnsafeWriteError("refusing to delete workspace root")
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def rename(self, old_rel, new_rel) -> Path:
        old = self._resolve(old_rel)
        new = self._resolve(new_rel)
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        return new

    def resolve(self, rel) -> Path:
        return self._resolve(rel)


def _atomic_write(dest: Path, data: bytes) -> None:
    """Write data to dest atomically via temp-file + os.replace."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        os.replace(tmp, dest)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(dest: Path, text: str) -> None:
    """Write text to dest atomically. Safe for use outside the workspace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest, text.encode("utf-8"))
