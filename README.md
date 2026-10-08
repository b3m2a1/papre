# Paper Patch Review Editor

**Paper Patch Review Editor** (`papre`) is a local browser interface for reviewing patches from an existing LLM chat before they change a scientific LaTeX manuscript. The white split viewer shows original source on the left, updates on the right, removals in red, and additions in green. Shared custom components keep controls and viewers consistent. There is no frontend build step, CDN dependency, or LLM provider connection.

Python 3.11+ serves the UI and API using only its standard library. Git is required. PDF compilation is optional and requires a TeX installation. For LLM assistance, point the agent to [AGENTS.md](AGENTS.md); it covers app conventions, manuscript patch preparation, and review safeguards.

## Install from GitHub

Use Python 3.11 or newer and install Git. The commands below assume `python` refers to your chosen Python interpreter. Create and activate a virtual environment if you do not already have one:

```bash
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS/Linux, or `.venv\Scripts\Activate.ps1` in Windows PowerShell. Then install from the GitHub repository:

```bash
python -m pip install "git+https://github.com/OWNER/REPOSITORY.git"
```

Replace `OWNER/REPOSITORY` with the repository containing this project's `pyproject.toml`. To select a particular release or commit, append `@REF` to the Git URL. If this project is inside a larger repository, append `#subdirectory=path/to/papre`. These are standard [pip Git installation formats](https://pip.pypa.io/en/stable/topics/vcs-support/).

The distribution name, import package, and command are all `papre`. Installing the Python package includes the browser UI and demo manuscript; it does not install Git, a TeX distribution, or browser test tools.

```bash
papre --check
papre
```

Open <http://127.0.0.1:8765/>. `python -m papre` accepts the same options if the console command is not on PATH. `papre --help` lists the options.

Open the HTTP address printed by the running app, rather than opening `papre/static/index.html` as a file. The styles, JavaScript modules, and repository API are served by the Python process. A `file://` page cannot load them correctly and may report missing resources or CORS errors. Keep the app process running while using the interface.

## Choose a repository

Click **Choose repository**, then **Browse…** to open your operating system's folder picker. Select the root of your manuscript's Git repository and click **Attach repository**. You can also enter a path (including `~` for your home directory) and click **Go** or press Enter. **Browse folders in this page** provides a fallback with folder navigation and an **Up** button.

The picker starts at the attached repository or the app's launch directory. It can navigate to other local directories by default. Use `--browse-root /path/to/folder` only when you want to restrict repository selection to that folder and its descendants; the restriction applies to both pickers. Choosing a folder does not attach it or create queue folders until you click **Attach repository**.

The native dialog uses macOS's folder chooser or Windows's folder dialog. On Linux it uses Zenity, KDialog, or Python's optional Tk support, when available. If a desktop picker is unavailable (for example, on a server without a graphical desktop), enter the path or use the in-page browser. No extra Python dependency is required for patch review.

Attaching creates `review_queue`, `review_queue/processed`, and `review_queue/superseded` if needed. Check **Enable Apply and Undo** when you want those buttons to change manuscript source. Importing, editing, and reviewing write queue artifacts and review state; manuscript source changes only when you explicitly click **Apply accepted changes** or **Undo apply**. The app does not stage, commit, pull, push, or edit `.gitignore`.

Attach a repository at launch:

```bash
papre --repo /path/to/manuscript --main main.tex --allow-write --port 8765
```

Omit `--allow-write` to disable manuscript Apply/Undo. With `--repo`, folder selection starts at that repository. The example path is a placeholder; use an appropriate path for your operating system.

For a demonstration:

```bash
papre --demo
```

This creates a Git repository containing synthetic scientific examples and queues three manuscript edits.

The [example patch](papre/demo/example.patch) targets the bundled [demo manuscript](papre/demo/main.tex). It clarifies three passages without changing its numerical values or equations. In the demo, the patch is already queued: review each change, or choose **Accept pending**, then **Apply accepted changes**. For another repository, generate a patch against that repository's current source instead.

