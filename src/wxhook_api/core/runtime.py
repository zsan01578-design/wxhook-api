"""Identify the Python source loaded by a running development server."""

import hashlib
from pathlib import Path


def source_revision() -> str:
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    dependencies = package.parents[1] / "pyproject.toml"
    if dependencies.is_file():
        digest.update(dependencies.read_bytes())
    return digest.hexdigest()
