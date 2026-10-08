from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from papre.preview import PreviewManager, complete_pdf, compile_diagnostics
from papre.core import Repository, ReviewError, ReviewStore, run_git
from papre.server import ROOT


class PreviewTests(unittest.TestCase):
    def test_incomplete_outputs_are_not_preview_pdfs(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            pdf = Path(folder) / "main.pdf"
            self.assertFalse(complete_pdf(pdf))
            for content in (b"", b"not a PDF\n%%EOF", b"%PDF-1.5\nunfinished object"):
                pdf.write_bytes(content)
                self.assertFalse(complete_pdf(pdf))
            pdf.write_bytes(b"%PDF-1.5\n" + b"data\n" * 300 + b"%%EOF\n")
            self.assertTrue(complete_pdf(pdf))

    def test_missing_packages_are_reported_once(self):
        log = "! LaTeX Error: File `tabularray.sty' not found.\nLatexmk: Missing input file: 'tabularray.sty'\n"
        diagnostic = compile_diagnostics(log)
        self.assertEqual(diagnostic["missing_packages"], ["tabularray.sty"])
        self.assertIn("TeX installation", diagnostic["error"])
        self.assertEqual(compile_diagnostics("./main.tex:8: Undefined control sequence.\n")["error"], "./main.tex:8: Undefined control sequence.")
        self.assertEqual(compile_diagnostics("No errors; Output written on main.pdf.")["error"], "")


class BackgroundPreviewTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "main.tex").write_text("Original.\n")
        run_git(repo, "init")
        self.store = ReviewStore(Repository(repo), self.root / "state")
        self.manager = PreviewManager(self.store, timeout=5)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.manager.close)
        self.tools = {"latexmk": "test-compiler", "engine_paths": {"pdflatex": "test-engine"},
                      "engines": ["pdflatex"], "renderer": None, "renderer_path": None}
        self.patcher = patch.object(self.manager, "available", return_value=self.tools)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.processes = []
        self.driver = self.root / "driver.py"
        self.driver.write_text('''from pathlib import Path
import subprocess, sys, time
mode, output, slow = sys.argv[1:]
output = Path(output)
output.mkdir(exist_ok=True)
if slow == 'yes':
    heartbeat = output / 'heartbeat'
    child = "from pathlib import Path; import time; p=Path(" + repr(str(heartbeat)) + ");\\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.03)"
    subprocess.Popen([sys.executable, '-c', child])
    (output / 'started').write_text('started')
    time.sleep(30)
if mode == 'compile':
    (output / 'main.pdf').write_bytes(b'%PDF-1.5\\ncomplete\\n%%EOF\\n')
''')
        original = self.manager._run_process

        def run(job, command, **kwargs):
            folder = self.manager.root / job["id"]
            mode = "compile" if command[0] == "test-compiler" else "render"
            output = folder / ("output" if mode == "compile" else "pages")
            text = (folder / "source/main.tex").read_text()
            slow = "Slow compile" in text if mode == "compile" else "Slow render" in text
            # Exercise actual process groups, timeouts, and cancellation with a portable test driver.
            return original(job, [sys.executable, str(self.driver), mode, str(output), "yes" if slow else "no"], **kwargs)

        self.run_patch = patch.object(self.manager, "_run_process", side_effect=run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if str(self.driver) in args[0]:
                self.processes.append(process)
            return process

        self.process_patch = patch("papre.preview.subprocess.Popen", side_effect=popen)
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail("Background preview did not reach the expected state")

    def start(self, text="Fast."):
        (self.store.repo.root / "main.tex").write_text(text + "\n")
        return self.manager.start("main.tex", "pdflatex", "working", None, None)

    def finished(self, job):
        self.wait_for(lambda: self.manager.get(job["id"])["status"] != "running")
        return self.manager.get(job["id"])

    @unittest.skipIf(sys.platform == "win32", "POSIX child-process group check")
    def test_new_preview_stops_compiler_and_child_and_reuses_finished_result(self):
        old = self.start("Slow compile")
        output = self.manager.root / old["id"] / "output"
        self.wait_for(lambda: (output / "heartbeat").exists())
        reused_running = self.manager.start("main.tex", "pdflatex", "working", None, None)
        self.assertEqual(reused_running["id"], old["id"])
        latest = self.start()
        self.assertEqual(self.finished(latest)["status"], "succeeded")
        self.assertEqual(self.manager.get(old["id"])["status"], "cancelled")
        self.assertIsNotNone(self.processes[0].poll())
        heartbeat = (output / "heartbeat").read_bytes()
        time.sleep(.1)
        self.assertEqual((output / "heartbeat").read_bytes(), heartbeat)
        count = len(self.processes)
        reused = self.manager.start("main.tex", "pdflatex", "working", None, None)
        self.assertEqual(reused["id"], latest["id"])
        self.assertEqual(len(self.processes), count)
        with self.assertRaises(ReviewError):
            self.manager.pdf(old["id"])
        (self.store.repo.root / "figure.txt").write_text("Changed input")
        fresh = self.manager.start("main.tex", "pdflatex", "working", None, None)
        self.assertNotEqual(fresh["id"], latest["id"])
        self.assertEqual(self.finished(fresh)["status"], "succeeded")

    @unittest.skipIf(sys.platform == "win32", "POSIX child-process group check")
    def test_new_preview_preempts_page_renderer(self):
        self.tools.update(renderer="pdftoppm", renderer_path="test-renderer")
        old = self.start("Slow render")
        pages = self.manager.root / old["id"] / "pages"
        self.wait_for(lambda: (pages / "heartbeat").exists())
        latest = self.start()
        self.assertEqual(self.finished(latest)["status"], "succeeded")
        self.assertEqual(self.manager.get(old["id"])["status"], "cancelled")
        self.assertIsNotNone(self.processes[1].poll())
        heartbeat = (pages / "heartbeat").read_bytes()
        time.sleep(.1)
        self.assertEqual((pages / "heartbeat").read_bytes(), heartbeat)

    def test_queued_intermediate_revisions_are_discarded(self):
        entered, release = threading.Event(), threading.Event()
        compile_job = self.manager._compile

        def blocked(job_id):
            if not entered.is_set():
                entered.set()
                release.wait(timeout=5)
            compile_job(job_id)

        with patch.object(self.manager, "_compile", side_effect=blocked):
            first = self.start("First")
            self.assertTrue(entered.wait(timeout=5))
            middle = self.start("Middle")
            latest = self.start("Latest")
            release.set()
            self.assertEqual(self.finished(latest)["status"], "succeeded")
        for job in (first, middle):
            self.assertEqual(self.manager.get(job["id"])["status"], "cancelled")
        self.assertEqual(len(self.processes), 1)
        self.assertFalse((self.manager.root / middle["id"] / "output").exists())

    def test_stale_revision_cannot_preempt_the_current_request(self):
        proposal = self.store.import_json({"edits": [{"path": "main.tex", "search": "Original.", "replace": "Updated."}]}, "Test")
        hunk = proposal["files"][0]["hunks"][0]
        proposal = self.store.update(proposal["id"], proposal["revision"], [{"id": hunk["id"], "decision": "accepted"}])
        latest = self.manager.start("main.tex", "pdflatex", "proposed", proposal["id"], proposal["revision"])
        with self.assertRaises(ReviewError):
            self.manager.start("main.tex", "pdflatex", "proposed", proposal["id"], proposal["revision"] - 1)
        self.assertEqual(self.finished(latest)["status"], "succeeded")
        self.assertEqual(self.manager.latest, latest["id"])

    def test_cancel_and_close_stop_queued_or_running_jobs(self):
        job = self.start("Slow compile")
        self.wait_for(lambda: (self.manager.root / job["id"] / "output/started").exists())
        self.manager.cancel()
        self.wait_for(lambda: not self.manager.running)
        self.assertEqual(self.manager.get(job["id"])["status"], "cancelled")
        self.manager.close()
        self.assertFalse(self.manager.worker.is_alive())

    def test_worker_timeout_still_stops_the_process(self):
        self.manager.timeout = .2
        job = self.start("Slow compile")
        result = self.finished(job)
        self.assertEqual(result["status"], "failed")
        self.assertIn("exceeded", result["error"])
        self.assertIsNone(result["pdf"])
        self.assertIsNotNone(self.processes[0].poll())

    def test_completed_pdf_remains_available_while_replacement_starts(self):
        published, release = threading.Event(), threading.Event()
        compile_job = self.manager._compile

        def finished_but_returning(job_id):
            compile_job(job_id)
            published.set()
            release.wait(timeout=5)

        with patch.object(self.manager, "_compile", side_effect=finished_but_returning):
            previous = self.start("Previous")
            self.assertTrue(published.wait(timeout=5))
            latest = self.start("Latest")
            self.assertEqual(self.manager.get(previous["id"])["status"], "succeeded")
            self.assertTrue(self.manager.pdf(previous["id"]).is_file())
            release.set()
            self.assertEqual(self.finished(latest)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
