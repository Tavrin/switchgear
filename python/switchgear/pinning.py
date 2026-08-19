"""Execution-profile pinning by CONTENT, not by path.

A consuming orchestrator records an execution profile at first spawn -- a digest
of the spawn environment plus the resolved executable -- and enforces it on resume. For
this rail a resolved-path pin would be worthless: `~/.local/bin/switchgear` is a
stable path that is a symlink into the working tree, so it always resolves and
its content changes with every edit. That is the "latest version wins" drift
the orchestrator had to pin away for codex, one level deeper.

And the launcher alone is not the program. It is an 11-line stub whose only job
is to resolve symlinks and exec `python/switchgear/`; digesting just the resolved
executable would pin the one file that never changes while missing everything
that does. So the digest covers the launcher AND a deterministic walk of the
package it execs.
"""

from __future__ import annotations

import hashlib
import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(PACKAGE_DIR))
LAUNCHER = os.path.join(ROOT, "bin", "switchgear")


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def package_files(package_dir: str = PACKAGE_DIR) -> list[str]:
    """Every source file the launcher can execute, in a stable order.

    Sorted by relative path so the digest is reproducible across machines and
    filesystem orderings. __pycache__ is excluded: it is derived from the
    sources already covered here, and including it would make the digest depend
    on whether anything happened to have imported the package yet.
    """
    out: list[str] = []
    for base, dirs, files in os.walk(package_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            out.append(os.path.join(base, name))
    return sorted(out, key=lambda p: os.path.relpath(p, package_dir))


def launcher_digest(launcher: str = LAUNCHER, package_dir: str = PACKAGE_DIR) -> str:
    """sha256 over the launcher plus a deterministic walk of the package.

    A digest of the per-file digest list rather than of concatenated bytes, so
    that renaming a file changes the result even when the bytes are identical.
    """
    parts: list[str] = []
    if os.path.isfile(launcher):
        parts.append(f"launcher:{_file_digest(os.path.realpath(launcher))}")
    for path in package_files(package_dir):
        rel = os.path.relpath(path, package_dir)
        parts.append(f"{rel}:{_file_digest(path)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def execution_profile(launcher: str = LAUNCHER, package_dir: str = PACKAGE_DIR) -> dict:
    return {
        "launcher": os.path.realpath(launcher) if os.path.exists(launcher) else launcher,
        "launcher_is_symlink": os.path.islink(launcher),
        "package": package_dir,
        "file_count": len(package_files(package_dir)),
        "launcherDigest": launcher_digest(launcher, package_dir),
    }
