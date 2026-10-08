"""Asynchronous LaTeX compilation against copies of the working tree."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .core import ReviewError, ReviewStore, timestamp
from .system import ENGINES, available_tools


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
        with self.lock:
            if self.running:
                raise ReviewError("A preview is already compiling. Wait for it to finish.", 409)
            job_id = uuid.uuid4().hex
            folder = self.root / job_id
            snapshot = folder / "source"
            snapshot.mkdir(parents=True)
            warnings = []
            try:
                with self.store.lock:
                    outputs = {}
                    if selection != "working":
                        proposal = self.store.get(proposal_id or "")
                        if proposal["status"] != "reviewing" or proposal["revision"] != revision:
                            raise ReviewError("Reload this proposal before compiling it.", 409)
                        self.store.verify_base(proposal)
                        outputs = self.store.render(proposal, selection)
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
                    warnings.append("Preview uses tracked and nonignored files; symlinks and hidden files are omitted.")
                    if (self.store.repo.root / ".latexmkrc").exists():
                        warnings.append("Project .latexmkrc is not executed. Use the selected engine and standard latexmk rules.")
            except Exception:
                shutil.rmtree(folder)
                raise
            job = {"id": job_id, "status": "running", "main": main, "engine": engine,
                   "selection": selection, "proposal": proposal_id, "revision": revision,
                   "strict": strict, "engine_path": tools["engine_paths"][engine], "latexmk_path": tools["latexmk"],
                   "created": timestamp(), "warnings": warnings, "log": "Preparing LaTeX preview…"}
            self.jobs[job_id] = job
            self.running = True
            self._persist(job)
            threading.Thread(target=self._compile, args=(job_id,), daemon=True).start()
            return dict(job)

    def _persist(self, job: dict):
        target = self.root / job["id"] / "job.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def _compile(self, job_id: str):
        job = self.jobs[job_id]
        folder = self.root / job_id
        output = folder / "output"
        output.mkdir()
        env = dict(os.environ)
        env.update(openin_any="p", openout_any="p", TEXMFOUTPUT=str(output))
        command = [job["latexmk_path"], "-norc", ENGINES[job["engine"]], "-no-shell-escape",
                   "-interaction=nonstopmode", "-halt-on-error" if job["strict"] else "-f", "-file-line-error",
                   "-outdir=" + str(output), "./" + job["main"]]
        started = time.monotonic()
        try:
            with (folder / "compile.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=folder / "source", env=env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    stop_process_tree(process)
                    raise ReviewError(f"Compilation exceeded {self.timeout} seconds.")
            pdf = output / (Path(job["main"]).stem + ".pdf")
            content = (folder / "compile.log").read_text(encoding="utf-8", errors="replace")
            diagnostics = compile_diagnostics(content)
            has_pdf = complete_pdf(pdf)
            status = ("succeeded" if code == 0 and not diagnostics["error"] else "with_errors") if has_pdf else "failed"
            if status == "with_errors":
                job["warnings"].append("LaTeX reported errors. This PDF may contain incomplete content or incorrect formatting. Read the compilation log.")
            pages = self._render_pages(job, pdf) if has_pdf else []
            with self.lock:
                job.update(status=status, log=content[-100000:], seconds=round(time.monotonic() - started, 2),
                           pages=pages,
                           exit_code=code, **diagnostics,
                           pdf="/api/previews/" + job_id + "/pdf" if has_pdf else None)
        except Exception as exc:
            with self.lock:
                partial = (folder / "compile.log").read_text(errors="replace") if (folder / "compile.log").exists() else ""
                job.update(status="failed", log=partial[-90000:] + "\n" + str(exc), pdf=None,
                           **(compile_diagnostics(partial) | {"error": str(exc)}))
        finally:
            with self.lock:
                self.running = False
                self._persist(job)

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
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            try:
                _, errors = process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                stop_process_tree(process)
                process.communicate()
                raise ReviewError("PDF page rendering timed out; open the original PDF.")
            if process.returncode:
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
