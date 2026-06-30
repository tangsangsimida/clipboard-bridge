"""Unit tests for clipboard-sync.py pure functions."""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, ".")
_mod = importlib.import_module("clipboard-sync")
detect_x11_files = _mod.detect_x11_files
detect_x11_image = _mod.detect_x11_image
resolve_file_path = _mod.resolve_file_path


class TestResolveFilePath(unittest.TestCase):
    """Tests for resolve_file_path()."""

    def test_absolute_path(self):
        result = resolve_file_path("/etc/hosts")
        self.assertEqual(result, "file:///etc/hosts")

    def test_relative_path(self):
        # ~/.bashrc should exist on most systems
        result = resolve_file_path(".bashrc")
        if result is not None:
            self.assertTrue(result.startswith("file://"))

    def test_nonexistent_path(self):
        result = resolve_file_path("/nonexistent/path/that/does/not/exist")
        self.assertIsNone(result)

    def test_nonexistent_relative_path(self):
        result = resolve_file_path("nonexistent_file_12345")
        self.assertIsNone(result)


class TestDetectX11Files(unittest.TestCase):
    """Tests for detect_x11_files()."""

    def test_single_file(self):
        result = detect_x11_files("/etc/hosts")
        self.assertEqual(result, "file:///etc/hosts")

    def test_multiple_files(self):
        result = detect_x11_files("/etc/hosts\n/etc/fstab")
        self.assertEqual(result, "file:///etc/hosts\nfile:///etc/fstab")

    def test_non_file_text(self):
        result = detect_x11_files("hello world")
        self.assertIsNone(result)

    def test_empty_string(self):
        result = detect_x11_files("")
        self.assertIsNone(result)

    def test_mixed_file_and_text(self):
        result = detect_x11_files("/etc/hosts\nnot_a_real_file_xyz")
        self.assertIsNone(result)


class TestDetectX11Image(unittest.TestCase):
    """Tests for detect_x11_image()."""

    def test_png(self):
        result = detect_x11_image(["TARGETS", "UTF8_STRING", "image/png"])
        self.assertEqual(result, "image/png")

    def test_jpeg(self):
        result = detect_x11_image(["image/jpeg", "UTF8_STRING"])
        self.assertEqual(result, "image/jpeg")

    def test_no_image(self):
        result = detect_x11_image(["TARGETS", "UTF8_STRING"])
        self.assertIsNone(result)

    def test_empty_targets(self):
        result = detect_x11_image([])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
