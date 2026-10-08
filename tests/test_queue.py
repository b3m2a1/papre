import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from papre.core import Repository, ReviewError, ReviewStore, git_patch, run_git
from papre.queue import PatchQueue
from papre.server import ROOT


class QueueTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.before = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n"
        self.after = self.before.replace("two\n", "TWO\n").replace("nine\n", "NINE\n")
        (self.repo_path / "main.tex").write_text(self.before)
        for args in [("init",), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-m", "base")]:
            self.assertEqual(run_git(self.repo_path, *args).returncode, 0)
        self.store = ReviewStore(Repository(self.repo_path), self.root / "state", True)
        self.queue = PatchQueue(self.store)
        self.raw = git_patch({"main.tex": (self.before, self.after)})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enqueue_open(self):
        queued = self.queue.enqueue(self.raw, "Example")
        result = self.queue.open(queued["id"])
        self.assertIsNone(result["warning"])
        return queued, result["proposal"]

    def review(self, queued, proposal, decisions):
        return self.queue.review(queued["id"], proposal["revision"], decisions)

    def test_enqueue_only_and_external_file_discovery(self):
        queued = self.queue.enqueue(self.raw, "Example")
        self.assertEqual(self.store.list(), [])
        self.assertEqual((self.queue.root / queued["path"]).read_text(), self.raw)
        (self.queue.root / "another.diff").write_text(self.raw)
        self.assertEqual(len(self.queue.list()["entries"]), 2)
        self.assertEqual(self.store.repo.files(), ["main.tex"])
        self.assertNotIn("review_queue/another.diff", self.store.repo.files(False))
        self.assertEqual(self.store.repo.read("main.tex"), self.before)

    def test_edited_patch_preserves_original_then_rejection_excludes_edit(self):
        queued, proposal = self.enqueue_open()
        first, second = proposal["files"][0]["hunks"]
        proposal = self.review(queued, proposal, [{"id": first["id"], "new": "MY EDIT\n", "decision": "pending"}])["proposal"]
        current = self.queue.get(queued["id"])
        self.assertIn("MY EDIT", (self.queue.root / current["path"]).read_text())
        archived = [p.read_text() for p in (self.queue.root / "superseded").glob("*.patch")]
        self.assertIn(self.raw, archived)
        result = self.review(queued, proposal, [{"id": first["id"], "decision": "rejected"}, {"id": second["id"], "decision": "accepted"}])
        self.assertEqual(result["entry"]["status"], "processed")
        path = self.queue.root / result["entry"]["path"]
        self.assertTrue(path.is_file())
        self.assertNotIn("MY EDIT", path.read_text())
        self.assertIn("NINE", path.read_text())
        self.assertEqual(self.store.repo.read("main.tex"), self.before)
        receipt = json.loads(Path(str(path) + ".review.json").read_text())
        self.assertEqual(receipt["proposal"]["files"][0]["hunks"][0]["decision"], "rejected")
        applied = self.queue.apply(queued["id"], result["proposal"]["revision"])["proposal"]
        self.assertEqual(self.store.repo.read("main.tex"), self.before.replace("nine", "NINE"))
        self.assertEqual(run_git(self.repo_path, "diff", "--cached").stdout, b"")
        self.queue.apply(queued["id"], applied["revision"], undo=True)
        self.assertEqual(self.store.repo.read("main.tex"), self.before)

    def test_skip_keeps_patch_and_open_resumes_decisions(self):
        queued, proposal = self.enqueue_open()
        hunk = proposal["files"][0]["hunks"][0]
        proposal = self.review(queued, proposal, [{"id": hunk["id"], "decision": "accepted"}])["proposal"]
        self.queue.skip(queued["id"])
        row = self.queue.get(queued["id"])
        self.assertEqual(row["status"], "skipped")
        self.assertTrue((self.queue.root / row["path"]).is_file())
        resumed = self.queue.open(queued["id"])
        self.assertEqual(resumed["entry"]["status"], "reviewing")
        self.assertEqual(resumed["proposal"]["counts"]["accepted"], 1)

    def test_invalid_patch_warns_and_archive_moves_original(self):
        raw = "--- a/main.tex\n+++ b/main.tex\n@@ -1 +1 @@\n-not present\n+Updated\n"
        queued = self.queue.enqueue(raw, "Stale")
        result = self.queue.open(queued["id"])
        self.assertTrue(result["warning"])
        self.assertEqual(result["entry"]["status"], "invalid")
        archived = self.queue.archive(queued["id"])
        self.assertTrue(archived["path"].startswith("superseded/"))
        self.assertEqual((self.queue.root / archived["path"]).read_text(), raw)
        self.assertEqual(self.store.repo.read("main.tex"), self.before)

    def test_stale_repository_detected_before_review_mutates_patch(self):
        queued, proposal = self.enqueue_open()
        (self.repo_path / "main.tex").write_text(self.before + "newer work\n")
        with self.assertRaisesRegex(ReviewError, "changed since import"):
            self.review(queued, proposal, [{"id": proposal["files"][0]["hunks"][0]["id"], "decision": "accepted"}])
        self.assertEqual((self.queue.root / queued["path"]).read_text(), self.raw)
        self.assertTrue(self.queue.open(queued["id"])["warning"])

    def test_external_patch_edit_preserves_old_version_and_reimports(self):
        queued, original = self.enqueue_open()
        changed = git_patch({"main.tex": (self.before, self.before.replace("two", "OTHER"))})
        (self.queue.root / queued["path"]).write_text(changed)
        self.queue.scan()
        self.assertEqual(self.store.get(original["id"])["status"], "superseded")
        self.assertTrue(any(p.read_text() == self.raw for p in (self.queue.root / "superseded").iterdir()))
        result = self.queue.open(queued["id"])
        self.assertIsNone(result["warning"])
        self.assertIn("OTHER", self.store.render(result["proposal"], "all")["main.tex"])

    def test_reviewed_patch_can_be_rejected_or_reedited_before_apply(self):
        queued, proposal = self.enqueue_open()
        result = self.review(queued, proposal, [{"id": h["id"], "decision": "accepted"} for h in proposal["files"][0]["hunks"]])
        self.assertEqual(result["entry"]["status"], "processed")
        proposal = result["proposal"]
        first = proposal["files"][0]["hunks"][0]
        result = self.review(queued, proposal, [{"id": first["id"], "new": "EDIT AFTER REVIEW\n", "decision": "pending"}])
        self.assertEqual(result["entry"]["status"], "reviewing")
        self.assertNotIn("/", result["entry"]["path"])
        result = self.review(queued, result["proposal"], [{"id": first["id"], "decision": "rejected"}])
        self.assertEqual(result["entry"]["status"], "processed")
        self.assertNotIn("EDIT AFTER REVIEW", (self.queue.root / result["entry"]["path"]).read_text())

    def test_all_rejected_is_processed_without_source_changes(self):
        queued, proposal = self.enqueue_open()
        result = self.review(queued, proposal, [{"id": h["id"], "decision": "rejected"} for h in proposal["files"][0]["hunks"]])
        self.assertEqual(result["entry"]["status"], "processed")
        self.assertEqual((self.queue.root / result["entry"]["path"]).read_text(), "")
        self.assertEqual(self.store.repo.read("main.tex"), self.before)
        with self.assertRaisesRegex(ReviewError, "No accepted"):
            self.queue.apply(queued["id"], result["proposal"]["revision"])

    def test_filesystem_save_failure_preserves_review_and_patch(self):
        queued, proposal = self.enqueue_open()
        first = proposal["files"][0]["hunks"][0]
        with patch.object(self.queue, "_replace_file", side_effect=OSError("read-only filesystem")):
            with self.assertRaises(OSError):
                self.review(queued, proposal, [{"id": first["id"], "new": "EDIT\n", "decision": "pending"}])
        self.assertEqual(self.store.get(proposal["id"])["files"][0]["hunks"][0]["new"], first["new"])
        self.assertEqual((self.queue.root / queued["path"]).read_text(), self.raw)

    def test_queue_paths_and_symlink_folders_are_rejected(self):
        for name in ["../outside.patch", "/absolute.patch", "superseded/../../outside.patch", "a/b/c.patch"]:
            with self.assertRaises(ReviewError):
                self.queue.text(name)
        outside = self.root / "outside"
        outside.mkdir()
        (self.queue.root / "superseded").rmdir()
        (self.queue.root / "superseded").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ReviewError):
            self.queue.list()

    def test_receipt_failure_keeps_decisions_consistent_with_processed_patch(self):
        queued, proposal = self.enqueue_open()
        decisions = [{"id": h["id"], "decision": "accepted"} for h in proposal["files"][0]["hunks"]]
        with patch.object(self.queue, "_write_receipt", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.review(queued, proposal, decisions)
        row = self.queue.get(queued["id"])
        self.assertEqual(row["status"], "processed")
        saved = self.store.get(proposal["id"])
        self.assertEqual(self.store.counts(saved)["pending"], 0)
        self.assertEqual((self.queue.root / row["path"]).read_text(), self.store.patch(saved, "accepted"))
        self.assertIsNone(self.queue.open(queued["id"])["warning"])

    def test_move_failure_keeps_saved_edit_and_can_be_retried(self):
        queued, proposal = self.enqueue_open()
        first = proposal["files"][0]["hunks"][0]
        decisions = [{"id": h["id"], "decision": "accepted", **({"new": "EDIT\n"} if h["id"] == first["id"] else {})}
                     for h in proposal["files"][0]["hunks"]]
        with patch("papre.queue.os.rename", side_effect=OSError("move failed")):
            with self.assertRaises(OSError):
                self.review(queued, proposal, decisions)
        saved = self.store.get(proposal["id"])
        self.assertIn("EDIT", (self.queue.root / self.queue.get(queued["id"])["path"]).read_text())
        self.assertEqual(saved["files"][0]["hunks"][0]["new"], "EDIT\n")
        result = self.review(queued, saved, [{"id": first["id"], "decision": "accepted"}])
        self.assertEqual(result["entry"]["status"], "processed")


if __name__ == "__main__":
    unittest.main()
