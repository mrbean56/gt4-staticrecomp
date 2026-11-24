"""Controls for the disc-to-folder extractor (TASK-220)."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gt4_disc_extract as extractor


class FolderTests(unittest.TestCase):
    def test_fresh_directory_refuses_a_non_empty_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "disc"
            path.mkdir()
            (path / "leftover.bin").write_bytes(b"x")
            with self.assertRaises(extractor.ExtractError):
                extractor.fresh_directory(path)

    def test_fresh_directory_refuses_a_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "disc"
            path.write_bytes(b"x")
            with self.assertRaises(extractor.ExtractError):
                extractor.fresh_directory(path)

    def test_fresh_directory_creates_nested_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a" / "b"
            self.assertEqual(extractor.fresh_directory(path), path)
            self.assertTrue(path.is_dir())

    def test_target_path_strips_the_version_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = extractor.target_path(Path(temporary), "/CORE.GT4;1")
            self.assertEqual(target.name, "CORE.GT4")
            self.assertEqual(target.parent, Path(temporary))

    def test_target_path_refuses_traversal(self):
        for name in ("/../escape.bin;1", "/a/../../escape.bin;1"):
            with self.assertRaises(extractor.ExtractError):
                extractor.target_path(Path("/tmp"), name)

    def test_verify_folder_reports_a_wrong_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "CORE.GT4").write_bytes(b"short")
            pin = {"files": {"/CORE.GT4;1": {"size_bytes": 5,
                                             "sha256": "0" * 64}}}
            with self.assertRaises(extractor.ExtractError):
                extractor.verify_folder(root, pin)

    def test_verify_folder_reports_a_wrong_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SYSTEM.CNF").write_bytes(b"BOOT2 = x\n")
            pin = {"files": {"/SYSTEM.CNF;1": {"size_bytes": 11,
                                               "sha256": "0" * 64}}}
            with self.assertRaises(extractor.ExtractError):
                extractor.verify_folder(root, pin)


if __name__ == "__main__":
    unittest.main()
