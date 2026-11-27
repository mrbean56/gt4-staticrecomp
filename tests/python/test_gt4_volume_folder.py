"""Controls for the GT4.VOL folder tree (extract, edit, rebuild).

The extraction test takes two small entries; the rebuild still writes the whole
5.3 GB image, so the class builds one image and asserts several things about it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_volume_folder as folder_tree
import gt4_volume_write as vw

ISO = Path(os.environ.get("GT4_ISO", ""))
PICKED = ("crs/tsukuba.ad", "projects/boot/US_carlist.adc")


class RealDiscTests(unittest.TestCase):
    work: Path | None = None
    folder: Path = Path()
    rebuilt: Path = Path()
    edited = PICKED[0]

    @classmethod
    def setUpClass(cls):
        if not ISO.is_file():
            raise unittest.SkipTest("GT4_ISO is not set to a disc image")
        cls.work = Path(tempfile.mkdtemp(prefix="gt4-folder-"))
        cls.folder = cls.work / "tree"
        cls.rebuilt = cls.work / "rebuilt.iso"
        folder_tree.extract(ISO, cls.folder, list(PICKED))
        target = cls.folder / folder_tree.safe_path(cls.edited)
        target.write_bytes(target.read_bytes() + b"\x00\x01")
        folder_tree.rebuild(ISO, cls.folder, cls.rebuilt)

    @classmethod
    def tearDownClass(cls):
        if cls.work is not None:
            import shutil
            shutil.rmtree(cls.work, ignore_errors=True)

    def test_the_manifest_names_the_picked_entries(self):
        manifest = json.loads((self.folder / folder_tree.MANIFEST).read_text())
        paths = [entry["path"] for entry in manifest["files"]]
        self.assertEqual(paths, sorted(PICKED))
        with ISO.open("rb") as stream:
            catalogue = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            for entry in manifest["files"]:
                record = catalogue.files[entry["path"]]
                stored = vw.read_at(stream, vw.VOLUME_OFFSET + record.offset,
                                    record.stored_size, vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
                payload = vw.decode_payload(stored, record.kind, record.size)
                self.assertEqual(entry["size"], len(payload), entry["path"])
                self.assertEqual(entry["sha256"], folder_tree.digest(payload), entry["path"])

    def test_only_the_edited_file_is_sent_back(self):
        replacements = folder_tree.changed(self.folder)
        self.assertEqual(list(replacements), [self.edited])

    def test_the_rebuilt_volume_carries_the_edit(self):
        with ISO.open("rb") as stream:
            before = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            shipped = vw.read_at(stream, vw.VOLUME_OFFSET + before.files[self.edited].offset,
                                 before.files[self.edited].stored_size,
                                 vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
        with self.rebuilt.open("rb") as stream:
            after = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            record = after.files[self.edited]
            stored = vw.read_at(stream, vw.VOLUME_OFFSET + record.offset, record.stored_size,
                                vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
        payload = vw.decode_payload(stored, record.kind, record.size)
        original = vw.decode_payload(shipped, before.files[self.edited].kind,
                                     before.files[self.edited].size)
        self.assertEqual(payload, original + b"\x00\x01")
        self.assertEqual(sorted(after.files), sorted(before.files))

    def test_every_other_entry_keeps_its_bytes(self):
        with ISO.open("rb") as source, self.rebuilt.open("rb") as stream:
            before = vw.read_catalogue(source, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            after = vw.read_catalogue(stream, vw.VOLUME_OFFSET, vw.VOLUME_SIZE)
            for path, record in before.files.items():
                if path == self.edited:
                    continue
                other = after.files[path]
                left = vw.read_at(source, vw.VOLUME_OFFSET + record.offset,
                                  record.stored_size, vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
                right = vw.read_at(stream, vw.VOLUME_OFFSET + other.offset,
                                   other.stored_size, vw.VOLUME_OFFSET + vw.VOLUME_SIZE)
                self.assertEqual(left, right, path)
                self.assertEqual(record.size, other.size, path)


class RefusalTests(unittest.TestCase):
    def test_unsafe_paths_are_refused(self):
        for path in ("../etc/passwd", "/absolute", "a//b", "a/../b", "a\\b"):
            with self.assertRaises(folder_tree.FolderError):
                folder_tree.safe_path(path)

    def test_an_existing_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / folder_tree.MANIFEST).write_text("{}")
            with self.assertRaises(folder_tree.FolderError):
                folder_tree.extract(Path("missing.iso"), root)


if __name__ == "__main__":
    unittest.main()
