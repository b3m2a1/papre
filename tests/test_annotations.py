from pathlib import Path
import re
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from papre.annotations import color_prose, review_annotations, safe_prose, sync_records, uncomment
from papre.core import Repository, ReviewError, ReviewStore, run_git
from papre.preview import PreviewManager
from papre.server import DEMO, ROOT


def green_in_pdf(pdf):
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf.read_bytes(), re.S):
        try:
            stream = zlib.decompress(stream)
        except zlib.error:
            pass
        if re.search(rb"0\.08 0\.48 0\.20? rg", stream):
            return True
    return False


class AnnotationTests(unittest.TestCase):
    def test_coloring_preserves_lines_comments_math_and_selected_text(self):
        text = "A value of $x^{2}$ and \\textbf{bold text}. % comment with an unmatched {\nAnother line.\n"
        colored = color_prose(text)
        self.assertEqual(colored.count("\n"), text.count("\n"))
        self.assertTrue(safe_prose(text, "\\documentclass{article}\n\\begin{document}\n"))
        self.assertIn("Another line.}", colored)
        self.assertTrue(safe_prose("Text with 20\\% uncertainty.\n", ""))
        self.assertEqual(uncomment("20\\% correct. % comment\n"), "20\\% correct. \n")
        self.assertEqual(uncomment("Line break \\\\% comment\n"), "Line break \\\\\n")

    def test_unsafe_constructs_and_preamble_are_not_grouped(self):
        for text, prefix in [("Partial } braces", ""), ("An unfinished $formula", ""),
                             ("Plain text.", "\\documentclass{article}\n"),
                             ("A verbatim line.", "\\begin{verbatim}\n"),
                             ("Text here", "\\textbf{"), ("Text here", "$$\n"),
                             ("x = 2", "\\begin{align}\n"),
                             ("\\newcommand{\\value}{updated}", ""),
                             ("\\begin{equation}x=2\\end{equation}", "")]:
            with self.subTest(text=text, prefix=prefix):
                self.assertFalse(safe_prose(text, prefix))

    def test_only_pending_prose_is_colored(self):
        file = {"path": "body.tex", "before": "Original.\n\nSecond.\n", "hunks": [
            {"id": "one", "start": 0, "end": 1, "old": "Original.\n", "new": "Proposed.\n", "decision": "pending"},
            {"id": "two", "start": 2, "end": 3, "old": "Second.\n", "new": "Accepted.\n", "decision": "accepted"}]}
        proposal = {"files": [file]}
        outputs, changes = review_annotations(proposal, "proposed")
        self.assertEqual([c["highlighted"] for c in changes], [True, False])
        self.assertEqual([c["paragraph"] for c in changes], [1, 2])
        self.assertEqual([c["line"] for c in changes], [1, 3])
        self.assertIn("Accepted.\n", outputs["body.tex"])
        for decision in ["accepted", "rejected"]:
            file["hunks"][0]["decision"] = decision
            outputs, changes = review_annotations(proposal, "proposed")
            self.assertEqual(outputs, ReviewStore.render(proposal, "proposed"))
            self.assertFalse(any(c["highlighted"] for c in changes))

    def test_sync_records_include_multiple_pages_and_colons_in_paths(self):
        result = sync_records("SyncTeX result begin\nPage:1\nx:10\ny:20\nPage:2\nx:30\ny:40\nSyncTeX result end\n")
        self.assertEqual([r["Page"] for r in result], ["1", "2"])
        result = sync_records("Input:C:/paper/main.tex\nLine:10\nColumn:2\n")
        self.assertEqual(result[0]["Input"], "C:/paper/main.tex")


@unittest.skipUnless(PreviewManager.available()["latexmk"] and "pdflatex" in PreviewManager.available()["engines"], "Local pdfLaTeX tools unavailable")
class AnnotatedPreviewTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        repo.mkdir()
        for name in ["main.tex", "references.bib"]:
            shutil.copyfile(DEMO / name, repo / name)
        run_git(repo, "init")
        self.original = (repo / "main.tex").read_bytes()
        self.store = ReviewStore(Repository(repo), self.root / "state")
        self.manager = PreviewManager(self.store)
        self.proposal = self.store.import_patch((DEMO / "example.patch").read_text(), "Preview annotations")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.manager.close)

    def compile(self):
        p = self.proposal
        job = self.manager.start("main.tex", "pdflatex", "proposed", p["id"], p["revision"], strict=True)
        deadline = time.monotonic() + 60
        while self.manager.get(job["id"])["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.05)
        result = self.manager.get(job["id"])
        self.assertEqual(result["status"], "succeeded", result["log"])
        return result

    def test_pending_green_disappears_after_accept_and_reject_without_source_changes(self):
        job = self.compile()
        self.assertTrue(green_in_pdf(self.manager.pdf(job["id"])))
        self.assertEqual([c["highlighted"] for c in job["changes"]], [True, True, True])
        decisions = [{"id": c["id"], "decision": "accepted" if i != 1 else "rejected"}
                     for i, c in enumerate(job["changes"])]
        self.proposal = self.store.update(self.proposal["id"], self.proposal["revision"], decisions)
        job = self.compile()
        self.assertFalse(green_in_pdf(self.manager.pdf(job["id"])))
        self.assertFalse(job["marked"])
        self.assertEqual((self.store.repo.root / "main.tex").read_bytes(), self.original)
        self.assertNotIn("\\color", self.store.patch(self.proposal))

    @unittest.skipUnless(PreviewManager.available().get("synctex") and PreviewManager.available()["renderer"], "Source navigation tools unavailable")
    def test_forward_locations_reverse_navigation_and_coordinate_guards(self):
        job = self.compile()
        self.assertEqual([c["page"] for c in job["changes"]], [1, 1, 2])
        import struct
        for change in job["changes"]:
            page = change["page"]
            header = self.manager.page(job["id"], str(page)).read_bytes()[:24]
            width, height = struct.unpack(">II", header[16:24])
            result = self.manager.source_at(job["id"], page, (change["x"] + 2) * 120 / 72 / width,
                                            (change["y"] - 2) * 120 / 72 / height)
            self.assertEqual(result["hunk"], change["id"])
        for page, x, y in [(0, .5, .5), (True, .5, .5), (1, -1, .5), (1, True, .5), (1, float("nan"), .5)]:
            with self.assertRaises(ReviewError):
                self.manager.source_at(job["id"], page, x, y)
        self.proposal = self.store.update(self.proposal["id"], self.proposal["revision"],
                                         [{"id": job["changes"][0]["id"], "decision": "accepted"}])
        with self.assertRaises(ReviewError) as caught:
            self.manager.source_at(job["id"], 1, .5, .5)
        self.assertEqual(caught.exception.status, 409)

    def test_failed_marked_build_retries_clean_source(self):
        run = self.manager._run_process
        injected = False
        def fail_first(job, command, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                source = self.manager.root / job["id"] / "source/main.tex"
                source.write_text("\\PapreMissingColorCommand\n" + source.read_text())
            return run(job, command, **kwargs)
        with patch.object(self.manager, "_run_process", side_effect=fail_first):
            job = self.compile()
        self.assertFalse(job["marked"])
        self.assertFalse(green_in_pdf(self.manager.pdf(job["id"])))
        self.assertTrue(any("coloring was disabled" in w for w in job["warnings"]))


if __name__ == "__main__":
    unittest.main()
