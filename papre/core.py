"""Repository access, patch import, and durable review decisions."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

SOURCE_SUFFIXES = {".tex", ".bib", ".sty", ".cls", ".bst"}
MAX_SOURCE = 2 * 1024 * 1024


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_git(cwd: Path, *args: str, patch: str | None = None) -> subprocess.CompletedProcess:
    # Inherited GIT_DIR/GIT_WORK_TREE must not redirect operations elsewhere.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=str(cwd.resolve().parent))
    try:
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd, env=env,
                              input=patch.encode("utf-8") if patch is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError(f"Git could not complete this operation: {exc}", 500) from exc


def git_patch(changes: dict[str, tuple[str, str]]) -> str:
    chunks = []
    for path, (before, after) in changes.items():
        if before == after:
            continue
        chunks.append(f"diff --git a/{path} b/{path}\n")
        for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                         fromfile="a/" + path, tofile="b/" + path):
            chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(chunks)


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        result = run_git(self.root, "rev-parse", "--show-toplevel")
        if result.returncode or Path(result.stdout.decode().strip()).resolve() != self.root:
            raise ReviewError("Choose the root of an existing local Git repository.")

    def path(self, name: str, source_only: bool = True) -> Path:
        if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name):
            raise ReviewError("Invalid repository file path.")
        parts = PurePosixPath(name).parts
        if source_only and parts and parts[0] == "review_queue":
            raise ReviewError("The review_queue is reserved for review artifacts.")
        if PurePosixPath(name).is_absolute() or any(p in {".", "..", ".git"} or p.startswith(".") for p in parts):
            raise ReviewError("Paths must stay inside the repository and cannot reference hidden files.")
        if name != PurePosixPath(name).as_posix():
            raise ReviewError("Use a normalized repository-relative file path.")
        path = self.root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ReviewError(f"Symlinks are not supported: {name}")
        if not path.resolve().is_relative_to(self.root):
            raise ReviewError("Path escapes the repository.")
        if source_only and path.suffix.lower() not in SOURCE_SUFFIXES:
            raise ReviewError("Patches can modify existing .tex, .bib, .sty, .cls, and .bst files.")
        return path

    def read(self, name: str) -> str:
        path = self.path(name)
        if not path.is_file():
            raise ReviewError(f"Source file does not exist: {name}")
        if path.stat().st_size > MAX_SOURCE:
            raise ReviewError(f"Source file is larger than 2 MB: {name}")
        try:
            raw = path.read_bytes()
            if b"\x00" in raw:
                raise UnicodeError("binary content")
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ReviewError(f"Source must be UTF-8 text: {name}") from exc
        if "\r" in text:
            raise ReviewError(f"Convert {name} to LF line endings before reviewing patches.")
        return text

    def files(self, source_only: bool = True) -> list[str]:
        result = run_git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        if result.returncode:
            raise ReviewError(result.stderr.decode(errors="replace"), 500)
        names = set(result.stdout.decode("utf-8").split("\x00")) - {""}
        files = []
        for name in sorted(names):
            if PurePosixPath(name).parts[0] == "review_queue":
                continue
            try:
                path = self.path(name, source_only)
                if path.is_file():
                    files.append(name)
            except ReviewError:
                continue
        return files

    def status(self) -> dict:
        status = run_git(self.root, "status", "--short").stdout.decode(errors="replace")
        branch = run_git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        head = run_git(self.root, "rev-parse", "--verify", "HEAD")
        return {"name": self.root.name, "root": str(self.root), "branch": branch.stdout.decode().strip()
                if branch.returncode == 0 else "unborn", "head": head.stdout.decode().strip()
                if head.returncode == 0 else None, "dirty": bool(status), "changes": status,
                "remotes": run_git(self.root, "remote").stdout.decode().splitlines()}


class ReviewStore:
    def __init__(self, repo: Repository, state: Path, allow_write: bool = False):
        self.repo, self.state, self.allow_write = repo, state.resolve(), allow_write
        if self.state.is_relative_to(repo.root):
            raise ReviewError("Review state must be outside the manuscript repository.")
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.state / "reviews.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, proposal TEXT, action TEXT, detail TEXT)")
        self.db.commit()

    def close(self):
        self.db.close()

    def _save(self, proposal: dict, action: str, detail: str = ""):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO proposals VALUES (?, ?)",
                            (proposal["id"], json.dumps(proposal, ensure_ascii=False)))
            self.db.execute("INSERT INTO events (time, proposal, action, detail) VALUES (?, ?, ?, ?)",
                            (timestamp(), proposal["id"], action, detail))

    def get(self, proposal_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            if not row:
                raise ReviewError("Proposal not found.", 404)
            return json.loads(row[0])

    def list(self) -> list[dict]:
        with self.lock:
            proposals = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM proposals ORDER BY rowid DESC")]
            return [{k: p[k] for k in ("id", "title", "created", "status", "revision")}
                    | {"counts": self.counts(p)} for p in proposals]

    @staticmethod
    def counts(proposal: dict) -> dict:
        counts = {"pending": 0, "accepted": 0, "rejected": 0, "edited": 0, "total": 0}
        for file in proposal["files"]:
            for hunk in file["hunks"]:
                counts[hunk["decision"]] += 1
                counts["edited"] += hunk["new"] != hunk["suggested"]
                counts["total"] += 1
        return counts

    def create(self, targets: dict[str, str], title: str, rationale: str = "", bases: dict | None = None) -> dict:
        with self.lock:
            if not targets or len(targets) > 100:
                raise ReviewError("A proposal must contain between 1 and 100 changed files.")
            files = []
            for name, after in targets.items():
                if not isinstance(after, str) or len(after.encode()) > MAX_SOURCE or "\r" in after or "\x00" in after:
                    raise ReviewError("Replacement content must be UTF-8 text with LF line endings, at most 2 MB.")
                before = self.repo.read(name) if bases is None else bases[name]
                if bases is not None and self.repo.read(name) != before:
                    raise ReviewError(f"{name} changed during patch import. Import it again.", 409)
                old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
                hunks = []
                for kind, i, j, a, b in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
                    if kind == "equal":
                        continue
                    new = "".join(new_lines[a:b])
                    hunks.append({"id": uuid.uuid4().hex[:12], "start": i, "end": j,
                                  "old": "".join(old_lines[i:j]), "new": new, "suggested": new,
                                  "context_before": "".join(old_lines[max(0, i - 2):i]),
                                  "context_after": "".join(old_lines[j:j + 2]),
                                  "decision": "pending", "reason": rationale})
                if hunks:
                    files.append({"path": name, "base_digest": digest(before), "before": before, "hunks": hunks})
            if not files:
                raise ReviewError("This patch does not change any source text.")
            proposal = {"id": uuid.uuid4().hex, "title": (title.strip() or "Imported patch")[:200],
                        "rationale": rationale[:10000], "created": timestamp(), "status": "reviewing",
                        "revision": 1, "files": files, "base_head": self.repo.status()["head"]}
            self._save(proposal, "import")
            return proposal

    def import_patch(self, patch: str, title: str, rationale: str = "") -> dict:
        if not isinstance(patch, str) or len(patch.encode()) > 8 * MAX_SOURCE:
            raise ReviewError("Patch must be text smaller than 16 MB.")
        # A copied fenced patch is convenient; explanatory prose is not executed.
        stripped = patch.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            patch = "\n".join(stripped.splitlines()[1:-1]) + "\n"
        if not patch.endswith("\n"):
            patch += "\n"
        paths, previous = [], None
        for line in patch.splitlines():
            if line.startswith(("GIT binary patch", "Binary files ", "rename from ", "rename to ",
                                "copy from ", "copy to ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")):
                raise ReviewError("This version supports text modifications only; no renames, modes, new files, or deletions.")
            if line.startswith("--- "):
                previous = line[4:].split("\t", 1)[0]
                if not previous.startswith("a/"):
                    raise ReviewError("Use a unified diff with --- a/path and +++ b/path headers.")
            elif line.startswith("+++ "):
                after = line[4:].split("\t", 1)[0]
                if previous is None or not after.startswith("b/") or previous[2:] != after[2:]:
                    raise ReviewError("Patch headers must refer to the same existing file.")
                name = after[2:]
                self.repo.path(name)
                if name in paths:
                    raise ReviewError("Use one diff section per file.")
                paths.append(name)
                previous = None
        if not paths or len(paths) > 100:
            raise ReviewError("No supported unified diff found. Include --- / +++ headers and @@ change lines.")
        with self.lock, tempfile.TemporaryDirectory(prefix="import-", dir=self.state) as folder:
            sandbox = Path(folder)
            parsed = run_git(sandbox, "apply", "--numstat", "-z", "--recount", "-", patch=patch)
            if parsed.returncode:
                raise ReviewError("Invalid patch:\n" + parsed.stderr.decode(errors="replace"))
            actual_paths = [entry.split("\t", 2)[-1] for entry in parsed.stdout.decode("utf-8").split("\x00") if entry]
            if sorted(actual_paths) != sorted(paths):
                raise ReviewError("Patch contains unsupported file operations or conflicting path headers.")
            bases = {name: self.repo.read(name) for name in paths}
            for name, text in bases.items():
                target = sandbox / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode())
            result = run_git(sandbox, "apply", "--recount", "--whitespace=nowarn", "-", patch=patch)
            if result.returncode:
                raise ReviewError("Patch could not be applied to the current source:\n" + result.stderr.decode(errors="replace"))
            targets = {}
            for name in paths:
                target = sandbox / name
                if not target.is_file() or target.is_symlink():
                    raise ReviewError("Only modifications to existing text files are supported.")
                try:
                    targets[name] = target.read_bytes().decode("utf-8")
                except UnicodeError as exc:
                    raise ReviewError("Patched source is not UTF-8.") from exc
            return self.create(targets, title, rationale, bases)

    def import_json(self, data: dict, title: str, rationale: str = "") -> dict:
        """Portable LLM format: {edits: [{path, search, replace}]} with unique exact anchors."""
        if not isinstance(data, dict) or not isinstance(data.get("edits"), list) or not data["edits"]:
            raise ReviewError("JSON must contain a nonempty edits array.")
        bases, targets = {}, {}
        for edit in data["edits"]:
            if not isinstance(edit, dict) or not all(isinstance(edit.get(k), str) for k in ("path", "search", "replace")):
                raise ReviewError("Each edit needs string path, search, and replace fields.")
            name, search, replacement = edit["path"], edit["search"], edit["replace"]
            if name not in targets:
                bases[name] = targets[name] = self.repo.read(name)
            if not search or targets[name].count(search) != 1:
                raise ReviewError(f"Search text must match exactly once in {name}. Include more context.")
            targets[name] = targets[name].replace(search, replacement, 1)
        return self.create(targets, title or data.get("title", ""), rationale or data.get("rationale", ""), bases)

    def update(self, proposal_id: str, revision: int, decisions: list[dict]) -> dict:
        with self.lock:
            proposal = self.get(proposal_id)
            self._editable(proposal, revision)
            known = {h["id"]: h for f in proposal["files"] for h in f["hunks"]}
            if not isinstance(decisions, list) or not decisions:
                raise ReviewError("No review decisions provided.")
            for change in decisions:
                if not isinstance(change, dict) or change.get("id") not in known:
                    raise ReviewError("Unknown change.")
                hunk = known[change["id"]]
                decision = change.get("decision", hunk["decision"])
                if decision not in {"pending", "accepted", "rejected"}:
                    raise ReviewError("Invalid decision.")
                new = change.get("new", hunk["new"])
                if not isinstance(new, str) or len(new.encode()) > MAX_SOURCE or "\r" in new or "\x00" in new:
                    raise ReviewError("Edited source must be LF text, at most 2 MB.")
                hunk.update(decision=decision, new=new)
            proposal["revision"] += 1
            self._save(proposal, "review", f"{len(decisions)} changes")
            return proposal

    @staticmethod
    def _editable(proposal: dict, revision: int):
        if proposal["status"] != "reviewing":
            raise ReviewError("This proposal has already been applied or undone.", 409)
        if proposal["revision"] != revision:
            raise ReviewError("Review changed in another tab. Reload before continuing.", 409)

    def verify_base(self, proposal: dict):
        for file in proposal["files"]:
            if digest(self.repo.read(file["path"])) != file["base_digest"]:
                raise ReviewError(f"{file['path']} changed since import. Re-import against the current source.", 409)

    @staticmethod
    def render(proposal: dict, selection: str = "accepted") -> dict[str, str]:
        if selection not in {"accepted", "proposed", "all"}:
            raise ReviewError("Choose accepted, proposed, or all changes.")
        outputs = {}
        for file in proposal["files"]:
            lines = file["before"].splitlines(keepends=True)
            parts, cursor = [], 0
            for hunk in file["hunks"]:
                parts.append("".join(lines[cursor:hunk["start"]]))
                include = selection == "all" or hunk["decision"] == "accepted" or (selection == "proposed" and hunk["decision"] == "pending")
                parts.append(hunk["new"] if include else hunk["old"])
                cursor = hunk["end"]
            parts.append("".join(lines[cursor:]))
            outputs[file["path"]] = "".join(parts)
        return outputs

    def patch(self, proposal: dict, selection: str = "accepted") -> str:
        output = self.render(proposal, selection)
        return git_patch({f["path"]: (f["before"], output[f["path"]]) for f in proposal["files"]})

    def apply(self, proposal_id: str, revision: int) -> dict:
        with self.lock:
            if not self.allow_write:
                raise ReviewError("Repository is read-only. Restart with --allow-write to enable Apply and Undo.", 403)
            proposal = self.get(proposal_id)
            self._editable(proposal, revision)
            counts = self.counts(proposal)
            if counts["pending"]:
                raise ReviewError("Accept or reject every change before applying.", 409)
            self.verify_base(proposal)
            patch = self.patch(proposal)
            if not patch:
                raise ReviewError("No accepted text changes to apply.")
            checked = run_git(self.repo.root, "apply", "--check", "--whitespace=nowarn", "-", patch=patch)
            if checked.returncode:
                raise ReviewError(checked.stderr.decode(errors="replace"), 409)
            # Persist the recovery material BEFORE Git changes any worktree files.
            proposal["recovery_after"] = self.render(proposal)
            proposal["status"] = "applying"
            self._save(proposal, "apply-start")
            try:
                self.verify_base(proposal)
            except ReviewError:
                proposal["status"] = "reviewing"
                self._save(proposal, "apply-cancelled", "Source changed before apply")
                raise
            applied = run_git(self.repo.root, "apply", "--whitespace=nowarn", "-", patch=patch)
            if applied.returncode:
                proposal["status"] = "reviewing"
                self._save(proposal, "apply-failed", applied.stderr.decode(errors="replace"))
                raise ReviewError("Git could not apply changes. Review the working tree:\n" + applied.stderr.decode(errors="replace"), 409)
            proposal.update(status="applied", applied=timestamp(), revision=proposal["revision"] + 1)
            self._save(proposal, "apply")
            return proposal

    def undo(self, proposal_id: str, revision: int) -> dict:
        with self.lock:
            if not self.allow_write:
                raise ReviewError("Repository is read-only.", 403)
            proposal = self.get(proposal_id)
            if proposal["status"] != "applied" or proposal["revision"] != revision:
                raise ReviewError("Only the current applied proposal can be undone.", 409)
            after = proposal["recovery_after"]
            changed_files = [f for f in proposal["files"] if f["before"] != after[f["path"]]]
            for file in changed_files:
                if self.repo.read(file["path"]) != after[file["path"]]:
                    raise ReviewError(f"{file['path']} changed after applying. Undo would overwrite newer work.", 409)
            patch = git_patch({f["path"]: (after[f["path"]], f["before"]) for f in changed_files})
            checked = run_git(self.repo.root, "apply", "--check", "--whitespace=nowarn", "-", patch=patch)
            if checked.returncode:
                raise ReviewError(checked.stderr.decode(errors="replace"), 409)
            proposal["status"] = "undoing"
            self._save(proposal, "undo-start")
            result = run_git(self.repo.root, "apply", "--whitespace=nowarn", "-", patch=patch)
            if result.returncode:
                proposal["status"] = "applied"
                self._save(proposal, "undo-failed")
                raise ReviewError(result.stderr.decode(errors="replace"), 409)
            proposal.update(status="undone", revision=proposal["revision"] + 1)
            self._save(proposal, "undo")
            return proposal

    def recover(self):
        """Classify an interrupted apply without ever guessing or overwriting files."""
        with self.lock:
            for summary in self.list():
                proposal = self.get(summary["id"])
                status = proposal["status"]
                if status not in {"applying", "undoing"}:
                    continue
                try:
                    current = {f["path"]: self.repo.read(f["path"]) for f in proposal["files"]}
                    before = {f["path"]: f["before"] for f in proposal["files"]}
                    after = proposal["recovery_after"]
                    if current == after:
                        proposal["status"] = "applied"
                    elif current == before:
                        proposal["status"] = "reviewing" if status == "applying" else "undone"
                    else:
                        proposal["status"] = "recovery-needed"
                except ReviewError:
                    proposal["status"] = "recovery-needed"
                proposal["revision"] += 1
                self._save(proposal, "recovered", proposal["status"])

    def events(self) -> list[dict]:
        with self.lock:
            return [{"time": t, "proposal": p, "action": a, "detail": d}
                    for t, p, a, d in self.db.execute(
                        "SELECT time, proposal, action, detail FROM events ORDER BY id DESC LIMIT 100")]
