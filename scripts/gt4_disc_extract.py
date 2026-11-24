"""Extract the pinned USA v2.00 disc into a folder for TASK-220.

The game reads its data from the disc image today. TASK-220 makes it read the same
leaves from a folder instead, so a local install no longer needs the 5,314,478,080
byte ISO. Everything this writes belongs to the owner's own disc: it is never
committed and never shipped.

Usage: gt4_disc_extract.py --iso <gt4.iso> --output <folder>
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pycdlib

ROOT = Path(__file__).resolve().parents[1]
DISC_MANIFEST = ROOT / "docs/inputs/usa-v2.00.json"
TOTAL_CAP = 6 * 1024 * 1024 * 1024
VOLUME_NAME = "GT4.VOL"
# docs/VOLUME-READER.md: the pinned GT4.VOL extent of this disc.
VOLUME_SHA256 = "49f1a67a75542f93b0ffcedd7ef4d83149b8d2165186ee96fd527980d3688e1c"


class ExtractError(ValueError):
    """The image or the output folder cannot be used as asked."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ExtractError(message)


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1 << 24)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def verify_disc(path: Path, pin: dict) -> None:
    require(path.stat().st_size == pin["disc"]["size_bytes"], "disc image size differs")
    require(sha256_of_file(path) == pin["disc"]["sha256"],
            "disc image does not match the pinned USA v2.00 dump")


def fresh_directory(output: Path) -> Path:
    if output.exists():
        require(output.is_dir(), "output exists and is not a directory")
        require(not any(output.iterdir()), "output directory is not empty")
    else:
        output.mkdir(parents=True)
    return output


def leaves(iso, directory: str = "/"):
    """Every file record, depth first, with its ISO path."""
    for record in iso.list_children(iso_path=directory):
        name = record.file_identifier().decode("ascii")
        if name in (".", ".."):
            continue
        child = directory.rstrip("/") + "/" + name
        if record.is_dir():
            yield from leaves(iso, child)
        else:
            yield child, record


def target_path(root: Path, iso_path: str) -> Path:
    relative = Path(iso_path.strip("/").split(";")[0])
    require(not relative.is_absolute() and ".." not in relative.parts,
            "disc path escapes the target folder: " + iso_path)
    return root / relative


def extract(iso_path: Path, output: Path) -> dict:
    pin = json.loads(DISC_MANIFEST.read_text(encoding="utf-8"))
    verify_disc(iso_path, pin)
    output = fresh_directory(output)
    written = []
    total = 0
    iso = pycdlib.PyCdlib()
    with iso_path.open("rb") as stream:
        iso.open_fp(stream)
        try:
            for iso_name, record in leaves(iso):
                target = target_path(output, iso_name)
                length = record.data_length
                total += length
                require(total <= TOTAL_CAP, "disc tree exceeds the extraction cap")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.name == VOLUME_NAME:
                    # The volume is one contiguous extent; copy it without decoding.
                    require(length == 2_459_502_592, "unexpected GT4.VOL extent")
                    stream.seek(record.extent_location() * 2048)
                    remaining = length
                    with target.open("xb") as destination:
                        while remaining:
                            chunk = stream.read(min(1 << 24, remaining))
                            require(bool(chunk), "image ended inside GT4.VOL")
                            destination.write(chunk)
                            remaining -= len(chunk)
                else:
                    with target.open("xb") as destination:
                        iso.get_file_from_iso_fp(destination, iso_path=iso_name)
                written.append({"path": target.relative_to(output).as_posix(),
                                "size_bytes": length})
        finally:
            iso.close()
    return {"written": written, "total_bytes": total}


def verify_folder(output: Path, pin: dict) -> dict:
    """Check the extracted tree against the disc pins and the volume hash."""
    checks = {}
    for pinned_path, expected in pin["files"].items():
        relative = pinned_path.strip("/").split(";")[0]
        target = output / relative
        require(target.is_file(), "pinned file is missing: " + relative)
        require(target.stat().st_size == expected["size_bytes"], "size differs: " + relative)
        require(sha256_of_file(target) == expected["sha256"], "hash differs: " + relative)
        checks[relative] = expected["sha256"]
    volume = output / VOLUME_NAME
    require(volume.is_file() and volume.stat().st_size == 2_459_502_592, "GT4.VOL size differs")
    require(sha256_of_file(volume) == VOLUME_SHA256, "GT4.VOL hash differs")
    checks[VOLUME_NAME] = VOLUME_SHA256
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = extract(arguments.iso, arguments.output)
    checks = verify_folder(arguments.output, json.loads(DISC_MANIFEST.read_text(encoding="utf-8")))
    print(json.dumps({"output": str(arguments.output), "files": len(report["written"]),
                      "total_bytes": report["total_bytes"], "verified": checks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
