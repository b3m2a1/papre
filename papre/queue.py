"""Filesystem review queue with durable decisions and preserved patch versions."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid

from .core import ReviewError, ReviewStore, digest, timestamp

PATCH_SUFFIXES = {".patch", ".diff", ".json"}
MAX_PATCH = 16 * 1024 * 1024


class PatchQueue:
    def __init__(self, store: ReviewStore):
        self.store = store
        self.root = store.repo.root / "review_queue"
        for folder in (self.root, self.root / "processed", self.root / "superseded"):
            if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                raise ReviewError(f"Queue folders must be ordinary directories: {folder}")
            folder.mkdir(exist_ok=True)
        with store.lock, store.db:
            store.db.execute("""CREATE TABLE IF NOT EXISTS queue (
                id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                hash TEXT NOT NULL, proposal TEXT, status TEXT NOT NULL,
                error TEXT, title TEXT NOT NULL, rationale TEXT NOT NULL, created TEXT NOT NULL)""")

    def _path(self, relative: str) -> Path:
        parts = PurePosixPath(relative).parts
        if not parts or len(parts) > 2 or (len(parts) == 2 and parts[0] not in {"processed", "superseded"}):
            raise ReviewError("Invalid queue path.")
        if relative != PurePosixPath(relative).as_posix() or any(p.startswith(".") or "\\" in p or any(ord(c) < 32 for c in p) for p in parts):
            raise ReviewError("Invalid queue path.")
        path = self.root
        if path.is_symlink():
            raise ReviewError("Queue directory was replaced by a symlink.")
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ReviewError("Queue files and folders cannot be symlinks.")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ReviewError("Queue path escapes its directory.")
        return path

    def _read(self, path: Path) -> str:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PATCH:
            raise ReviewError("Queue patches must be regular text files smaller than 16 MB.")
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
                raw = stream.read(MAX_PATCH + 1)
            if len(raw) > MAX_PATCH or b"\0" in raw:
                raise ValueError("oversized or binary file")
            return raw.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise ReviewError("Cannot read queue patch as UTF-8 text.") from exc

    def _save(self, row: dict):
        columns = ("id", "path", "payload", "hash", "proposal", "status", "error", "title", "rationale", "created")
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [row[k] for k in columns])

    def _rows(self) -> list[dict]:
        cursor = self.store.db.execute("SELECT * FROM queue ORDER BY created, path")
        keys = [column[0] for column in cursor.description]
        return [dict(zip(keys, row)) for row in cursor]

    def get(self, entry_id: str) -> dict:
        with self.store.lock:
            for row in self._rows():
                if row["id"] == entry_id:
                    return row
        raise ReviewError("Queue patch not found.", 404)

    def _unique(self, folder: str, name: str) -> str:
        path = PurePosixPath(name)
        candidate = f"{folder}/{name}" if folder else name
        while self._path(candidate).exists():
            new = f"{path.stem}.{uuid.uuid4().hex[:8]}{path.suffix}"
            candidate = f"{folder}/{new}" if folder else new
        return candidate

    def _create_file(self, relative: str, text: str):
        path = self._path(relative)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644), "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())

    def _replace_file(self, relative: str, text: str, expected_hash: str):
        path = self._path(relative)
        if digest(self._read(path)) != expected_hash:
            raise ReviewError("The queue patch changed outside this review. Reload it before continuing.", 409)
        fd, temporary = tempfile.mkstemp(prefix=".review-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            self._path(relative)
            if digest(self._read(path)) != expected_hash:
                raise ReviewError("The queue patch changed during save. Reload it.", 409)
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _preserve(self, row: dict) -> str:
        name = PurePosixPath(row["path"]).name
        version = f"{Path(name).stem}.{uuid.uuid4().hex[:12]}{Path(name).suffix}"
        destination = self._unique("superseded", version)
        self._create_file(destination, row["payload"])
        return destination

    def scan(self):
        with self.store.lock:
            self._path("processed")
            self._path("superseded")
            rows = {row["path"]: row for row in self._rows()}
            found = set()
            for path in sorted(self.root.iterdir()):
                if path.name.startswith(".") or path.suffix.lower() not in PATCH_SUFFIXES:
                    continue
                if path.is_symlink():
                    continue
                try:
                    text = self._read(self._path(path.name))
                except ReviewError as exc:
                    # Unreadable files still appear with Archive/Skip controls.
                    text = ""
                    error = str(exc)
                else:
                    error = None
                found.add(path.name)
                row = rows.get(path.name)
                if row and digest(text) == row["hash"] and (not error or row["error"] == error):
                    continue
                if row:
                    if row["payload"]:
                        self._preserve(row)
                    if row["proposal"]:
                        old = self.store.get(row["proposal"])
                        if old["status"] == "reviewing":
                            old.update(status="superseded", revision=old["revision"] + 1)
                            self.store._save(old, "queue-replaced")
                    row.update(payload=text, hash=digest(text), proposal=None, status="invalid" if error else "pending", error=error)
                else:
                    row = {"id": uuid.uuid4().hex, "path": path.name, "payload": text, "hash": digest(text),
                           "proposal": None, "status": "invalid" if error else "pending", "error": error,
                           "title": path.stem, "rationale": "", "created": timestamp()}
                self._save(row)
            for name, row in rows.items():
                if "/" not in name and name not in found and row["status"] != "removed":
                    row.update(status="removed", error="Queue file was removed outside the interface.")
                    self._save(row)

    def list(self) -> dict:
        with self.store.lock:
            self.scan()
            entries = []
            for row in self._rows():
                if row["status"] == "removed":
                    continue
                summary = {key: row[key] for key in ("id", "path", "status", "error", "title", "proposal", "created")}
                if row["proposal"]:
                    p = self.store.get(row["proposal"])
                    summary.update(counts=self.store.counts(p), proposal_status=p["status"], revision=p["revision"])
                entries.append(summary)
            archives = [{"path": "superseded/" + p.name, "name": p.name} for p in sorted((self.root / "superseded").iterdir())
                        if p.is_file() and not p.is_symlink() and p.suffix.lower() in PATCH_SUFFIXES]
            return {"entries": entries, "archives": archives, "root": str(self.root)}

    def enqueue(self, text: str, title: str = "", rationale: str = "", filename: str | None = None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_PATCH or "\0" in text:
            raise ReviewError("Paste a text patch smaller than 16 MB.")
        if not isinstance(title, str) or not isinstance(rationale, str):
            raise ReviewError("Title and explanation must be text.")
        if filename:
            if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
                raise ReviewError("Use a filename without directory components.")
            self._path(filename)
            if Path(filename).suffix.lower() not in PATCH_SUFFIXES:
                filename = str(Path(filename).with_suffix(".patch"))
        else:
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", title).strip("-")[:70] or "patch"
            filename = slug + (".json" if text.lstrip().startswith("{") else ".patch")
        with self.store.lock:
            relative = self._unique("", filename)
            self._create_file(relative, text)
            row = {"id": uuid.uuid4().hex, "path": relative, "payload": text, "hash": digest(text),
                   "proposal": None, "status": "pending", "error": None, "title": title.strip() or Path(relative).stem,
                   "rationale": rationale, "created": timestamp()}
            self._save(row)
            return {k: row[k] for k in ("id", "path", "status", "title")}

    def open(self, entry_id: str) -> dict:
        with self.store.lock:
            self.scan()
            row = self.get(entry_id)
            proposal = None
            warning = row["error"]
            if row["status"] in {"superseded", "removed"}:
                return {"entry": self._summary(row), "proposal": None, "warning": warning or "This patch has been archived."}
            try:
                self._current(row)
                if row["proposal"]:
                    proposal = self.store.get(row["proposal"])
                    if proposal["status"] == "reviewing":
                        self.store.verify_base(proposal)
                else:
                    text = row["payload"].strip()
                    if text.startswith("```") and text.endswith("```"):
                        text = "\n".join(text.splitlines()[1:-1])
                    if text.startswith("{"):
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise ReviewError("Queued JSON could not be parsed: " + str(exc)) from exc
                        proposal = self.store.import_json(data, row["title"], row["rationale"])
                    else:
                        proposal = self.store.import_patch(text, row["title"], row["rationale"])
                    row["proposal"] = proposal["id"]
                if row["status"] != "processed":
                    row["status"] = "reviewing"
                row["error"] = None
                warning = None
            except ReviewError as exc:
                row.update(status="invalid", error=str(exc))
                warning = str(exc)
            self._save(row)
            return {"entry": self._summary(row), "proposal": self._public(proposal), "warning": warning}

    @staticmethod
    def _summary(row: dict) -> dict:
        return {k: row[k] for k in ("id", "path", "status", "title", "error", "proposal")}

    def _public(self, proposal: dict | None):
        return proposal | {"counts": self.store.counts(proposal)} if proposal else None

    def _current(self, row: dict):
        if digest(self._read(self._path(row["path"]))) != row["hash"]:
            raise ReviewError("The queue file changed externally. Reload it before continuing.", 409)

    def review(self, entry_id: str, revision: int, decisions: list[dict]) -> dict:
        return self._modify(entry_id, lambda before: self.store.update(before["id"], revision, decisions))

    def edit_source(self, data: dict) -> dict:
        with self.store.lock:
            if data.get("entry") is not None:
                return self._modify(data["entry"], lambda before: self.store.edit_source(data, before["id"], data.get("revision")))
            proposal = self.store.edit_source(data)
            row = self.get(self.enqueue(self.store.patch(proposal, "all"), proposal["title"])["id"])
            row.update(proposal=proposal["id"], status="reviewing")
            self._save(row)
            return {"entry": self._summary(row), "proposal": self._public(proposal), "warning": None}

    def _modify(self, entry_id: str, operation) -> dict:
        with self.store.lock:
            row = self.get(entry_id)
            self._current(row)
            if not row["proposal"] or row["status"] not in {"reviewing", "processed", "skipped"}:
                raise ReviewError("Open a valid queue patch before reviewing it.", 409)
            before = self.store.get(row["proposal"])
            original_row = row.copy()
            self.store.verify_base(before)
            proposal = operation(before)
            try:
                self._sync(row, proposal)
            except Exception:
                if row["payload"] != original_row["payload"] or row["path"] != original_row["path"]:
                    # The patch was already saved or moved. Keep decisions consistent
                    # with that file even if a later move or audit receipt failed.
                    self._save(row)
                else:
                    before["revision"] = proposal["revision"] + 1
                    self.store._save(before, "queue-save-failed")
                raise
            return {"entry": self._summary(row), "proposal": self._public(proposal), "warning": None}

    def _sync(self, row: dict, proposal: dict):
        complete = self.store.counts(proposal)["pending"] == 0
        text = self.store.patch(proposal, "accepted" if complete else "all")
        changed = text != row["payload"]
        if changed:
            self._preserve(row)
            self._replace_file(row["path"], text, row["hash"])
            row.update(payload=text, hash=digest(text))
        folder = "processed" if complete else ""
        current_folder = str(PurePosixPath(row["path"]).parent)
        if current_folder == ".":
            current_folder = ""
        basename = PurePosixPath(row["path"]).name
        filename = str(Path(basename).with_suffix(".patch")) if changed else basename
        if current_folder != folder or basename != filename:
            self._current(row)
            destination = self._unique(folder, filename)
            os.rename(self._path(row["path"]), self._path(destination))
            row["path"] = destination
        row.update(status="processed" if complete else "reviewing", error=None)
        self._save(row)
        if complete:
            self._write_receipt(row, proposal)

    def _write_receipt(self, row: dict, proposal: dict):
        receipt = row["path"] + ".review.json"
        text = json.dumps({"queue": row["id"], "reviewed": timestamp(), "proposal": proposal}, indent=2, ensure_ascii=False)
        path = self._path(receipt)
        if path.exists():
            self._replace_file(receipt, text, digest(self._read(path)))
        else:
            self._create_file(receipt, text)

    def skip(self, entry_id: str) -> dict:
        with self.store.lock:
            row = self.get(entry_id)
            if "/" in row["path"]:
                raise ReviewError("Only a queued patch can be skipped.")
            if not row["error"]:
                self._current(row)
            row["status"] = "skipped"
            self._save(row)
            return self._summary(row)

    def archive(self, entry_id: str) -> dict:
        with self.store.lock:
            row = self.get(entry_id)
            if row["status"] == "superseded":
                return self._summary(row)
            path = self._path(row["path"])
            if path.is_file() and not path.is_symlink():
                destination = self._unique("superseded", path.name)
                os.rename(path, self._path(destination))
                row["path"] = destination
            else:
                raise ReviewError("The queued file no longer exists.", 409)
            row.update(status="superseded", error=None)
            if row["proposal"]:
                proposal = self.store.get(row["proposal"])
                if proposal["status"] == "reviewing":
                    proposal.update(status="superseded", revision=proposal["revision"] + 1)
                    self.store._save(proposal, "archive")
            self._save(row)
            return self._summary(row)

    def apply(self, entry_id: str, revision: int, undo=False) -> dict:
        with self.store.lock:
            row = self.get(entry_id)
            if row["status"] != "processed" or not row["proposal"]:
                raise ReviewError("Finish reviewing this patch before applying it.", 409)
            self._current(row)
            operation = self.store.undo if undo else self.store.apply
            proposal = operation(row["proposal"], revision)
            self._write_receipt(row, proposal)
            return {"entry": self._summary(row), "proposal": self._public(proposal), "warning": None}

    def text(self, relative: str) -> str:
        return self._read(self._path(relative))
