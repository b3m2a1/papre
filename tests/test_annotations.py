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

    def test_prose_in_conditional_text_branches_can_be_colored(self):
        for prefix in [
                "\\ifthenelse{\\equal{\\pageLimit}{5}}{%\n",
                "\\ifthenelse{\\equal{\\pageLimit}{5}}{%\n\n}{%\n\n",
                "\\ifthen{test}{True prose with \\textbf{balanced braces}.}{\n",
                "\\ifthenelse{test}{\\ifthenelse{nested}{\n",
                "\\ifthenelse{test}{\\ifthenelse{nested}{First branch.}{\n",
                "\\ifthenelse{test}{An escaped \\{ and 20\\% value. % ignored }\n",
                "\\ifthenelse % first argument\n {test} % second argument\n {} % third argument\n {\n"]:
            with self.subTest(prefix=prefix):
                self.assertTrue(safe_prose("Pending prose with $x^{2}$ and \\emph{emphasis}.\n", prefix))
        # Nested condition tests, definitions and other argument types stay guarded.
        for prefix in ["\\ifthenelse{\\equal{", "\\ifthenelse{test}{\\textbf{",
                       "\\ifthenelse{test}{\\unknown{", "\\textbf{\\ifthenelse{test}{",
                       "\\ifthenelse{test}{\\ifthenelse{\\equal{value}{",
                       "\\ifthenelse{test}{Text with $unfinished math.\n",
                       "\\newcommand{\\example}{\\ifthenelse{test}{",
                       "\\ifthenelse{test}{\\ifthenelse{nested}"]:
            with self.subTest(prefix=prefix):
                self.assertFalse(safe_prose("Pending prose.\n", prefix))

    def test_conditional_annotations_preserve_branch_controls_and_line_mapping(self):
        before = "\\ifthenelse{\\equal{\\pageLimit}{5}}{%\n\n}{%\n\nOriginal paragraph. % trailing comment\nUnchanged continuation.\n}\n"
        proposal = {"files": [{"path": "body.tex", "before": before, "hunks": [
            {"id": "branch", "start": 4, "end": 5, "old": "Original paragraph. % trailing comment\n",
             "new": "Pending paragraph. % trailing comment\n", "decision": "pending"}]}]}
        outputs, changes = review_annotations(proposal, "proposed")
        self.assertTrue(changes[0]["highlighted"])
        self.assertEqual(changes[0]["line"], 5)
        self.assertEqual(outputs["body.tex"].count("\n"), before.count("\n"))
        self.assertIn("Pending paragraph. }% trailing comment", outputs["body.tex"])
        self.assertEqual(outputs["body.tex"].splitlines()[:4], before.splitlines()[:4])
        self.assertTrue(outputs["body.tex"].endswith("Unchanged continuation.\n}\n"))
        for decision in ("accepted", "rejected"):
            proposal["files"][0]["hunks"][0]["decision"] = decision
            outputs, changes = review_annotations(proposal, "proposed")
            self.assertFalse(changes[0]["highlighted"])
            self.assertEqual(outputs, ReviewStore.render(proposal, "proposed"))

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

    def test_true_false_and_nested_conditional_prose_compiles_with_local_color(self):
        main = ("\\documentclass{article}\n\\usepackage{ifthen}\n"
                "\\newcommand{\\ifthen}[3]{\\ifthenelse{#1}{#2}{#3}}\n"
                "\\begin{document}\n\\input{body}\n\\end{document}\n")
        body = ("\\section{Conditional prose}\n"
                "\\ifthenelse{\\equal{a}{a}}{%\nOriginal true paragraph.\n}{%\nHidden false paragraph.\n}\n\n"
                "\\ifthenelse{\\equal{a}{b}}{%\nHidden true paragraph.\n}{%\n\n"
                "Original false paragraph. % keep this comment\n\n"
                "\\ifthenelse{\\equal{a}{a}}{%\nOriginal nested paragraph.\n}{Unused nested paragraph.}\n}\n\n"
                "\\ifthen{\\equal{a}{a}}{%\nOriginal alias paragraph.\n}{Unused alias paragraph.}\n\n"
                "Unchanged prose after every conditional.\n")
        root = self.store.repo.root
        (root / "main.tex").write_text(main)
        (root / "body.tex").write_text(body)
        after = body.replace("Original true", "Pending true").replace("Original false", "Pending false")
        after = after.replace("Original nested", "Pending nested").replace("Original alias", "Pending alias")
        self.proposal = self.store.create({"body.tex": after}, "Conditional prose coloring")
        job = self.compile()
        self.assertTrue(all(c["highlighted"] for c in job["changes"]))
        self.assertTrue(job["marked"])
        self.assertTrue(green_in_pdf(self.manager.pdf(job["id"])))
        self.assertFalse(any("coloring was disabled" in warning for warning in job["warnings"]))
        marked = (self.manager.root / job["id"] / "source/body.tex").read_text()
        self.assertEqual(marked.count("\n"), body.count("\n"))
        self.assertIn("Unchanged prose after every conditional.", marked)
        self.assertNotIn("\\color", self.store.patch(self.proposal))
        self.assertEqual((root / "main.tex").read_text(), main)
        self.assertEqual((root / "body.tex").read_text(), body)
        self.proposal = self.store.update(self.proposal["id"], self.proposal["revision"], [
            {"id": change["id"], "decision": "accepted" if i == 0 else "rejected"}
            for i, change in enumerate(job["changes"])])
        job = self.compile()
        self.assertFalse(green_in_pdf(self.manager.pdf(job["id"])))
        self.assertFalse(job["marked"])
        # Color in an unselected branch must not activate that branch or reach the PDF.
        self.proposal = self.store.create({"body.tex": body.replace("Hidden false", "Pending hidden false")}, "Hidden branch")
        job = self.compile()
        self.assertTrue(job["changes"][0]["highlighted"])
        self.assertFalse(green_in_pdf(self.manager.pdf(job["id"])))
        self.assertEqual((root / "body.tex").read_text(), body)

    @unittest.skipUnless(PreviewManager.available().get("synctex") and PreviewManager.available()["renderer"], "Source navigation tools unavailable")
    def test_unchanged_pdf_text_maps_back_through_review_offsets(self):
        job = self.compile()
        original = self.original.decode()
        line = next(i + 1 for i, text in enumerate(original.splitlines()) if text.startswith("The model uses"))
        hunks = self.proposal["files"][0]["hunks"]
        offset = sum(len(h["new"].splitlines()) - (h["end"] - h["start"]) for h in hunks if h["start"] < line - 1)
        source = self.manager.root / job["id"] / "source/main.tex"
        records = self.manager._sync(job, ["view", "-i", f"{line + offset + job['line_offsets'].get('main.tex', 0)}:0:{source}",
                                          "-o", str(self.manager.pdf(job["id"]))])
        record = next(r for r in records if float(r.get("H", "0")) > 0 and float(r.get("W", "0")) > 0)
        import struct
        width, height = struct.unpack(">II", self.manager.page(job["id"], record["Page"]).read_bytes()[16:24])
        result = self.manager.source_at(job["id"], int(record["Page"]), (float(record["x"]) + 4) * 120 / 72 / width,
                                        (float(record["y"]) - 2) * 120 / 72 / height)
        self.assertIsNone(result["hunk"])
        region = self.store.source_region(result["path"], result["line"], self.proposal["id"], self.proposal["revision"],
                                         expected_digest=result["base_digest"])
        self.assertIn("illustrative temperature", region["before"])
        self.assertNotIn("\\color", region["before"])
        # A working-tree build has no proposal baseline to guard it; its source stamp still must match.
        with patch.object(self.manager, "get", return_value=job | {"proposal": None, "revision": None, "selection": "working"}):
            (self.store.repo.root / "main.tex").write_bytes(self.original + b"% external change\n")
            with self.assertRaisesRegex(ReviewError, "Source changed since this preview"):
                self.manager.source_at(job["id"], 1, .5, .5)


if __name__ == "__main__":
    unittest.main()
