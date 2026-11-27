"""GT4.VOL as a folder tree: extract every entry, rebuild the image from the tree.

The port iterates on files, not on 5.3 GB ISO copies.  This module unpacks each
catalogue entry's decoded payload to its path under a folder and keeps a manifest
(kind, decoded size, SHA-256) beside it; a rebuild sends back only the files whose
hash moved, so the volume writer reuses every other shipped entry byte for byte.

The folder holds the owner's own game data: it lives outside git, like every other
extracted input, and the repository ships only this tooling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import gt4_volume_write as vw

MANIFEST = "manifest.json"


class FolderError(ValueError):
    """A folder tree that does not match its manifest or the image."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FolderError(message)


def safe_path(path: str) -> Path:
    parts = path.split("/")
    require(bool(path) and all(part and part not in (".", "..") for part in parts),
            "unsafe catalogue path: " + path)
    require(not path.startswith("/") and "\\" not in path and "\x00" not in path,
            "unsafe catalogue path: " + path)
    return Path(*parts)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(image: Path, folder: Path, only: list[str] | None = None) -> dict:
    """Write every selected catalogue entry as a file, with a manifest."""
    require(not (folder / MANIFEST).exists(), "folder already holds a manifest")
    entries = []
    written = 0
    with image.open("rb") as stream:
        catalogue = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
        for path in sorted(catalogue.files):
            if only and not any(part in path for part in only):
                continue
            record = catalogue.files[path]
            stored = vw.read_at(stream, vw.VOLUME_OFFSET + record.offset, record.stored_size,
                                vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
            payload = vw.decode_payload(stored, record.kind, record.size)
            target = folder / safe_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            entries.append({"path": path, "kind": record.kind, "size": len(payload),
                            "sha256": digest(payload)})
            written += len(payload)
    manifest = {"volume": {"offset": vw.VOLUME_OFFSET, "size": vw.VOLUME_SIZE},
                "files": entries}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return {"files": len(entries), "bytes": written, "folder": str(folder)}


def read_manifest(folder: Path) -> dict:
    manifest_file = folder / MANIFEST
    require(manifest_file.is_file(), "folder has no manifest: " + str(folder))
    manifest = json.loads(manifest_file.read_text())
    require(manifest.get("volume", {}).get("size") == vw.VOLUME_SIZE,
            "manifest was written for another volume extent")
    files = {}
    for entry in manifest["files"]:
        files[entry["path"]] = entry
    return files


def changed(folder: Path) -> dict[str, bytes]:
    """Return the files whose bytes no longer match their manifest hash."""
    files = read_manifest(folder)
    replacements = {}
    for path, entry in files.items():
        data = (folder / safe_path(path)).read_bytes()
        if digest(data) != entry["sha256"]:
            require(len(data) <= vw.FILE_CAP, "file cap exceeded: " + path)
            replacements[path] = data
    return replacements


def rebuild(image: Path, folder: Path, destination: Path, growth: int = 0) -> dict:
    """Write an image whose volume carries the folder's changed files."""
    replacements = changed(folder)
    require(not destination.exists(), "destination image already exists")
    if growth:
        import gt4_iso_grow
        report = gt4_iso_grow.grow_image(image, destination, growth, replacements, None)
        return {"replacements": sorted(replacements), "growth": report["growth"],
                "volume_size": report["volume_size"]}
    size = image.stat().st_size
    with image.open("rb") as stream:
        plan = vw.build_plan(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE, replacements, None)
        with destination.open("xb") as output:
            vw.copy_range(stream, output, 0, vw.VOLUME_OFFSET, size)
            vw.write_plan(stream, output, plan, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            vw.copy_range(stream, output, vw.VOLUME_OFFSET + vw.VOLUME_SIZE,
                          size - vw.VOLUME_OFFSET - vw.VOLUME_SIZE, size)
    return {"replacements": sorted(replacements), "growth": 0,
            "volume_size": vw.VOLUME_SIZE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GT4.VOL as a folder tree.")
    commands = parser.add_subparsers(dest="command", required=True)
    unpack = commands.add_parser("extract")
    unpack.add_argument("image", type=Path)
    unpack.add_argument("folder", type=Path)
    unpack.add_argument("--only", action="append", default=[], metavar="SUBSTRING")
    pack = commands.add_parser("rebuild")
    pack.add_argument("image", type=Path)
    pack.add_argument("folder", type=Path)
    pack.add_argument("destination", type=Path)
    pack.add_argument("--grow-mb", type=int, default=0)
    listing = commands.add_parser("status")
    listing.add_argument("folder", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "extract":
        print(extract(arguments.image, arguments.folder, arguments.only))
    elif arguments.command == "rebuild":
        print(rebuild(arguments.image, arguments.folder, arguments.destination,
                      arguments.grow_mb * 1024 * 1024))
    else:
        replacements = changed(arguments.folder)
        print({"changed": sorted(replacements)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
