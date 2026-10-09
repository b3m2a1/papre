# Paper Patch Review Editor: agent guide

Read `README.md` before changing the app or preparing manuscript patches. This
project (`papre`) is a local, human-reviewed LaTeX patch interface, served by Python 3.11+
with Git and an optional local TeX installation. Manuscripts stay on the user's
computer; the app does not call an LLM service.

## Working on the app

- Keep changes inside this app unless the user explicitly authorizes another
  location. Do not edit a manuscript repository when the request only concerns
  this application.
- Preserve the minimal white interface: original source on the left, update on
  the right, red removals, and green additions. Only updates are editable.
- Reuse the custom components in `papre/static/components.js` and their shared
  styles in `style.css`. Extend a shared component rather than copying it into
  another part of the interface. Native controls belong in those components.
- Keep the Python runtime dependency-free and the frontend buildless. A free CDN
  component is allowed if needed, but prefer the existing custom components and
  avoid adding a framework for routine changes.
- Preserve explicit Accept, Reject, Edit, Skip, Archive, Apply, and Undo actions.
  Review decisions do not directly modify manuscript source.
- Keep Git staging, commits, pulls, pushes, and Overleaf sync under user control.
  Do not publish this app or push a repository unless asked.

## Preparing a manuscript patch

When asked to suggest manuscript changes, produce a patch for review rather than
editing manuscript source directly. Only write to a manuscript repository's
queue when that location and action are authorized by the user's request.

1. Read the relevant current working-tree files, including uncommitted edits.
   Use that text as the baseline, not an assumed version of `HEAD`.
2. Preserve scientific meaning, numerical values, equations, citations, labels,
   and LaTeX commands unless the requested change requires otherwise. Do not
   invent results or references; explain uncertainty outside the patch.
3. Produce a Git-style unified diff with `--- a/path`, `+++ b/path`, and valid
   `@@` hunks and context. Exact-match JSON with `edits` containing `path`,
   `search`, and `replace` is also supported; escape LaTeX backslashes in JSON.
4. Modify only existing UTF-8/LF `.tex`, `.bib`, `.sty`, `.cls`, or `.bst` files.
   File creation/deletion, renames, binary changes, mode changes, hidden paths,
   symlinks, and changes inside `review_queue` are unsupported patch operations.
5. Save an authorized suggestion as a uniquely named `.patch`, `.diff`, or `.json`
   file directly inside the manuscript's `review_queue/`, or return it for the
   user to paste into **Import LLM patch**. Keep explanations outside a diff;
   JSON may include `title` and `rationale`.
6. Leave review decisions and application to the user. Do not overwrite queued
   patches or move them to `processed` or `superseded` yourself.

## Workflow invariants

- Import only enqueues a file. Opening a patch validates it against current source.
- Skip retains the queued file and saved decisions for later.
- A patch that cannot apply offers a warning with Archive; Archive moves it to
  `review_queue/superseded/`.
- Saving an edited patch preserves its previous version in `superseded`. The
  user must be able to reject the edited update afterward.
- Double-clicking unchanged update/context lines or full-file source opens the
  shared inline source-region editor. Keep its original column read-only. Bound
  paragraph/section edits at existing hunks, derive new pending hunks against the
  exact source baseline, and retain previous IDs and decisions. Source edits may
  add another existing file to the current review, or create a queued review if
  none is editable. Cancel discards unsaved text. Saving must preserve the previous
  queue file and prepare a preview; it must never write manuscript source or Git's
  index. Guard the source digest, review revision, paths, and external queue edits.
- Once every change is accepted or rejected, the final accepted patch moves to
  `review_queue/processed/` with its review receipt. All-rejected patches are empty.
- Apply changes only accepted source text, leaves Git's index unchanged, and
  refuses a stale baseline. Undo refuses to overwrite subsequent edits.
- PDF compilation uses a separate snapshot. Exclude queue artifacts from previews;
  retain disabled shell escape and compiler timeouts. A new PDF from an errored
  build is allowed as an explicitly marked error preview, never as a clean success.
  Reject incomplete outputs and never reuse a manuscript PDF as a failed build's output.
