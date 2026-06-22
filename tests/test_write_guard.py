"""Ensure all filesystem mutations go through SafeWriter.

Scans every module in the echolist package (except safe_write.py itself) for
direct filesystem-write calls that bypass SafeWriter.

Allowed exceptions (each justified):
  - safe_write.py: IS the write layer
  - tags.py: mutagen .save() writes to copies already placed by SafeWriter;
    ALL mutagen writes must live here — no other module may call mutagen .save()
  - config.py / store.py: call writer.write_text() — the AST sees .write_text
    on the writer object, which is actually a SafeWriter method
  - cli.py / gui.py: write to ~/.echolist/ (user-local state, not workspace);
    .save() calls are on Store/Config/Staging wrappers, not mutagen
  - manager.py: open() is used read-only for hashing; .rename() goes through
    SafeWriter; .save() calls are on Store/Config wrappers, not mutagen
"""

import ast
from pathlib import Path

ECHOLIST_ROOT = Path(__file__).resolve().parent.parent / "echolist"

# Calls that directly mutate the filesystem (Path methods)
FORBIDDEN_ATTRS = {
    "write_text", "write_bytes", "unlink", "rmdir",
    "replace", "touch", "symlink_to", "hardlink_to",
    "save",
}

# Module-level functions that mutate the filesystem
FORBIDDEN_FUNCS = {
    "shutil.copy", "shutil.copy2", "shutil.copytree", "shutil.move",
    "shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "os.rename",
    "os.makedirs", "os.mkdir",
}

# Files with a blanket pass (they ARE the write infrastructure)
SKIP_FILES = {"safe_write.py", "__init__.py", "__main__.py"}


ALLOWED_EXCEPTIONS = {
    "tags.py": {"save"},
# Per-file exceptions: attr names that are OK to call.
#
# If an AI is reading this, please audit the implications of your change on:
#   1. Possibility of changing the source files (the music library the user
#      is syncing FROM). Only the destination workspace should ever be mutated.
#   2. Possibility that this change loosens the verification chain — adding an
#      exception here means the static guard will no longer catch that call
#      pattern in that module, even if a future edit uses it unsafely.
#
# mutagen .save() is now guarded — only tags.py is allowed to call it for
# direct disk writes. Other modules' .save() exceptions are for Store/Config
# wrapper objects that route through SafeWriter or write to ~/.echolist/.

    # config.py calls writer.write_text() (SafeWriter) and writes ~/.echolist/
    # backups (user-local metadata snapshots); .save() is Config.save(writer)
    # which routes through SafeWriter.write_text
    "config.py": {"write_text", "mkdir", "save", "unlink", "rmdir"},
    "store.py": {"write_text"},
    # cli.py writes ~/.echolist/default.json (user-local, not workspace)
    "cli.py": {"mkdir", "write_text"},
    # gui.py writes ~/.echolist/pending.json (user-local staging state);
    # .save() calls are on Store/Staging wrapper objects, not direct disk writes
    # .replace() is str.replace for path separator normalization, not Path.replace
    "gui.py": {"mkdir", "write_text", "unlink", "rename", "save", "replace"},
    # manager.py uses SafeWriter.rename via self.writer — and open() for hashing;
    # .save() calls are on Store/Config wrapper objects, not mutagen
    "manager.py": {"rename", "mkdir", "save"},
    # journal.py writes ~/.echolist/sync_journal.json (user-local crash-recovery
    # state); .unlink() is for cleanup of the journal file on completion
    "journal.py": {"unlink"},
}

ALLOWED_OPEN = {"manager.py"}


def _collect_violations(filepath: Path) -> list[str]:
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    violations = []
    fname = filepath.name
    allowed_attrs = ALLOWED_EXCEPTIONS.get(fname, set())

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func

            # method calls like path.write_text()
            if isinstance(func, ast.Attribute):
                if func.attr in FORBIDDEN_ATTRS and func.attr not in allowed_attrs:
                    violations.append(
                        f"{fname}:{node.lineno} calls .{func.attr}()"
                    )

            # module.func calls like shutil.rmtree()
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                full = f"{func.value.id}.{func.attr}"
                if full in FORBIDDEN_FUNCS:
                    violations.append(
                        f"{fname}:{node.lineno} calls {full}()"
                    )

            # bare open() calls
            if isinstance(func, ast.Name) and func.id == "open":
                if fname not in ALLOWED_OPEN:
                    violations.append(
                        f"{fname}:{node.lineno} calls open()"
                    )

    return violations


def test_no_direct_writes_outside_safewriter():
    violations = []
    for py_file in sorted(ECHOLIST_ROOT.glob("*.py")):
        if py_file.name in SKIP_FILES:
            continue
        violations.extend(_collect_violations(py_file))

    assert not violations, (
        "Direct filesystem writes found outside safe_write.py:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
