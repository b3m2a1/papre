from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from papre.core import ReviewError
from papre.picker import MAC_DIALOG, pick_directory


class PickerTests(unittest.TestCase):
    @patch("papre.picker.sys.platform", "darwin")
    @patch("papre.picker.shutil.which", return_value="/usr/bin/osascript")
    @patch("papre.picker.subprocess.run")
    def test_macos_selection_is_an_argument_not_script_text(self, run, which):
        start = Path('/folder/with "quotes" and spaces')
        run.return_value = subprocess.CompletedProcess([], 0, "/chosen manuscript/\n", "")
        self.assertEqual(pick_directory(start), Path("/chosen manuscript"))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/osascript", "-e", MAC_DIALOG, str(start)])
        self.assertNotIn(str(start), MAC_DIALOG)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("papre.picker.sys.platform", "darwin")
    @patch("papre.picker.shutil.which", return_value="/usr/bin/osascript")
    @patch("papre.picker.subprocess.run")
    def test_cancel_and_failure_are_different(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, "\n", "")
        self.assertIsNone(pick_directory(Path("/")))
        run.return_value = subprocess.CompletedProcess([], 1, "", "system error")
        with self.assertRaisesRegex(ReviewError, "Cannot open"):
            pick_directory(Path("/"))
        run.side_effect = subprocess.TimeoutExpired("osascript", 300)
        with self.assertRaisesRegex(ReviewError, "timed out"):
            pick_directory(Path("/"))

    @patch("papre.picker.sys.platform", "win32")
    @patch("papre.picker.shutil.which", return_value="powershell.exe")
    @patch("papre.picker.subprocess.run")
    def test_windows_path_is_passed_as_data(self, run, which):
        start = Path('/folder/$(not code)')
        run.return_value = subprocess.CompletedProcess([], 0, '"/chosen folder"\n', "")
        self.assertEqual(pick_directory(start), Path("/chosen folder"))
        self.assertEqual(run.call_args.kwargs["env"]["PAPRE_PICKER_START"], str(start))
        self.assertNotIn(str(start), run.call_args.args[0])
        self.assertIn("-STA", run.call_args.args[0])

    @patch("papre.picker.sys.platform", "linux")
    @patch("papre.picker.shutil.which", return_value=None)
    @patch("papre.picker.subprocess.run")
    def test_tk_runs_in_its_own_main_thread(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, '""\n', "")
        self.assertIsNone(pick_directory(Path("/")))
        self.assertEqual(run.call_args.args[0][1:], ["-m", "papre.picker", "/"])

    @patch("papre.picker.sys.platform", "linux")
    @patch("papre.picker.shutil.which", side_effect=lambda name: "/usr/bin/zenity" if name == "zenity" else None)
    @patch("papre.picker.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", ""))
    def test_linux_cancel(self, run, which):
        self.assertIsNone(pick_directory(Path("/")))


if __name__ == "__main__":
    unittest.main()
