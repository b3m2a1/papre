"""Portable runtime paths and installed-tool discovery."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

ENGINES = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}


def default_state_dir() -> Path:
    """Keep writable review data outside both installed code and manuscripts."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "papre"


def installation_help() -> dict:
    if sys.platform == "darwin":
        return {"title": "Install MacTeX", "url": "https://tug.org/mactex/mactex-download.html",
                "text": "Install MacTeX with TeX Live, then open a new terminal and restart papre. "
                        "A smaller TeX installation must also include latexmk and the packages your manuscript uses."}
    if sys.platform == "win32":
        return {"title": "Install TeX Live", "url": "https://tug.org/texlive/windows.html",
                "text": "Install TeX Live for Windows with latexmk and at least one LaTeX engine. "
                        "Allow the installer to add its binaries to PATH, then open a new terminal and restart papre."}
    return {"title": "Install TeX Live", "url": "https://tug.org/texlive/quickinstall.html",
            "text": "Install TeX Live through your system package manager or the TeX Live installer. "
                    "Include latexmk and at least one LaTeX engine, add its binaries to PATH, and restart papre."}


def available_tools() -> dict:
    latexmk = shutil.which("latexmk")
    engine_paths = {name: path for name in ENGINES if (path := shutil.which(name))}
    renderer_path = next((path for name in ("pdftoppm", "gs", "gswin64c", "gswin32c")
                          if (path := shutil.which(name))), None)
    renderer = "pdftoppm" if renderer_path and Path(renderer_path).stem.lower() == "pdftoppm" else "gs" if renderer_path else None
    return {"git": shutil.which("git"), "latexmk": latexmk, "engines": list(engine_paths),
            "engine_paths": engine_paths, "renderer": renderer, "renderer_path": renderer_path,
            "synctex": shutil.which("synctex"),
            "can_compile": bool(latexmk and engine_paths), "installation": installation_help()}


def print_tool_status():
    tools = available_tools()
    print("Paper Patch Review Editor (papre): dependency check")
    print("Git: " + (tools["git"] or "not found; install Git and add it to PATH"))
    print("latexmk: " + (tools["latexmk"] or "not found"))
    for engine in ENGINES:
        print(engine + ": " + (tools["engine_paths"].get(engine) or "not found"))
    print("PDF renderer: " + (tools["renderer_path"] or "not found; using the browser PDF viewer"))
    print("PDF source navigation: " + (tools["synctex"] or "synctex not found; page locations unavailable"))
    if not tools["can_compile"]:
        print("PDF preview needs latexmk and at least one detected LaTeX engine.")
        print(tools["installation"]["text"])
        print(tools["installation"]["url"])
    print("Default review state: " + str(default_state_dir()))
