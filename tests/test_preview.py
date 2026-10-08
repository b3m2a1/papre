from pathlib import Path
import tempfile
import unittest

from papre.preview import complete_pdf, compile_diagnostics
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


if __name__ == "__main__":
    unittest.main()
