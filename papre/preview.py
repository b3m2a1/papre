"""Asynchronous LaTeX compilation against copies of the working tree."""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import tempfile
import time
import uuid

from .core import ReviewError, ReviewStore, timestamp
from .system import ENGINES, available_tools
from .annotations import review_annotations, sync_records


def complete_pdf(path: Path) -> bool:
    """Only expose a PDF from this build after the engine has finished writing it."""
    try:
        with path.open("rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                return False
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 1024))
            return b"%%EOF" in stream.read()
    except OSError:
        return False


def compile_diagnostics(log: str) -> dict:
    missing = list(dict.fromkeys(re.findall(r"(?:LaTeX Error: File|Missing input file:)\s*[`'\"]([^`'\"]+\.(?:sty|cls))[`'\"]", log)))
    if missing:
        error = "Required LaTeX package or class not found: " + ", ".join(missing) + ". Check your TeX installation and the manuscript's package requirements."
    else:
        error = next((line.strip() for line in log.splitlines() if "LaTeX Error:" in line or "Undefined control sequence" in line or
                      ("Package " in line and " Error:" in line)), "")
    return {"error": error, "missing_packages": missing}


def stop_process_tree(process):
    """Stop the timed-out compiler and its children on supported desktop systems."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


class PreviewManager:
    def __init__(self, store: ReviewStore, timeout: int = 120):
        self.store, self.timeout = store, timeout
        self.root = store.state / "builds"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.running = False
        self.condition = threading.Condition(self.lock)
        self.pending = self.active = self.latest = None
        self.cancelled = {}
        self.closed = False
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    @staticmethod
    def available() -> dict:
        return available_tools()

    def start(self, main: str, engine: str, selection: str, proposal_id: str | None, revision: int | None, strict: bool = False) -> dict:
        if not isinstance(strict, bool):
            raise ReviewError("Stop on first error must be true or false.")
        if engine not in ENGINES:
            raise ReviewError("Unsupported LaTeX engine.")
        if selection not in {"working", "proposed", "accepted"}:
            raise ReviewError("Invalid preview selection.")
        self.store.repo.path(main)
        if not main.endswith(".tex") or main not in self.store.repo.files():
            raise ReviewError("Choose a .tex main document from this repository.")
        tools = self.available()
        if not tools["latexmk"] or engine not in tools["engines"]:
            raise ReviewError(f"PDF preview needs latexmk and {engine} on PATH. "
                              + tools["installation"]["text"] + " " + tools["installation"]["url"], 503)
        with self.lock, self.store.lock:
            if self.closed:
                raise ReviewError("The preview compiler has stopped.", 503)
            if selection != "working":
                proposal = self.store.get(proposal_id or "")
                if proposal["status"] != "reviewing" or proposal["revision"] != revision:
                    raise ReviewError("Reload this proposal before compiling it.", 409)
                self.store.verify_base(proposal)
            else:
                proposal_id = revision = None
            stamp = self._source_stamp()
            identity = (main, engine, selection, proposal_id, revision, strict, stamp,
                        tools["engine_paths"][engine], tools["latexmk"])
            previous = self.jobs.get(self.latest)
            if previous and previous["identity"] == identity and previous["status"] in {"running", "succeeded", "with_errors"}:
                return dict(previous)
            job_id = uuid.uuid4().hex
            folder = self.root / job_id
            snapshot = folder / "source"
            snapshot.mkdir(parents=True)
            warnings = []
            try:
                with self.store.lock:
                    outputs, changes, clean_outputs = {}, [], {}
                    if selection != "working":
                        proposal = self.store.get(proposal_id or "")
                        if proposal["status"] != "reviewing" or proposal["revision"] != revision:
                            raise ReviewError("Reload this proposal before compiling it.", 409)
                        self.store.verify_base(proposal)
                        clean_outputs = self.store.render(proposal, selection)
                        outputs, changes = review_annotations(proposal, selection)
                    total = 0
                    for name in self.store.repo.files(source_only=False):
                        path = self.store.repo.path(name, source_only=False)
                        size = path.stat().st_size
                        total += size
                        if size > 64 * 1024 * 1024 or total > 256 * 1024 * 1024:
                            raise ReviewError("Preview snapshot exceeds the 64 MB file / 256 MB project limit.")
                        target = snapshot / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                    for name, text in outputs.items():
                        (snapshot / name).write_bytes(text.encode("utf-8"))
                    marked = any(change["highlighted"] for change in changes)
                    if marked:
                        clean_outputs.setdefault(main, (snapshot / main).read_text(encoding="utf-8"))
                        for name, text in clean_outputs.items():
                            backup = folder / "unmarked" / name
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            backup.write_text(text, encoding="utf-8")
                        # A separate line keeps a first-line comment from hiding the loader.
                        source = snapshot / main
                        source.write_text("\\RequirePackage{color}\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
                    warnings.append("Preview uses tracked and nonignored files; symlinks and hidden files are omitted.")
                    if (self.store.repo.root / ".latexmkrc").exists():
                        warnings.append("Project .latexmkrc is not executed. Use the selected engine and standard latexmk rules.")
                    if stamp != self._source_stamp():
                        raise ReviewError("Repository files changed while preparing the preview. Try again.", 409)
            except Exception:
                shutil.rmtree(folder)
                raise
            job = {"id": job_id, "status": "running", "main": main, "engine": engine,
                   "selection": selection, "proposal": proposal_id, "revision": revision,
                   "strict": strict, "engine_path": tools["engine_paths"][engine], "latexmk_path": tools["latexmk"],
                   "created": timestamp(), "warnings": warnings, "log": "Preparing LaTeX preview…"}
            job.update(changes=changes, marked=marked, line_offsets={main: 1} if marked else {},
                       synctex_path=tools.get("synctex"))
            job.update(identity=identity, phase="queued")
            self._cancel_locked("Superseded by a newer preview.")
            self.jobs[job_id] = job
            self.cancelled[job_id] = threading.Event()
            self.pending = self.latest = job_id
            self.running = True
            self._persist(job)
            self.condition.notify()
            return dict(job)

    def _source_stamp(self):
        """Invalidate warm builds when any snapshot input is changed or replaced."""
        entries = []
        for name in self.store.repo.files(source_only=False):
            stat = self.store.repo.path(name, source_only=False).stat()
            entries.append((name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
        return hashlib.sha256(json.dumps(entries).encode()).hexdigest()

    def _cancel_locked(self, reason):
        for job_id in {self.pending, self.active} - {None}:
            job = self.jobs[job_id]
            if job["status"] != "running":
                continue
            self.cancelled[job_id].set()
            job.update(status="cancelled", phase="cancelled", error=reason, pdf=None, pages=[])
            self._persist(job)
        self.pending = None

    def cancel(self):
        with self.condition:
            self._cancel_locked("Preview cancelled.")
            self.running = self.active is not None
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self._cancel_locked("Preview compiler stopped.")
            self.condition.notify_all()
        self.worker.join(timeout=15)

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.closed)
                if self.closed:
                    return
                job_id = self.pending
                self.pending = None
                self.active = job_id
            self._compile(job_id)
            with self.condition:
                self.active = None
                self.running = self.pending is not None
                self.condition.notify_all()

    def _check_cancelled(self, job):
        if (event := self.cancelled.get(job["id"])) and event.is_set():
            raise ReviewError("Preview cancelled.")

    def _run_process(self, job, command, *, timeout, **kwargs):
        self._check_cancelled(job)
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._check_cancelled(job)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReviewError(f"Preview process exceeded {timeout} seconds.")
                try:
                    _, errors = process.communicate(timeout=min(.1, remaining))
                    self._check_cancelled(job)
                    return process.returncode, errors
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            stop_process_tree(process)
            process.communicate()
            raise

    def _persist(self, job: dict):
        target = self.root / job["id"] / "job.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def _compile(self, job_id: str):
        job = self.jobs[job_id]
        folder = self.root / job_id
        output = folder / "output"
        env = dict(os.environ)
        env.update(openin_any="p", openout_any="p", TEXMFOUTPUT=str(output))
        command = [job["latexmk_path"], "-norc", ENGINES[job["engine"]], "-no-shell-escape",
                   "-interaction=nonstopmode", "-halt-on-error" if job["strict"] else "-f", "-file-line-error",
                   "-synctex=1", "-outdir=" + str(output), "./" + job["main"]]
        started = time.monotonic()
        try:
            self._check_cancelled(job)
            output.mkdir()
            with self.lock:
                self._check_cancelled(job)
                job["phase"] = "compiling"
            with (folder / "compile.log").open("wb") as log:
                code, _ = self._run_process(job, command, timeout=self.timeout, cwd=folder / "source", env=env,
                                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            pdf = output / (Path(job["main"]).stem + ".pdf")
            content = (folder / "compile.log").read_text(encoding="utf-8", errors="replace")
            diagnostics = compile_diagnostics(content)
            has_pdf = complete_pdf(pdf)
            if job["marked"] and (code or diagnostics["error"] or not has_pdf):
                self._check_cancelled(job)
                # Coloring must never stop an otherwise usable manuscript preview.
                for source in (folder / "unmarked").rglob("*"):
                    if source.is_file():
                        shutil.copyfile(source, folder / "source" / source.relative_to(folder / "unmarked"))
                shutil.rmtree(output)
                output.mkdir()
                with (folder / "compile.log").open("wb") as log:
                    remaining = self.timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise ReviewError(f"Preview process exceeded {self.timeout} seconds.")
                    code, _ = self._run_process(job, command, timeout=remaining, cwd=folder / "source", env=env,
                                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                content = (folder / "compile.log").read_text(encoding="utf-8", errors="replace")
                diagnostics, has_pdf = compile_diagnostics(content), complete_pdf(pdf)
                job.update(marked=False, line_offsets={})
                for change in job["changes"]:
                    if change["highlighted"]:
                        change.update(highlighted=False, note="Coloring disabled after a LaTeX error; displaying the unmarked preview.")
                job["warnings"].append("Pending edit coloring was disabled after a LaTeX error. The unmarked source was compiled instead.")
            status = ("succeeded" if code == 0 and not diagnostics["error"] else "with_errors") if has_pdf else "failed"
            if status == "with_errors":
                job["warnings"].append("LaTeX reported errors. This PDF may contain incomplete content or incorrect formatting. Read the compilation log.")
            pages = self._render_pages(job, pdf) if has_pdf else []
            if has_pdf:
                self._locate_changes(job, pdf)
            with self.lock:
                self._check_cancelled(job)
                job.update(status=status, phase="finished", log=content[-100000:], seconds=round(time.monotonic() - started, 2),
                           pages=pages,
                           exit_code=code, **diagnostics,
                           pdf="/api/previews/" + job_id + "/pdf" if has_pdf else None)
        except Exception as exc:
            with self.lock:
                if not self.cancelled[job_id].is_set():
                    partial = (folder / "compile.log").read_text(errors="replace") if (folder / "compile.log").exists() else ""
                    job.update(status="failed", phase="finished", log=partial[-90000:] + "\n" + str(exc), pdf=None,
                               **(compile_diagnostics(partial) | {"error": str(exc)}))
        finally:
            with self.lock:
                self._persist(job)

    def _sync(self, job: dict, arguments: list[str]) -> list[dict]:
        env = {key: value for key, value in os.environ.items() if key not in {"SYNCTEX_EDITOR", "SYNCTEX_VIEWER"}}
        with tempfile.TemporaryFile(dir=self.root / job["id"]) as stream:
            code, _ = self._run_process(job, [job["synctex_path"], *arguments], timeout=2,
                                        cwd=self.root / job["id"] / "source", env=env,
                                        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.PIPE)
            stream.seek(0)
            return sync_records(stream.read(65536).decode(errors="replace")) if code == 0 else []

    def _locate_changes(self, job: dict, pdf: Path):
        if not job["changes"]:
            return
        if not job["synctex_path"]:
            job["warnings"].append("Install SyncTeX from your TeX distribution for PDF page locations and source navigation.")
            return
        deadline = time.monotonic() + 12
        with self.lock:
            job["phase"] = "locating changes"
        for change in job["changes"]:
            self._check_cancelled(job)
            if time.monotonic() >= deadline:
                job["warnings"].append("Some PDF locations are unavailable because source mapping reached its time limit.")
                break
            if not change["path"].endswith(".tex"):
                continue
            line = change["line"] + job["line_offsets"].get(change["path"], 0)
            source = self.root / job["id"] / "source" / change["path"]
            try:
                records = self._sync(job, ["view", "-i", f"{line}:0:{source}", "-o", str(pdf)])
                # Shipout can assign a new paragraph's line number to headers/footers
                # on the preceding page. A second source line disambiguates that noise.
                noisy = len(records) > 4 or any(float(r.get("H", "1")) == 0 for r in records)
                if noisy:
                    probe = max(change["end_line"], change["line"] + 1) + job["line_offsets"].get(change["path"], 0)
                    following = self._sync(job, ["view", "-i", f"{probe}:0:{source}", "-o", str(pdf)])
                    reference = next((r for r in following if "Page" in r and float(r.get("H", "0")) > 0), None)
                    if reference:
                        records.sort(key=lambda r: abs(int(r.get("Page", "0")) - int(reference["Page"])) * 100000 +
                                     abs(float(r.get("y", "0")) - float(reference.get("y", "0"))))
                for record in records:
                    if "Page" not in record or float(record.get("H", "0")) <= 0 or float(record.get("W", "0")) <= 0:
                        continue
                    page = int(record["Page"])
                    x, y = float(record.get("x", record.get("h", "nan"))), float(record.get("y", record.get("v", "nan")))
                    if page > 0 and math.isfinite(x) and math.isfinite(y):
                        change.update(page=page, x=x, y=y)
                        break
            except (OSError, ValueError, ReviewError):
                self._check_cancelled(job)
                # Missing SyncTeX data never prevents a PDF from being displayed.
                continue

    def source_at(self, job_id: str, page: int, x: float, y: float) -> dict:
        job = self.get(job_id)
        if job.get("proposal"):
            proposal = self.store.get(job["proposal"])
            if proposal["revision"] != job["revision"]:
                raise ReviewError("This preview refers to an older review revision. Wait for the current preview.", 409)
            self.store.verify_base(proposal)
        if not job.get("synctex_path"):
            raise ReviewError("SyncTeX source navigation is unavailable for this preview.", 409)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= len(job.get("pages", [])):
            raise ReviewError("Choose a rendered PDF page.")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or
               not 0 <= value <= 1 for value in (x, y)):
            raise ReviewError("Invalid PDF coordinates.")
        with self.page(job_id, str(page)).open("rb") as image:
            header = image.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            raise ReviewError("PDF page coordinates are unavailable.", 409)
        width, height = struct.unpack(">II", header[16:24])
        # Both renderer adapters use 120 dpi; SyncTeX coordinates use 72 dpi.
        point = f"{page}:{x * width * 72 / 120:.3f}:{y * height * 72 / 120:.3f}:{self.pdf(job_id)}"
        records = self._sync(job, ["edit", "-o", point])
        snapshot = self.root / job_id / "source"
        for record in records:
            if "Input" not in record or "Line" not in record:
                continue
            source = Path(record["Input"])
            source = (source if source.is_absolute() else snapshot / source).resolve()
            if not source.is_relative_to(snapshot.resolve()):
                continue
            path = str(source.relative_to(snapshot.resolve())).replace(os.sep, "/")
            line = int(record["Line"]) - job.get("line_offsets", {}).get(path, 0)
            candidates = [change for change in job.get("changes", []) if change["path"] == path and
                          change["paragraph_start"] <= line <= max(change["paragraph_end"], change["end_line"])]
            if candidates:
                change = min(candidates, key=lambda item: abs(item["line"] - line))
                return {"proposal": job["proposal"], "revision": job["revision"], "hunk": change["id"],
                        "path": path, "line": line}
        raise ReviewError("There is no reviewed change at this PDF location.", 404)

    def _render_pages(self, job: dict, pdf: Path) -> list[str]:
        """Use installed CLI tools; the original PDF stays available even if rendering fails."""
        pages = self.root / job["id"] / "pages"
        pages.mkdir()
        tools = self.available()
        if tools["renderer"] == "pdftoppm":
            command = [tools["renderer_path"], "-f", "1", "-l", "50", "-r", "120", "-png", str(pdf), str(pages / "render")]
        elif tools["renderer"] == "gs":
            command = [tools["renderer_path"], "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dFirstPage=1", "-dLastPage=50",
                       "-sDEVICE=png16m", "-r120", "-sOutputFile=" + str(pages / "render-%d.png"), str(pdf)]
        else:
            job["warnings"].append("Install Poppler (pdftoppm) or Ghostscript for rendered pages. Using your browser's PDF viewer.")
            return []
        try:
            with self.lock:
                self._check_cancelled(job)
                job["phase"] = "rendering"
            code, errors = self._run_process(job, command, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if code:
                raise ReviewError("PDF page rendering failed: " + errors.decode(errors="replace")[-1000:])
            images = sorted(pages.glob("render-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
            urls = []
            for index, image in enumerate(images, 1):
                image.replace(pages / f"page-{index}.png")
                urls.append(f"/api/previews/{job['id']}/pages/{index}")
            if len(urls) == 50:
                job["warnings"].append("The in-app preview displays the first 50 pages. Open the PDF for all pages.")
            return urls
        except (OSError, ReviewError) as exc:
            self._check_cancelled(job)
            job["warnings"].append(str(exc))
            return []

    def get(self, job_id: str) -> dict:
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ReviewError("Preview not found.", 404)
        with self.lock:
            if job_id in self.jobs:
                job = dict(self.jobs[job_id])
            else:
                path = self.root / job_id / "job.json"
                if not path.is_file():
                    raise ReviewError("Preview not found.", 404)
                job = json.loads(path.read_text())
                if job["status"] == "running":
                    job.update(status="failed", log="Server restarted during compilation.", pdf=None)
            if job["status"] == "running":
                log = self.root / job_id / "compile.log"
                if log.exists():
                    job["log"] = log.read_text(errors="replace")[-100000:]
            return job

    def pdf(self, job_id: str) -> Path:
        job = self.get(job_id)
        if job["status"] not in {"succeeded", "with_errors"}:
            raise ReviewError("This build produced no preview PDF.", 404)
        return self.root / job_id / "output" / (Path(job["main"]).stem + ".pdf")

    def page(self, job_id: str, number: str) -> Path:
        job = self.get(job_id)
        if job["status"] not in {"succeeded", "with_errors"} or not number.isdigit() or not 1 <= int(number) <= len(job.get("pages", [])):
            raise ReviewError("Preview page not found.", 404)
        return self.root / job_id / "pages" / f"page-{int(number)}.png"
