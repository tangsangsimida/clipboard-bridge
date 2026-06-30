"""Unit tests for clipboard-sync.py."""

import importlib
import sys
import unittest

sys.path.insert(0, ".")
_mod = importlib.import_module("clipboard-sync")

fast_hash = _mod.fast_hash
ClipState = _mod.ClipState
SyncDirection = _mod.SyncDirection
detect_x11_files = _mod.detect_x11_files
detect_x11_image = _mod.detect_x11_image
resolve_file_path = _mod.resolve_file_path


class TestFastHash(unittest.TestCase):
    def test_empty(self):
        h = fast_hash(b"")
        self.assertTrue(h.startswith("0:"))

    def test_consistent(self):
        self.assertEqual(fast_hash(b"test"), fast_hash(b"test"))

    def test_different(self):
        self.assertNotEqual(fast_hash(b"a"), fast_hash(b"b"))

    def test_length_prefix(self):
        h = fast_hash(b"hello")
        self.assertTrue(h.startswith("5:"))


class TestSyncDirection(unittest.TestCase):
    def test_values(self):
        self.assertEqual(SyncDirection.NONE.value, "")
        self.assertEqual(SyncDirection.X11_TO_WL.value, "x2w")
        self.assertEqual(SyncDirection.WL_TO_X11.value, "w2x")


class TestResolveFilePath(unittest.TestCase):
    def test_absolute(self):
        self.assertEqual(resolve_file_path("/etc/hosts"), "file:///etc/hosts")

    def test_nonexistent(self):
        self.assertIsNone(resolve_file_path("/nonexistent/path"))

    def test_relative(self):
        result = resolve_file_path(".bashrc")
        if result is not None:
            self.assertTrue(result.startswith("file://"))


class TestDetectX11Files(unittest.TestCase):
    def test_single_file(self):
        self.assertEqual(detect_x11_files("/etc/hosts"), "file:///etc/hosts")

    def test_multiple_files(self):
        result = detect_x11_files("/etc/hosts\n/etc/fstab")
        self.assertEqual(result, "file:///etc/hosts\nfile:///etc/fstab")

    def test_non_file(self):
        self.assertIsNone(detect_x11_files("hello world"))

    def test_empty(self):
        self.assertIsNone(detect_x11_files(""))


class TestDetectX11Image(unittest.TestCase):
    def test_png(self):
        self.assertEqual(detect_x11_image(["image/png"]), "image/png")

    def test_jpeg(self):
        self.assertEqual(detect_x11_image(["image/jpeg"]), "image/jpeg")

    def test_no_image(self):
        self.assertIsNone(detect_x11_image(["text/plain"]))

    def test_empty(self):
        self.assertIsNone(detect_x11_image([]))


class TestEnvFloat(unittest.TestCase):
    def test_default(self):
        self.assertEqual(_mod._env_float("NONEXISTENT_VAR", 1.5), 1.5)

    def test_valid(self):
        import os
        os.environ["TEST_CB_VAL"] = "2.5"
        self.assertEqual(_mod._env_float("TEST_CB_VAL", 1.0), 2.5)
        del os.environ["TEST_CB_VAL"]

    def test_invalid(self):
        import os
        os.environ["TEST_CB_VAL"] = "abc"
        self.assertEqual(_mod._env_float("TEST_CB_VAL", 1.0), 1.0)
        del os.environ["TEST_CB_VAL"]

    def test_below_min(self):
        import os
        os.environ["TEST_CB_VAL"] = "0.01"
        self.assertEqual(_mod._env_float("TEST_CB_VAL", 1.0, min_val=0.1), 1.0)
        del os.environ["TEST_CB_VAL"]


if __name__ == "__main__":
    unittest.main()
