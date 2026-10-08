import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote
from unittest.mock import patch

from papre.core import Repository, ReviewError, ReviewStore, git_patch, run_git
from papre.preview import PreviewManager
from papre.server import ROOT, ReviewHTTPServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.base = "\\documentclass{article}\n\\begin{document}\nOriginal manuscript.\n\\end{document}\n"
        (self.repo_path / "main.tex").write_text(self.base)
        run_git(self.repo_path, "init")
        run_git(self.repo_path, "symbolic-ref", "HEAD", "refs/heads/main")
        self.store = ReviewStore(Repository(self.repo_path), self.root / "state", True)
        self.server = ReviewHTTPServer(("127.0.0.1", 0), self.store, "main.tex", False, self.root, self.root / "states")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.get("/api/session")["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.url + path, timeout=10) as response:
            return json.load(response)

    def post(self, path, data, token=None, headers=None):
        request_headers = {"Content-Type": "application/json", "X-Review-Token": self.token if token is None else token}
        request_headers.update(headers or {})
        request = Request(self.url + path, data=json.dumps(data).encode(), headers=request_headers, method="POST")
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def wait_preview(self, job):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            job = self.get("/api/previews/" + job["id"])
            if job["status"] != "running":
                return job
            time.sleep(.1)
        self.fail("Preview did not finish within 35 seconds")

    def test_host_token_and_origin_protection(self):
        for headers, token in [({}, "bad-token"), ({"Origin": "https://other.example"}, self.token),
                               ({"Host": "other.example"}, self.token)]:
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as caught:
                self.post("/api/context", {"files": ["main.tex"]}, token=token, headers=headers)
            self.assertEqual(caught.exception.code, 403)

    def test_import_review_export_and_apply_over_http(self):
        patch = git_patch({"main.tex": (self.base, self.base.replace("Original", "Suggested"))})
        queued = self.post("/api/import", {"patch": patch, "title": "HTTP test"})
        self.assertTrue((self.repo_path / "review_queue" / queued["path"]).exists())
        self.assertEqual(self.store.list(), [])
        p = self.post(f"/api/queue/{queued['id']}/open", {})["proposal"]
        hunk = p["files"][0]["hunks"][0]
        result = self.post(f"/api/queue/{queued['id']}/review", {"revision": p["revision"], "decisions": [
            {"id": hunk["id"], "decision": "accepted", "new": "Human edited manuscript.\n"}]})
        p = result["proposal"]
        self.assertTrue(result["entry"]["path"].startswith("processed/"))
        self.assertEqual((self.repo_path / "main.tex").read_text(), self.base)
        with urlopen(self.url + f"/api/proposals/{p['id']}/patch") as response:
            self.assertIn(b"Human edited manuscript", response.read())
        self.post(f"/api/queue/{queued['id']}/apply", {"revision": p["revision"]})
        self.assertIn("Human edited manuscript", (self.repo_path / "main.tex").read_text())

    @unittest.skipUnless(PreviewManager.available()["latexmk"] and "pdflatex" in PreviewManager.available()["engines"], "LaTeX unavailable")
    def test_preview_uses_snapshot_and_failed_build_has_no_pdf(self):
        queued = self.post("/api/import", {"patch": git_patch({"main.tex": (self.base, self.base.replace("Original", "Suggested"))})})
        p = self.post(f"/api/queue/{queued['id']}/open", {})["proposal"]
        job = self.post("/api/previews", {"main": "main.tex", "engine": "pdflatex", "selection": "proposed",
                                         "proposal": p["id"], "revision": p["revision"]})
        job = self.wait_preview(job)
        self.assertEqual(job["status"], "succeeded", job["log"])
        self.assertEqual((self.repo_path / "main.tex").read_text(), self.base)
        self.assertIn("Suggested", (self.root / "state/builds" / job["id"] / "source/main.tex").read_text())
        self.assertFalse((self.repo_path / "main.pdf").exists())
        with urlopen(self.url + job["pdf"]) as response:
            self.assertEqual(response.headers["Content-Type"], "application/pdf")
            self.assertTrue(response.read().startswith(b"%PDF"))
        if PreviewManager.available()["renderer"]:
            self.assertTrue(job["pages"], job["warnings"])
            with urlopen(self.url + job["pages"][0]) as response:
                self.assertEqual(response.headers["Content-Type"], "image/png")
                self.assertTrue(response.read().startswith(b"\x89PNG"))
        queued = self.post("/api/import", {"edits": [{"path": "main.tex", "search": "Original manuscript.",
                                                   "replace": "\\ThisCommandDoesNotExist"}]})
        bad = self.post(f"/api/queue/{queued['id']}/open", {})["proposal"]
        job = self.post("/api/previews", {"main": "main.tex", "engine": "pdflatex", "selection": "proposed",
                                         "proposal": bad["id"], "revision": bad["revision"], "strict": True})
        job = self.wait_preview(job)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Undefined control sequence", job["log"])
        self.assertIsNone(job["pdf"])
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + "/api/previews/" + job["id"] + "/pdf")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual((self.repo_path / "main.tex").read_text(), self.base)

    @unittest.skipUnless(PreviewManager.available()["latexmk"] and "pdflatex" in PreviewManager.available()["engines"], "LaTeX unavailable")
    def test_recoverable_errors_still_serve_a_new_pdf_and_missing_packages_do_not(self):
        source = self.repo_path / "main.tex"
        source.write_text(self.base.replace("Original manuscript.", "Original manuscript.\\ThisCommandDoesNotExist\nMore text."))
        job = self.wait_preview(self.post("/api/previews", {"main": "main.tex", "selection": "working"}))
        self.assertEqual(job["status"], "with_errors", job["log"])
        self.assertFalse(job["strict"])
        self.assertNotEqual(job["exit_code"], 0)
        self.assertIn("Undefined control sequence", job["error"])
        self.assertTrue(job["warnings"])
        with urlopen(self.url + job["pdf"]) as response:
            self.assertTrue(response.read().startswith(b"%PDF"))
        if job["pages"]:
            with urlopen(self.url + job["pages"][0]) as response:
                self.assertTrue(response.read().startswith(b"\x89PNG"))
        # A project PDF from an earlier compile cannot become this build's output.
        (self.repo_path / "main.pdf").write_bytes(b"%PDF-1.5\nold document\n%%EOF\n")
        source.write_text(self.base.replace("\\begin{document}", "\\usepackage{papre-package-that-does-not-exist}\n\\begin{document}"))
        job = self.wait_preview(self.post("/api/previews", {"main": "main.tex", "selection": "working"}))
        self.assertEqual(job["status"], "failed", job["log"])
        self.assertEqual(job["missing_packages"], ["papre-package-that-does-not-exist.sty"])
        self.assertIn("Required LaTeX package", job["error"])
        self.assertIsNone(job["pdf"])
        with self.assertRaises(HTTPError):
            urlopen(self.url + "/api/previews/" + job["id"] + "/pdf")
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/previews", {"main": "main.tex", "strict": "false"})
        self.assertEqual(caught.exception.code, 400)

    def test_directory_browser_attach_and_token_rotation(self):
        listing = self.get("/api/directories")
        self.assertEqual(listing["path"], str(self.root))
        self.assertTrue(any(p["path"] == str(self.repo_path) and p["is_repo"] for p in listing["directories"]))
        with self.assertRaises(HTTPError):
            self.get("/api/directories?path=/")
        new_repo = self.root / "second"
        new_repo.mkdir()
        (new_repo / "article.tex").write_text(self.base)
        run_git(new_repo, "init")
        old_token = self.token
        selected = self.post("/api/repository", {"path": str(new_repo), "allow_write": False})
        self.assertEqual(selected["repo"]["root"], str(new_repo))
        self.assertEqual(selected["main"], "article.tex")
        self.assertNotEqual(selected["token"], old_token)
        self.assertTrue((new_repo / "review_queue/processed").is_dir())
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/import", {"patch": "invalid"}, token=old_token)
        self.assertEqual(caught.exception.code, 403)

    def test_unrestricted_browser_can_leave_launch_directory_and_expand_home(self):
        with patch("papre.server.Path.cwd", return_value=self.repo_path):
            server = ReviewHTTPServer(("127.0.0.1", 0), state_base=self.root / "states")
        try:
            self.assertIsNone(server.session()["browse_root"])
            self.assertEqual(server.browse()["parent"], str(self.root))
            self.assertEqual(server.browse(str(self.root))["path"], str(self.root))
            with patch.dict("os.environ", {"HOME": str(self.root), "USERPROFILE": str(self.root)}):
                self.assertEqual(server.browse("~/repo")["path"], str(self.repo_path))
                session = server.attach("~/repo")
                self.assertEqual(session["repo"]["root"], str(self.repo_path))
            self.assertIsNone(server.browse(str(self.root.anchor))["parent"])
        finally:
            server.server_close()

    def test_typed_home_path_and_missing_directory(self):
        with patch.dict("os.environ", {"HOME": str(self.root), "USERPROFILE": str(self.root)}):
            listing = self.get("/api/directories?path=" + quote("~/repo"))
        self.assertEqual(listing["path"], str(self.repo_path))
        with self.assertRaises(HTTPError) as caught:
            self.get("/api/directories?path=" + quote(str(self.root / "missing")))
        self.assertIn("does not exist", json.load(caught.exception)["error"])

    @patch("papre.server.pick_directory")
    def test_native_picker_validation_cancel_and_token_guard(self, picker):
        picker.return_value = self.repo_path
        result = self.post("/api/directory-picker", {"path": str(self.root)})
        self.assertEqual(result["directory"]["path"], str(self.repo_path))
        self.assertFalse(result["cancelled"])
        self.assertEqual(self.server.store, self.store)
        picker.assert_called_once_with(self.root)
        picker.return_value = None
        self.assertTrue(self.post("/api/directory-picker", {})["cancelled"])
        picker.return_value = Path(self.root.anchor)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/directory-picker", {})
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/directory-picker", {}, token="invalid")
        self.assertEqual(caught.exception.code, 403)
        self.server.picker_lock.acquire()
        try:
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/directory-picker", {})
            self.assertEqual(caught.exception.code, 409)
        finally:
            self.server.picker_lock.release()
        picker.side_effect = ReviewError("Picker unavailable", 503)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/directory-picker", {})
        self.assertEqual(caught.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
