"""Open a local desktop's native folder dialog without adding a runtime dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .core import ReviewError

MAC_DIALOG = '''on run argv
    try
        set selectedFolder to choose folder with prompt "Choose a Git repository" default location (POSIX file (item 1 of argv))
        return POSIX path of selectedFolder
    on error number -128
        return ""
    end try
end run'''

WINDOWS_DIALOG = '''Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose a Git repository'
$dialog.SelectedPath = $env:PAPRE_PICKER_START
$dialog.ShowNewFolderButton = $false
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        ConvertTo-Json -Compress -InputObject $dialog.SelectedPath
    }
} finally { $dialog.Dispose() }'''


def pick_directory(start: Path) -> Path | None:
    """Return a selection, or None on cancellation. Run UI outside HTTP threads."""
    env = None
    json_output = False
    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        command = [executable, "-e", MAC_DIALOG, str(start)] if executable else None
    elif sys.platform == "win32":
        executable = shutil.which("powershell.exe")
        env = dict(os.environ, PAPRE_PICKER_START=str(start))
        command = [executable, "-NoProfile", "-STA", "-Command", WINDOWS_DIALOG] if executable else None
        json_output = True
    elif shutil.which("zenity"):
        command = [shutil.which("zenity"), "--file-selection", "--directory", "--title=Choose a Git repository",
                   "--filename=" + str(start) + os.sep]
    elif shutil.which("kdialog"):
        command = [shutil.which("kdialog"), "--getexistingdirectory", str(start), "--title", "Choose a Git repository"]
    else:
        # Tk must own its process's main thread, especially on desktop platforms.
        command = [sys.executable, "-m", "papre.picker", str(start)]
        json_output = True
    if command is None:
        raise ReviewError("The system folder picker is unavailable. Enter a path or browse folders in this page.", 503)
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise ReviewError("The folder picker timed out. Try again or enter a path.", 503) from exc
    except OSError as exc:
        raise ReviewError("Cannot open the system folder picker. Enter a path or browse folders in this page.", 503) from exc
    # Zenity and KDialog use exit status 1 for Cancel.
    if result.returncode == 1 and Path(command[0]).name in {"zenity", "kdialog"}:
        return None
    if result.returncode:
        raise ReviewError("Cannot open the system folder picker. Enter a path or browse folders in this page.", 503)
    selection = result.stdout.strip()
    if not selection:
        return None
    if json_output:
        try:
            selection = json.loads(selection)
        except json.JSONDecodeError as exc:
            raise ReviewError("The system folder picker returned an invalid selection.", 503) from exc
    if not isinstance(selection, str):
        raise ReviewError("The system folder picker returned an invalid selection.", 503)
    return Path(selection).expanduser().resolve() if selection else None


def main():
    """Optional Tk dialog for systems without an existing desktop picker command."""
    import tkinter
    from tkinter import filedialog

    window = tkinter.Tk()
    window.withdraw()
    try:
        folder = filedialog.askdirectory(title="Choose a Git repository", initialdir=sys.argv[1], mustexist=True)
        print(json.dumps(folder), flush=True)
    finally:
        window.destroy()


if __name__ == "__main__":
    main()
