#!/usr/bin/env python3
"""Build the Azure Web App ZIP from code plus the verified generated registry release."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def _git_files(root: Path) -> set[Path]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True).stdout
    return {root / os.fsdecode(name) for name in output.split(b"\0") if name}


def archive_files(root: Path = ROOT, output: Path | None = None) -> dict[str, Path]:
    """Return archive name -> local file, dereferencing registry/current into one generation."""
    files = _git_files(root)
    files.update(path for path in (root / "sources").rglob("*") if path.is_file())
    current = root / "registry" / "current"
    if not current.is_dir():
        raise RuntimeError("registry/current is missing; build a registry release first")

    archive = {}
    for path in files:
        if path.is_file() and "__pycache__" not in path.parts and path.resolve() != output:
            archive[path.relative_to(root).as_posix()] = path
    for path in current.rglob("*"):
        if path.is_file():
            relative = path.relative_to(current).as_posix()
            archive[f"registry/current/{relative}"] = path
    return archive


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(output: Path):
    subprocess.run([sys.executable, "registry/index.py", "verify", "--release"],
                   cwd=ROOT, check=True)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = archive_files(ROOT, output)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, name)
    digest = sha256(output)
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"{output}  {output.stat().st_size:,} bytes  sha256:{digest}")
    print(f"Deploy: az webapp deploy --resource-group <group> --name <app> --src-path {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, help="output ZIP, normally under dist/")
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
