"""Tests for the pure-logic pieces of sdb.py.

Runs head-less: no QApplication is created, no window is opened. The CLI
tests only exercise the text/report paths, and the ApplicationManager
tests use a stubbed ScriptDatabaseWindow so the singleton lifecycle is
verified without touching real Qt widgets.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdb  # noqa: E402


def write(tmpdir, name, text):
    path = os.path.join(str(tmpdir), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class RunCliArgumentHandlingTests(unittest.TestCase):
    def test_missing_files_prints_help_and_returns_zero(self):
        with mock.patch.object(sdb, "argparse") as _:
            pass  # argparse is only patched out if needed; keep real one
        # Use argv with no positional files: run_cli should print help.
        import io, contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sdb.run_cli(["--no-gui"])
        self.assertEqual(code, 0)
        self.assertIn("diffgui", buffer.getvalue())

    def test_missing_left_file_returns_error(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            right = write(tmp, "right.txt", "b\n")
            import io, contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = sdb.run_cli(["nonexistent_left.txt", right, "--no-gui"])
            self.assertEqual(code, 1)
            self.assertIn("left file does not exist", err.getvalue())

    def test_missing_right_file_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            left = write(tmp, "left.txt", "a\n")
            import io, contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = sdb.run_cli([left, "nonexistent_right.txt", "--no-gui"])
            self.assertEqual(code, 1)
            self.assertIn("right file does not exist", err.getvalue())

    def test_text_format_returns_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            left = write(tmp, "left.txt", "a\nb\n")
            right = write(tmp, "right.txt", "a\nc\n")
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sdb.run_cli([left, right, "--no-gui", "--format", "text"])
            self.assertEqual(code, 0)
            self.assertTrue(out.getvalue().strip())

    def test_report_with_bad_extension_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            left = write(tmp, "left.txt", "a\n")
            right = write(tmp, "right.txt", "b\n")
            import io, contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = sdb.run_cli([left, right, "--no-gui", "--report", "out.exe"])
            self.assertEqual(code, 1)
            self.assertIn("report file must have", err.getvalue())

    def test_report_json_is_written(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            left = write(tmp, "left.txt", "a\nb\n")
            right = write(tmp, "right.txt", "a\nc\n")
            report_path = os.path.join(tmp, "report.json")
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sdb.run_cli([left, right, "--no-gui", "--report", report_path])
            self.assertEqual(code, 0)
            self.assertIn("report written", out.getvalue())
            with open(report_path, encoding="utf-8") as handle:
                json.load(handle)

    def test_stats_flag_prints_statistics(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            left = write(tmp, "left.txt", "a\nb\n")
            right = write(tmp, "right.txt", "a\nc\n")
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sdb.run_cli([left, right, "--no-gui", "--stats"])
            self.assertEqual(code, 0)
            self.assertTrue(out.getvalue().strip())

    def test_format_report_unified_and_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            left = write(tmp, "left.txt", "a\nb\n")
            right = write(tmp, "right.txt", "a\nc\n")
            report = sdb.compare_files(left, right, "myers", "text")
            self.assertIsInstance(sdb.format_report(report, "unified"), str)
            self.assertIsInstance(sdb.format_report(report, "json"), str)


class ApplicationManagerTests(unittest.TestCase):
    def _make_window_stub(self):
        calls = []

        def hide():
            calls.append("hide")

        def deleteLater():
            calls.append("deleteLater")

        stub = types.SimpleNamespace(hide=hide, deleteLater=deleteLater)
        return stub, calls

    def test_singleton_returns_same_instance(self):
        sdb.ApplicationManager._instance = None
        sdb.ApplicationManager._main_window = None
        sdb.ApplicationManager._app = None
        self.assertIs(sdb.ApplicationManager(), sdb.ApplicationManager())

    def test_shutdown_clears_class_state(self):
        manager = sdb.ApplicationManager()
        stub, calls = self._make_window_stub()
        app = types.SimpleNamespace(quit=lambda: None, processEvents=lambda: None)
        sdb.ApplicationManager._main_window = stub
        sdb.ApplicationManager._app = app
        manager.shutdown()
        self.assertIsNone(sdb.ApplicationManager._main_window)
        self.assertIsNone(sdb.ApplicationManager._app)
        self.assertEqual(calls, ["hide", "deleteLater"])

    def test_reset_produces_clean_state(self):
        manager = sdb.ApplicationManager()
        stub, _ = self._make_window_stub()
        sdb.ApplicationManager._main_window = stub
        sdb.ApplicationManager._app = types.SimpleNamespace(
            quit=lambda: None, processEvents=lambda: None)
        manager.reset()
        self.assertIsNone(sdb.ApplicationManager._instance)
        self.assertIsNone(sdb.ApplicationManager._main_window)
        self.assertIsNone(sdb.ApplicationManager._app)
        # A fresh singleton is created after reset.
        self.assertIsNot(sdb.ApplicationManager(), manager)

    def test_shutdown_reraises_errors(self):
        manager = sdb.ApplicationManager()

        def boom():
            raise RuntimeError("teardown failed")

        sdb.ApplicationManager._main_window = types.SimpleNamespace(hide=boom)
        with self.assertRaises(RuntimeError):
            manager.shutdown()
        self.assertIsNone(sdb.ApplicationManager._main_window)


if __name__ == "__main__":
    unittest.main()