- Compile from the selected main document's parent directory inside the snapshot,
  passing its filename to latexmk. Use that same directory for unmarked retries
  and SyncTeX queries. Resolve relative SyncTeX inputs from the build's recorded
  directory, retain snapshot containment checks, and expose repository-relative
  source paths. Keep compiler outputs in the separate build output directory.
- Prepare previews when a patch opens or saved review content changes. Keep one
  compiler/rendering worker per repository; newer requests preempt older process
  groups and discard intermediate queued builds. Reuse only matching settings,
  review revisions, and repository inputs. A cancelled build must never publish
  output or replace the current preview. Stop workers before closing their stores.
- Display the latest completed preview automatically and reveal the first ready PDF
  by default. Respect an explicitly hidden preview pane; completed builds must still
  update its contents. Preserve generation guards so stale builds cannot replace it.
- Keep the PDF viewport free of outer padding. The main document, Open PDF link,
  expand/restore icon, and settings hamburger belong in its compact toolbar. Icons
  need tooltips and accessible labels. Viewer selection, rendered-page zoom, settings,
  and the log use the shared overlay drawer, closed by default; opening it must not
  resize the PDF. Support outside-click and Escape dismissal with keyboard focus.
- Preserve the browser PDF iframe and rendered-page alternative. Switching viewers
  should reuse the displayed PDF, without changing review state or recompiling it.
- Color only pending safe prose in disposable proposed-preview snapshots. Never add
  annotation markup to manuscript source or exported patches; accepted/rejected edits
  have no review color. Retry unmarked compilation if markup causes an error, within
  the original timeout. Leave unsafe TeX constructs unmarked and explain the limitation.
  Permit locally grouped color inside the true/false text arguments of ifthenelse
  and three-argument ifthen, including nested branches. Do not color condition-test
  arguments or allow arbitrary open macro groups; keep branch selection and source
  line numbers intact. Verify with compiled conditional examples.
- Preserve the native PDF plugin. Use optional SyncTeX for change locations and page
  fragments for native page jumps; do not overlay it or claim to capture its mouse events.
  Double-click source navigation belongs in Rendered pages. Guard coordinates, source
  paths, repository identity and review revisions. Strip SYNCTEX_EDITOR/SYNCTEX_VIEWER
  before queries so mapping never launches a configured external command.
  Map unchanged preview lines back through insertion/deletion offsets before editing
  source outside a hunk, and refuse source changes since the preview was compiled.
- Preserve path and symlink guards, loopback binding, request-token and origin
  checks, repository identity checks, durable decisions, and revision guards.
- Render source and patch content as text, never as trusted HTML.

## Code map and verification

- `papre/core.py`: source validation, patch import, decisions, Apply/Undo, history.
- `papre/queue.py`: discovery, enqueue, version backups, processing, and receipts.
- `papre/preview.py`: compilation snapshots, logs, PDFs, and page rendering.
- `papre/annotations.py`: preview-only coloring, source paragraph ranges, SyncTeX records.
- `papre/server.py`: HTTP API, repository picker, and launch configuration.
- `papre/picker.py`: native operating-system folder selection; keep HTTP token checks,
  explicit `--browse-root` restrictions, cancellation, and the in-page fallback.
- `papre/static/`: shared UI components and browser workflow.
- `papre/assets/`: self-contained toolbar logo and favicon assets. Keep the favicon's
  simplified small-size drawing and include assets in wheels and source distributions.
- `tests/`: disposable-repository tests and optional browser verification.

Use Python 3.11+ from the chosen development environment. From the project root:

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

The distribution, import package, and installed command are named `papre`.
`papre --check` reports available Git, LaTeX engines, and PDF tools. Keep runtime
state in the user data directory or an explicit `--state-dir`; never write it
beside installed package files. Include static files, logo assets, and demo fixtures when
changing distribution configuration, and verify an installed build works from
an unrelated working directory.

Run relevant tests for behavior changes. For UI changes, use the existing optional
Playwright workflow in `tests/browser_smoke.py` and inspect its screenshots in
`.runtime/qa/`. Keep test manuscripts, embedded Git repositories, review databases,
source snapshots, builds, and screenshots inside ignored `.runtime/` directories.
Documentation-only changes need a content check, not a full browser/test run.

Before committing, inspect the staged files. Include app source, demo fixtures,
tests, and documentation; exclude local environments, credentials, manuscript
repositories, queue data, review history, and generated preview artifacts.
