#!/usr/bin/env python3
"""Build a reproducible DataMax-LSI release archive and update release metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import time
from datetime import date
from pathlib import Path

EXCLUDES = {
    ".git",
    ".venv",
    ".deploy-backups",
    ".impeccable",
    "data",
    "dist",
    "charts",
    "__pycache__",
    "node_modules",
    "release-site",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_PREFIXES = ("storcli.log", "web_server.out")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version(source: Path) -> str:
    text = (source / "web_server.py").read_text("utf-8")
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit("无法从 web_server.py 读取 VERSION")
    return match.group(1)


def git_value(source: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(source), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def include(path: Path, source: Path) -> bool:
    rel = path.relative_to(source)
    if any(part in EXCLUDES for part in rel.parts):
        return False
    name = path.name
    if name.endswith(EXCLUDED_SUFFIXES) or name.startswith(EXCLUDED_PREFIXES):
        return False
    if ".bak-" in name:
        return False
    return path.is_file()


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(os.environ.get("SOURCE_DATE_EPOCH", str(int(time.time()))))
    from io import BytesIO

    archive.addfile(info, BytesIO(data))


def build(source: Path, site_dir: Path, version: str | None, notes: str) -> Path:
    source = source.resolve()
    version = version or project_version(source)
    package_name = f"datamax-lsi-{version}"
    archive_path = site_dir / f"{package_name}.tar.gz"
    commit = git_value(source, "rev-parse", "--short", "HEAD")
    release_info = "\n".join([
        f"Name: DataMax-LSI",
        f"Version: {version}",
        f"Build-Date: {date.today().isoformat()}",
        f"Source-Commit: {commit or 'working-tree'}",
        "StorCLI: not bundled; provide a licensed storcli64 separately.",
        "",
    ]).encode("utf-8")

    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        add_bytes(archive, f"{package_name}/RELEASE.txt", release_info)
        for path in sorted(source.rglob("*")):
            if not include(path, source):
                continue
            arcname = f"{package_name}/{path.relative_to(source)}"
            archive.add(path, arcname=arcname, recursive=False)

    digest = sha256(archive_path)
    manifest_path = site_dir / "releases.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except Exception:
        manifest = {"latest": version, "releases": []}
    releases = [
        item for item in manifest.get("releases", [])
        if item.get("version") != version
    ]
    releases.insert(0, {
        "version": version,
        "date": date.today().isoformat(),
        "file": archive_path.name,
        "sha256": digest,
        "size": archive_path.stat().st_size,
        "notes": notes,
        "commit": commit,
    })
    manifest = {"latest": version, "releases": releases}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--site", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--version")
    parser.add_argument("--notes", default="正式发布")
    args = parser.parse_args()
    artifact = build(args.source, args.site.resolve(), args.version, args.notes)
    print(artifact)


if __name__ == "__main__":
    main()
