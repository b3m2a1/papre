import contextlib
import io
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import Mock, patch

from papre.preview import stop_process_tree
from papre.server import main
from papre.system import available_tools, default_state_dir


class SystemTests(unittest.TestCase):
    def test_state_paths_do_not_depend_on_installation_directory(self):
        home = Path("/example/home")
        with patch("papre.system.Path.home", return_value=home), patch.dict(os.environ, {}, clear=True):
            with patch("papre.system.sys.platform", "linux"):
                self.assertEqual(default_state_dir(), home / ".local/state/papre")
                with patch.dict(os.environ, {"XDG_STATE_HOME": "/example/state"}):
                    self.assertEqual(default_state_dir(), Path("/example/state/papre"))
            with patch("papre.system.sys.platform", "darwin"):
                self.assertEqual(default_state_dir(), home / "Library/Application Support/papre")
            with patch("papre.system.sys.platform", "win32"):
                self.assertEqual(default_state_dir(), home / "AppData/Local/papre")
                with patch.dict(os.environ, {"LOCALAPPDATA": "/example/local"}):
                    self.assertEqual(default_state_dir(), Path("/example/local/papre"))

    def test_detected_engines_and_renderer(self):
        paths = {name: "/tools/" + name for name in ["git", "latexmk", "xelatex", "pdftoppm"]}
        with patch("papre.system.shutil.which", side_effect=paths.get):
            tools = available_tools()
        self.assertTrue(tools["can_compile"])
        self.assertEqual(tools["engines"], ["xelatex"])
        self.assertEqual(tools["renderer_path"], "/tools/pdftoppm")
        self.assertEqual(tools["renderer"], "pdftoppm")

    def test_missing_tools_and_windows_ghostscript(self):
        for paths in [{}, {"pdflatex": "/tools/pdflatex"}, {"latexmk": "/tools/latexmk"}]:
            with self.subTest(paths=paths), patch("papre.system.shutil.which", side_effect=paths.get):
                self.assertFalse(available_tools()["can_compile"])
        with patch("papre.system.shutil.which", side_effect={"gswin64c": "/tools/gswin64c.exe"}.get), \
             patch("papre.system.sys.platform", "win32"):
            tools = available_tools()
            self.assertEqual(tools["renderer"], "gs")
            self.assertEqual(tools["renderer_path"], "/tools/gswin64c.exe")
            self.assertIn("windows", tools["installation"]["url"])

    def test_dependency_check_runs_without_creating_a_server_or_state(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["papre", "--check"]), \
             patch("papre.system.shutil.which", return_value=None), \
             patch("papre.server.ReviewHTTPServer") as server, \
             patch("papre.server.ReviewStore") as store, contextlib.redirect_stdout(output):
            main()
        server.assert_not_called()
        store.assert_not_called()
        self.assertIn("Paper Patch Review Editor", output.getvalue())
        self.assertIn("PDF preview needs latexmk", output.getvalue())

    def test_posix_compiler_timeout_stops_process_group(self):
        process = Mock(pid=123)
        with patch("papre.preview.sys.platform", "linux"), patch("papre.preview.os.killpg") as kill:
            stop_process_tree(process)
        kill.assert_called_once_with(123, signal.SIGKILL)
        process.wait.assert_called_once()

    def test_windows_compiler_timeout_stops_child_tree_and_falls_back(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch("papre.preview.sys.platform", "win32"), patch("papre.preview.subprocess.run") as run:
            stop_process_tree(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "123", "/T", "/F"])
        process.kill.assert_called_once()
        process.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