Runtime files are stored in a user data directory, independently of the installed package:

| Operating system | Default state directory |
| --- | --- |
| Linux/Unix | `$XDG_STATE_HOME/papre`, or `~/.local/state/papre` |
| macOS | `~/Library/Application Support/papre` |
| Windows | `%LOCALAPPDATA%\papre`, or `~\AppData\Local\papre` |

`--state-dir /path/to/state` overrides the parent directory for all runtime data. Repository reviews live in `repositories/<repository-key>/`; the demo lives in `demo-repo/`. Keep the state directory outside manuscript repositories and retain it to resume review decisions. Starting without `--repo` opens the picker again; reattaching the same repository restores its history. No writable files are stored in the package installation directory.

## PDF preview setup

Run `papre --check` to report the detected Git executable, `latexmk`, each supported LaTeX engine, and the PDF renderer. The app detects executables on PATH at startup, when refreshing the session, and before compiling. Available engines are enabled in the preview selector; unavailable engines are disabled. The preview panel displays detection results and links to setup instructions if tools are missing. Patch review works without a TeX installation.

PDF compilation requires **latexmk** and at least one of **pdflatex**, **xelatex**, or **lualatex**. Install the distribution and document packages you need:

| Operating system | Installation |
| --- | --- |
| macOS | Download and run the [MacTeX installer](https://tug.org/mactex/mactex-download.html), including TeX Live. |
| Linux/Unix | Use your system's TeX Live packages or follow the [TeX Live installation instructions](https://tug.org/texlive/quickinstall.html); include `latexmk` and the selected engine. |
| Windows | Download and run the [TeX Live Windows installer](https://tug.org/texlive/windows.html), including `latexmk` and an engine, and let it add its binaries to PATH. |

For Debian/Ubuntu, an installation supporting the demo and the three engine choices is:

```bash
sudo apt update
sudo apt install latexmk texlive-latex-extra texlive-fonts-recommended texlive-science texlive-xetex texlive-luatex poppler-utils
```

The distribution packages provide [latexmk](https://packages.debian.org/stable/latexmk), [additional LaTeX packages](https://packages.debian.org/stable/texlive-latex-extra), and [scientific packages](https://packages.debian.org/stable/texlive-science). Other manuscripts may require additional TeX packages, fonts, or bibliography tools. After installing tools or changing PATH, open a new terminal, rerun `papre --check`, and restart the app. The app detects tools but does not install system software automatically.

Poppler's `pdftoppm` or Ghostscript is optional for rendered pages in the preview pane. If neither is detected, the app uses the browser's PDF viewer. The original compiled PDF remains available either way. Windows Ghostscript console executables (`gswin64c`/`gswin32c`) are also detected.

## Queue workflow

The selected repository contains:

```text
review_queue/
  suggested-edit.patch          # awaiting or undergoing review
  processed/
    suggested-edit.patch        # accepted changes after every decision is resolved
    suggested-edit.patch.review.json
  superseded/
    suggested-edit.<version>.patch
```

1. Paste a unified diff or JSON response into **Import LLM patch**, or choose a patch file, then click **Add to queue**. Import writes a file into `review_queue` and opens it for review. You can also place `.patch`, `.diff`, or `.json` files there using another tool; the interface discovers them automatically. Enqueuing by itself does not apply or validate a patch.
2. Opening a patch checks it against the current working-tree text, including uncommitted changes. If it cannot apply, a warning offers **Archive**, which moves it to `superseded`, or **Skip**, which leaves it queued for later. Skipped patches are listed separately and retain saved decisions.
3. Click **Accept**, **Reject**, or **Edit** for each change. Only the update is editable. **Cancel edit** discards an unsaved edit. **Save edit** writes the revised patch and preserves the previous file in `superseded`; a changed edit returns to pending for another Accept/Reject decision. **Restore suggestion** is available after a saved edit. You can still reject your own update. **Accept pending** and **Reject pending** resolve all remaining changes.
4. Review context defaults to ten lines around each change. Choose three, thirty, or **Full file**, or expand a hidden unchanged region. **Open full file** and the sidebar file list show current repository source read-only, including text outside the patch. The split diff's original column remains the source snapshot against which the patch was opened.
5. When every change is decided, the patch moves into `processed`. It contains only accepted changes; its adjacent `.review.json` receipt retains the decisions and rejected suggestions. An all-rejected patch is empty and has a receipt. Before applying, you can reopen a processed review and change decisions; saving an edit that needs review returns the patch to the queue. Earlier versions are retained in `superseded`.
6. Toggle **PDF preview** and compile **Proposed changes** (accepted and pending, excluding rejected), **Accepted changes**, or **Working tree**. Compilation uses a separate snapshot with bibliography processing. It does not modify the manuscript. A compiled PDF can become stale after a review change; recompile to refresh it. The compilation log is available in the preview panel. **Hide sidebar** frees more space, and dragging either vertical divider adjusts the sidebar or PDF width. Focus a divider and use Left/Right arrows for keyboard resizing (Shift for larger steps). Widths and sidebar visibility are remembered in this browser. **Expand PDF** fills the workspace with the preview and folds **Compilation settings**; **Restore split view** or Escape restores your previous layout. You can reopen the settings while expanded. **Page size → Fit width** fits rendered pages to the available width.
7. **Apply accepted changes** checks the baseline again, then modifies only accepted manuscript text in the working tree. **Export accepted patch** works with Apply disabled. Review the Git diff, commit, and use your existing Overleaf Git sync workflow. **Undo apply** restores the baseline only if the files still match the applied result; it refuses to overwrite later edits and leaves the index unchanged.

The queue is polled while the page is open. Editing a queued file externally preserves its previously known version and starts a fresh review. Changing manuscript source externally invalidates the old baseline; archive the stale patch and provide a replacement against the current source. Queue files can appear as untracked files in Git status unless you choose to ignore them in your repository.

**Copy LLM context** copies a prompt and selected current source files for use in your existing chat. The app does not contact an LLM provider. **Git status and history** shows repository status and saved review activity.

## Patch formats

Git-style unified diff, optionally inside a Markdown code fence:

```diff
--- a/main.tex
+++ b/main.tex
@@ -1,2 +1,2 @@
 \section{Results}
-This is due to the fact that the force lowers the barrier.
+The force lowers the activation barrier.
```

The importer uses Git in an isolated copy and recounts hunk lengths, tolerating incorrect line counts when the source context is valid. It does not silently fuzzy-match text. Generated patches can be applied with `git apply reviewed.patch`.

An exact-match JSON alternative:

```json
{
  "title": "Clarify the result",
  "rationale": "Remove unnecessary wording while retaining scientific meaning.",
  "edits": [
    {
      "path": "main.tex",
      "search": "This is due to the fact that the force lowers the barrier.",
      "replace": "The force lowers the activation barrier."
    }
  ]
}
```

Each search must match exactly once. Include surrounding text when needed for uniqueness. Escape LaTeX backslashes in JSON (`\\section`, for example). Multiple edits to one file run in order. After an imported JSON patch is revised, it is saved as a unified `.patch`; the prior JSON remains in `superseded`.

## Extension points and limits

- `papre/core.py`: repository validation, isolated import, review decisions, patch generation, Apply/Undo, baseline checks, and SQLite history.
- `papre/queue.py`: directory discovery, enqueue, skip, archive, version preservation, processing, and review receipts.
- `papre/preview.py`: snapshots, asynchronous compilation, timeouts, logs, PDFs, and rendered pages.
- `papre/server.py`: directory browser, repository attachment, loopback HTTP API, request protection, and launcher configuration.
- `papre/picker.py`: native desktop folder dialogs with cancellation and an in-page fallback.
- `papre/static/components.js`: shared custom controls, resizable dividers, and split/source viewers. `app.js` coordinates the workflow; `layout.js` manages panel widths and expanded PDF viewing; `style.css` defines their appearance.

This version changes existing UTF-8/LF `.tex`, `.bib`, `.sty`, `.cls`, and `.bst` files up to 2 MB. It excludes file creation/deletion, renames, permission changes, binary patches, hidden paths, symlinks, Git's quoted path syntax, and changes inside `review_queue`. Ordinary filenames containing spaces and existing dirty worktrees are supported. Queued patches are limited to 16 MB.

Previews copy tracked and nonignored regular files, including images. Queue artifacts, ignored assets, external symlinks, and hidden files are omitted. Snapshots are limited to 64 MB per file and 256 MB total. Compilation stops after 120 seconds. Rendering stops after 45 seconds and displays up to 50 pages; the original PDF includes all pages. Builds and logs remain in review state; old `builds/` folders can be removed while the server is stopped.

Compilation continues through recoverable LaTeX errors by default. If the engine produces a new PDF, the app displays it as **Preview with errors**, with a warning and the compilation log. Check **Stop on first error** to request the stricter compiler behavior. Error previews may omit content or have incorrect formatting. Missing required packages or fatal errors can prevent any PDF from being generated; these builds show the error and produce no new preview. Package and class lookup uses the selected TeX installation on PATH. `papre --check` prints the detected tool paths; use an up-to-date TeX distribution containing the manuscript's packages. A PDF stored in the manuscript repository is never reused as output from a failed build.

The compiler disables shell escape and automatic `latexmkrc` execution and restricts TeX input/output paths. It is intended for trusted local manuscripts and is not an operating-system sandbox. Packages such as `minted` or custom `.latexmkrc` rules require an extension to the compiler adapter. Local TeX versions and packages may render differently from Overleaf. A preview produced despite errors is explicitly distinguished from a successful compilation; incomplete PDF files are not served.

The server binds only to loopback and guards mutations with a request token and repository identity. Repository attachment affects the server's current session; use one browser session per server when switching repositories. Saved decisions use revisions to prevent competing edits from silently overwriting each other. If an Apply/Undo operation is interrupted, startup compares recovery snapshots with source and locks ambiguous results instead of overwriting files.

## Development and verification

From a source checkout, install the app in editable mode:

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

The tests use disposable repositories inside ignored `.runtime/tests/`. They cover review decisions, selective Apply/Undo, index preservation, stale source, path guards, queue discovery and versioning, skipped/processed/superseded states, recovery, repository selection, request protection, tool detection, and snapshot compilation. Compilation checks skip when the required tools are unavailable.

For optional browser verification and distribution builds:

```bash
python -m pip install -e ".[dev]"
python -m playwright install chromium
python tests/browser_smoke.py
python -m build
```

The browser workflow checks the directory picker, white split viewer, context expansion, update-only editing and rejection, version backups, persistence, processing, PDF preview, sidebar visibility, draggable and keyboard-resizable widths, expanded PDF viewing, export, Apply/Undo, Skip/Archive, and external queue changes. It needs `latexmk`, `pdflatex`, and Poppler or Ghostscript. Screenshots go to `.runtime/qa/`. Playwright and the build frontend are development tools, not app runtime dependencies.

`run-local.sh` is a convenience launcher for a source checkout on macOS/Linux. It uses `python3` by default; set `PAPRE_PYTHON` to choose another interpreter. Installed users can use the `papre` command from any working directory.

`pyproject.toml` defines the build backend, package metadata, console command, and optional development dependencies. `MANIFEST.in` includes source assets and development documentation in source distributions. The root `.gitignore` excludes runtime data, environments, credentials, caches, and generated artifacts while retaining demo sources and patch fixtures.
