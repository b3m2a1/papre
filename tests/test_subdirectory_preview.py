from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from papre.core import Repository, ReviewError, ReviewStore, run_git
from papre.preview import PreviewManager
from papre.server import ROOT


@unittest.skipUnless(PreviewManager.available()["latexmk"] and "pdflatex" in PreviewManager.available()["engines"], "Local pdfLaTeX tools unavailable")
class SubdirectoryPreviewTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.directory = self.repo / "Teaching"
        self.directory.mkdir(parents=True)
        self.main = "Teaching/MentoringOnly.tex"
        self.document = ("\\documentclass{article}\n\\usepackage{graphicx}\n\\usepackage{localstyle}\n"
                         "\\begin{document}\n\\LocalLabel\n\\input{MentoringSection}\n"
                         "\\includegraphics[width=1em]{diagram}\n\\cite{example}\n"
                         "\\bibliographystyle{plain}\n\\bibliography{references}\n\\end{document}\n")
        self.section = "Original mentoring paragraph.\n\nUnchanged section context.\n"
        (self.directory / "MentoringOnly.tex").write_text(self.document)
        (self.directory / "MentoringSection.tex").write_text(self.section)
        (self.directory / "localstyle.sty").write_text("\\ProvidesPackage{localstyle}\n\\newcommand{\\LocalLabel}{Local style loaded.}\n")
        (self.directory / "references.bib").write_text("@misc{example, author={Demo}, title={Local bibliography}, year={2026}}\n")
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        image += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")
        (self.directory / "diagram.png").write_bytes(image)
        # Same-named root files must not win over resources beside the selected main.
        (self.repo / "MentoringSection.tex").write_text("\\WrongRootSection\n")
        (self.repo / "localstyle.sty").write_text("\\WrongRootStyle\n")
        for args in [("init",), ("add", "."), ("-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
                     "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")]:
            self.assertEqual(run_git(self.repo, *args).returncode, 0)
        self.store = ReviewStore(Repository(self.repo), self.root / "state")
        self.manager = PreviewManager(self.store)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.manager.close)

    def compile(self, proposal=None):
        job = self.manager.start(self.main, "pdflatex", "proposed" if proposal else "working",
                                 proposal["id"] if proposal else None, proposal["revision"] if proposal else None, strict=True)
        deadline = time.monotonic() + 60
        while self.manager.get(job["id"])["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.05)
        job = self.manager.get(job["id"])
        self.assertEqual(job["status"], "succeeded", job["log"])
        self.assertEqual(job["directory"], "Teaching")
        return job

    def test_working_tree_build_resolves_local_input_style_graphics_and_bibliography(self):
        job = self.compile()
        output = self.manager.root / job["id"] / "output"
        self.assertTrue(self.manager.pdf(job["id"]).read_bytes().startswith(b"%PDF"))
        self.assertIn("Local bibliography", (output / "MentoringOnly.bbl").read_text())
        self.assertIn("./MentoringSection.tex", job["log"])
        self.assertNotIn("WrongRoot", job["log"])
        self.assertEqual((self.directory / "MentoringOnly.tex").read_text(), self.document)
        self.assertEqual((self.directory / "MentoringSection.tex").read_text(), self.section)
        self.assertFalse(list(self.repo.rglob("*.pdf")))
        self.assertFalse(list(self.repo.rglob("*.aux")))
        self.assertEqual(run_git(self.repo, "status", "--porcelain").stdout, b"")

    def test_nested_main_with_spaces_and_include_uses_its_parent_directory(self):
        self.directory.rename(self.repo / "Teaching notes")
        self.directory = self.repo / "Teaching notes"
        nested = self.directory / "More notes"
        nested.mkdir()
        for file in list(self.directory.iterdir()):
            if file.is_file():
                file.rename(nested / file.name)
        self.main = "Teaching notes/More notes/MentoringOnly.tex"
        (nested / "MentoringOnly.tex").write_text(self.document.replace("\\input{MentoringSection}", "\\include{MentoringSection}"))
        job = self.manager.start(self.main, "pdflatex", "working", None, None, strict=True)
        deadline = time.monotonic() + 60
        while self.manager.get(job["id"])["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.05)
        job = self.manager.get(job["id"])
        self.assertEqual(job["status"], "succeeded", job["log"])
        self.assertEqual(job["directory"], "Teaching notes/More notes")

    def test_marked_build_and_retry_use_the_same_main_directory(self):
        proposal = self.store.create({"Teaching/MentoringSection.tex": self.section.replace("Original mentoring", "Pending mentoring")}, "Edit mentoring")
        original = self.manager._run_process
        compile_calls = []
        def fail_first(job, command, **kwargs):
            if command[0] == job["latexmk_path"]:
                compile_calls.append((command[-1], kwargs["cwd"]))
                if len(compile_calls) == 1:
                    source = self.manager.root / job["id"] / "source" / self.main
                    source.write_text("\\PapreUndefinedColorTest\n" + source.read_text())
            return original(job, command, **kwargs)
        with patch.object(self.manager, "_run_process", side_effect=fail_first):
            job = self.compile(proposal)
        directory = self.manager.root / job["id"] / "source/Teaching"
        self.assertEqual(compile_calls, [("./MentoringOnly.tex", directory)] * 2)
        self.assertFalse(job["marked"])
        self.assertTrue(any("coloring was disabled" in warning for warning in job["warnings"]))
        self.assertEqual((self.directory / "MentoringOnly.tex").read_text(), self.document)
        self.assertEqual((self.directory / "MentoringSection.tex").read_text(), self.section)

    @unittest.skipUnless(PreviewManager.available().get("synctex") and PreviewManager.available()["renderer"], "Source navigation tools unavailable")
    def test_forward_reverse_and_relative_synctex_paths_keep_repository_file_names(self):
        proposal = self.store.create({"Teaching/MentoringSection.tex": self.section.replace("Original mentoring", "Pending mentoring")}, "Edit mentoring")
        job = self.compile(proposal)
        change = job["changes"][0]
        self.assertTrue(change["highlighted"])
        self.assertEqual(change["path"], "Teaching/MentoringSection.tex")
        self.assertIsNotNone(change["page"])
        width, height = struct.unpack(">II", self.manager.page(job["id"], str(change["page"])).read_bytes()[16:24])
        x, y = (change["x"] + 2) * 120 / 72 / width, (change["y"] - 2) * 120 / 72 / height
        mapped = self.manager.source_at(job["id"], change["page"], x, y)
        self.assertEqual(mapped["hunk"], change["id"])
        self.assertEqual(mapped["path"], "Teaching/MentoringSection.tex")
        # Some SyncTeX versions return paths relative to the compiler's working directory.
        with patch.object(self.manager, "_sync", return_value=[{"Input": "./MentoringSection.tex", "Line": "1"}]):
            mapped = self.manager.source_at(job["id"], change["page"], x, y)
        self.assertEqual(mapped["path"], "Teaching/MentoringSection.tex")
        with patch.object(self.manager, "_sync", return_value=[{"Input": "./MentoringOnly.tex", "Line": "6"}]):
            mapped = self.manager.source_at(job["id"], 1, .1, .1)
        self.assertEqual(mapped["path"], self.main)
        self.assertEqual(mapped["line"], 5)  # Excludes the preview-only color loader.
        with patch.object(self.manager, "_sync", return_value=[{"Input": "../../outside.tex", "Line": "1"}]):
            with self.assertRaises(ReviewError) as error:
                self.manager.source_at(job["id"], 1, .1, .1)
        self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
