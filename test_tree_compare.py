from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
import unittest
from unittest.mock import patch

from last_paths import load_last_paths, save_last_paths
from server import CompareApp, aligned_lines, read_text
from tree_compare import compare_trees, is_ignored, load_ignore_patterns


class TreeCompareTests(unittest.TestCase):
    def test_recursive_comparison_and_empty_folders(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            left, right = base / "left", base / "right"
            for root in (left, right):
                (root / "nested").mkdir(parents=True)
                (root / "nested" / "same.txt").write_text("same", encoding="utf-8")
                (root / "nested" / "changed.txt").write_text("left" if root == left else "right", encoding="utf-8")
            (left / "empty").mkdir()
            (right / "different_empty").mkdir()
            (left / "only.txt").write_text("a")
            (right / "only_right.txt").write_text("b")
            status = {entry.relative_path: entry.status for entry in compare_trees(left, right)}
            self.assertEqual(status["nested"], "Different")
            self.assertEqual(status["nested/same.txt"], "Same")
            self.assertEqual(status["nested/changed.txt"], "Different")
            self.assertEqual(status["empty"], "Left only")
            self.assertEqual(status["different_empty"], "Right only")
            self.assertEqual(status["only.txt"], "Left only")
            self.assertEqual(status["only_right.txt"], "Right only")

    def test_type_mismatch_and_same_size_content_change(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            left, right = base / "left", base / "right"
            left.mkdir()
            right.mkdir()
            (left / "item").mkdir()
            (right / "item").write_text("x")
            (left / "bytes.bin").write_bytes(b"abc")
            (right / "bytes.bin").write_bytes(b"abd")
            status = {entry.relative_path: entry.status for entry in compare_trees(left, right)}
            self.assertEqual(status["item"], "Different type")
            self.assertEqual(status["bytes.bin"], "Different")

    def test_same_root_rejected(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                compare_trees(temporary, temporary)

    def test_text_preview_and_alignment(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "utf16.txt"
            path.write_bytes("one\ntwo".encode("utf-16"))
            self.assertEqual(read_text(path), (["one", "two"], ""))
            self.assertEqual(list(aligned_lines(["a", "b"], ["a", "c"])),
                             [("a", "a", "equal"), ("b", "c", "replace")])

    def test_line_endings_and_final_newline_are_ignored(self):
        with TemporaryDirectory() as temporary:
            left, right = Path(temporary) / "left", Path(temporary) / "right"
            left.mkdir()
            right.mkdir()
            (left / "sample.txt").write_bytes(b"same\r\nlast\r\n")
            (right / "sample.txt").write_bytes(b"same\nlast")
            self.assertEqual(compare_trees(left, right)[0].status, "Same")
            a, error_a = read_text(left / "sample.txt")
            b, error_b = read_text(right / "sample.txt")
            self.assertEqual((error_a, error_b), ("", ""))
            rows = list(aligned_lines(a, b))
            self.assertEqual([row[2] for row in rows], ["equal", "equal"])

    def test_content_change_still_detected_with_mixed_line_endings(self):
        with TemporaryDirectory() as temporary:
            left, right = Path(temporary) / "left", Path(temporary) / "right"
            left.mkdir()
            right.mkdir()
            (left / "sample.txt").write_bytes(b"same\r\nold\r\n")
            (right / "sample.txt").write_bytes(b"same\nnew\n")
            self.assertEqual(compare_trees(left, right)[0].status, "Different")
            a, _ = read_text(left / "sample.txt")
            b, _ = read_text(right / "sample.txt")
            self.assertEqual([row[2] for row in aligned_lines(a, b)], ["equal", "replace"])

    def test_cr_only_and_utf16_line_endings_are_ignored(self):
        with TemporaryDirectory() as temporary:
            left, right = Path(temporary) / "left", Path(temporary) / "right"
            left.mkdir()
            right.mkdir()
            (left / "legacy.txt").write_bytes(b"one\rtwo\r")
            (right / "legacy.txt").write_bytes(b"one\ntwo")
            (left / "wide.txt").write_bytes("one\r\ntwo\r\n".encode("utf-16"))
            (right / "wide.txt").write_bytes("one\ntwo".encode("utf-16"))
            self.assertEqual({entry.status for entry in compare_trees(left, right)}, {"Same"})

    def test_binary_newline_bytes_are_not_normalized(self):
        with TemporaryDirectory() as temporary:
            left, right = Path(temporary) / "left", Path(temporary) / "right"
            left.mkdir()
            right.mkdir()
            (left / "data.bin").write_bytes(b"\x00a\r\nb")
            (right / "data.bin").write_bytes(b"\x00a\nb")
            self.assertEqual(compare_trees(left, right)[0].status, "Different")

    def test_preview_applies_color_tags_to_changed_lines(self):
        try:
            app = CompareApp()
        except tk.TclError as exc:
            self.skipTest(f"GUI unavailable: {exc}")
        try:
            app.withdraw()
            with TemporaryDirectory() as temporary:
                left, right = Path(temporary) / "left", Path(temporary) / "right"
                left.mkdir()
                right.mkdir()
                (left / "sample.txt").write_text("same\nold\n", encoding="utf-8")
                (right / "sample.txt").write_text("same\nnew\n", encoding="utf-8")
                app.entries = compare_trees(left, right)
                app.by_path = {entry.relative_path: entry for entry in app.entries}
                app._fill_tree()
                app.tree.selection_set("sample.txt")
                app._show_selection()
                self.assertEqual(tuple(map(str, app.left_text.tag_ranges("replace"))),
                                 ("2.0", "3.0"))
                self.assertEqual(tuple(map(str, app.right_text.tag_ranges("replace"))),
                                 ("2.0", "3.0"))
                self.assertNotEqual(app.left_text.tag_cget("replace", "background"),
                                    app.right_text.tag_cget("replace", "background"))
        finally:
            app.destroy()

    def test_use_last_restores_paths_saved_on_compare_click(self):
        with TemporaryDirectory() as temporary:
            storage = Path(temporary) / "state" / "last_paths.json"
            try:
                app = CompareApp(storage_path=storage)
            except tk.TclError as exc:
                self.skipTest(f"GUI unavailable: {exc}")
            try:
                app.withdraw()
                app.left_var.set("C:/first folder")
                app.right_var.set("D:/second folder")
                with patch("server.threading.Thread"):
                    app.compare_button.invoke()
                self.assertEqual(load_last_paths(storage), ("C:/first folder", "D:/second folder"))
            finally:
                app.destroy()
            restored = CompareApp(storage_path=storage)
            try:
                restored.withdraw()
                restored.use_last_button.invoke()
                self.assertEqual(restored.left_var.get(), "C:/first folder")
                self.assertEqual(restored.right_var.get(), "D:/second folder")
            finally:
                restored.destroy()

    def test_last_paths_rejects_invalid_saved_data(self):
        with TemporaryDirectory() as temporary:
            storage = Path(temporary) / "last_paths.json"
            self.assertIsNone(load_last_paths(storage))
            storage.write_text("not json", encoding="utf-8")
            self.assertIsNone(load_last_paths(storage))
            save_last_paths("left", "right", storage)
            self.assertEqual(load_last_paths(storage), ("left", "right"))

    def test_ignore_file_skips_matching_files_and_folders_on_both_sides(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            left, right = base / "left", base / "right"
            for root in (left, right):
                (root / "src" / "node_modules").mkdir(parents=True)
                (root / "src" / "node_modules" / "many.txt").write_text("different")
                (root / "cache.pyc").write_bytes(b"compiled")
                (root / "Main.class").write_bytes(b"compiled")
                (root / "library.jar").write_bytes(b"compiled")
                (root / "keep.txt").write_text("left" if root == left else "right")
            ignore_file = base / "ignore.txt"
            ignore_file.write_text("node_modules/\n*.pyc\n*.class\n*.jar\n", encoding="utf-8")
            paths = {entry.relative_path for entry in compare_trees(left, right, ignore_file)}
            self.assertEqual(paths, {"src", "keep.txt"})

    def test_ignore_rules_support_comments_and_custom_paths(self):
        with TemporaryDirectory() as temporary:
            ignore_file = Path(temporary) / "ignore.txt"
            ignore_file.write_text("# comment\n\nlogs/*.log\nnode_modules/\n", encoding="utf-8")
            patterns = load_ignore_patterns(ignore_file)
            self.assertEqual(patterns, ("logs/*.log", "node_modules/"))
            self.assertTrue(is_ignored("logs/output.log", False, patterns))
            self.assertFalse(is_ignored("other/output.log", False, patterns))
            self.assertTrue(is_ignored("src/node_modules", True, patterns))
            self.assertFalse(is_ignored("node_modules", False, patterns))


if __name__ == "__main__":
    unittest.main()
