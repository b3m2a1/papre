import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from papre.core import Repository, ReviewError, ReviewStore, digest, git_patch, run_git
from papre.preview import PreviewManager
from papre.queue import PatchQueue
from papre.server import ROOT


class SourceEditTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.base = "\\section{First}\nFirst line.\nSecond line.\n\nUnchanged paragraph.\nMore context.\n\n\\section{Second}\nLast line.\n"
        (self.repo / "main.tex").write_text(self.base)
        (self.repo / "body.tex").write_text("Another source file.\n")
        for args in [("init",), ("add", "."), ("-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
                     "-c", "core.hooksPath=/dev/null", "commit", "-m", "base")]:
            self.assertEqual(run_git(self.repo, *args).returncode, 0)
        self.store = ReviewStore(Repository(self.repo), self.root / "state", True)
        self.queue = PatchQueue(self.store)
        self.raw = git_patch({"main.tex": (self.base, self.base.replace("First line.", "Suggested first.").replace("Last line.", "Suggested last."))})
        self.entry = self.queue.enqueue(self.raw, "Current review")
        self.proposal = self.queue.open(self.entry["id"])["proposal"]
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def region(self, line=5, path="main.tex", scope="paragraph", current=True):
        return self.store.source_region(path, line, self.proposal["id"] if current else None,
                                        self.proposal["revision"] if current else None, scope)

    def save(self, region, new, current=True):
        data = region | {"new": new}
        if current:
            data["entry"] = self.entry["id"]
        return self.queue.edit_source(data)

    def test_region_and_larger_section_stop_at_existing_changes(self):
        region = self.region()
        self.assertEqual((region["start"], region["end"]), (4, 6))
        self.assertEqual(region["before"], "Unchanged paragraph.\nMore context.\n")
        section = self.region(scope="section")
        self.assertEqual((section["start"], section["end"]), (2, 7))
        self.assertNotIn("First line.", section["before"])
        hunk = self.region(line=2)
        self.assertEqual(hunk["hunk"], self.proposal["files"][0]["hunks"][0]["id"])

    def test_saving_preserves_decisions_ids_and_original_patch(self):
        first, last = self.proposal["files"][0]["hunks"]
        decisions = [{"id": first["id"], "decision": "accepted"}, {"id": last["id"], "decision": "rejected"}]
        result = self.queue.review(self.entry["id"], self.proposal["revision"], decisions)
        self.proposal = result["proposal"]
        self.assertEqual(result["entry"]["status"], "processed")
        old_patch = self.queue.text(result["entry"]["path"])
        saved = self.save(self.region(), "My new paragraph.\nMore context.\n")
        self.assertEqual(saved["entry"]["status"], "reviewing")
        self.assertNotIn("/", saved["entry"]["path"])
        first_now, fresh, last_now = saved["proposal"]["files"][0]["hunks"]
        self.assertEqual((first_now["id"], first_now["decision"]), (first["id"], "accepted"))
        self.assertEqual((last_now["id"], last_now["decision"]), (last["id"], "rejected"))
        self.assertEqual((fresh["start"], fresh["end"], fresh["decision"]), (4, 5, "pending"))
        self.assertIn(old_patch, [path.read_text() for path in (self.queue.root / "superseded").glob("*.patch")])
        self.assertEqual((self.repo / "main.tex").read_text(), self.base)
        done = self.queue.review(self.entry["id"], saved["proposal"]["revision"], [{"id": fresh["id"], "decision": "rejected"}])
        self.queue.apply(self.entry["id"], done["proposal"]["revision"])
        self.assertEqual((self.repo / "main.tex").read_text(), self.base.replace("First line.", "Suggested first."))
        self.assertEqual(run_git(self.repo, "diff", "--cached").stdout, b"")

    def test_disjoint_edits_and_insertions_reconcile_without_resetting_review(self):
        original_hunks = copy.deepcopy(self.proposal["files"][0]["hunks"])
        section = self.region(scope="section")
        new = section["before"].replace("Second line.", "New second line.").replace("More context.", "More context.\nAn inserted line.")
        result = self.save(section, new)
        hunks = result["proposal"]["files"][0]["hunks"]
        self.assertEqual(len(hunks), 4)
        for original in original_hunks:
            self.assertIn(original, hunks)
        expected = self.store.render(self.proposal, "all")["main.tex"].replace("Second line.", "New second line.").replace("More context.", "More context.\nAn inserted line.")
        self.assertEqual(self.store.render(result["proposal"], "all")["main.tex"], expected)
        self.assertEqual(run_git(self.repo, "apply", "--check", "-", patch=self.store.patch(result["proposal"], "all")).returncode, 0)

    def test_other_source_file_joins_current_patch_and_can_be_rejected(self):
        region = self.region(line=1, path="body.tex")
        result = self.save(region, "My additional edit.\n")
        self.assertEqual([file["path"] for file in result["proposal"]["files"]], ["main.tex", "body.tex"])
        self.assertEqual((self.repo / "body.tex").read_text(), "Another source file.\n")
        fresh = result["proposal"]["files"][-1]["hunks"][0]
        result = self.queue.review(self.entry["id"], result["proposal"]["revision"], [{"id": fresh["id"], "decision": "rejected"}])
        self.assertEqual(self.store.render(result["proposal"], "proposed")["body.tex"], region["before"])

    def test_without_current_review_creates_a_new_queued_patch(self):
        region = self.region(current=False)
        result = self.save(region, "An independent suggestion.\n", current=False)
        self.assertNotEqual(result["entry"]["id"], self.entry["id"])
        self.assertEqual(result["proposal"]["counts"]["pending"], 1)
        self.assertIn("An independent suggestion.", self.queue.text(result["entry"]["path"]))
        self.assertEqual((self.repo / "main.tex").read_text(), self.base)

    def test_noop_does_not_change_revision_or_archive(self):
        region = self.region()
        result = self.save(region, region["before"])
        self.assertEqual(result["proposal"]["revision"], self.proposal["revision"])
        self.assertEqual(self.queue.list()["archives"], [])

    def test_stale_revision_source_and_external_queue_edits_are_refused(self):
        region = self.region()
        self.store.update(self.proposal["id"], self.proposal["revision"], [{"id": self.proposal["files"][0]["hunks"][0]["id"], "decision": "accepted"}])
        with self.assertRaises(ReviewError) as caught:
            self.save(region, "Stale review.\n")
        self.assertEqual(caught.exception.status, 409)
        region = self.region(current=False)
        (self.repo / "main.tex").write_text(self.base + "External edit.\n")
        with self.assertRaisesRegex(ReviewError, "Source changed"):
            self.save(region, "Stale source.\n", current=False)
        with self.assertRaisesRegex(ReviewError, "Source changed"):
            self.store.source_region("main.tex", 5, expected_digest=region["base_digest"])
        (self.repo / "main.tex").write_text(self.base)
        self.proposal = self.store.get(self.proposal["id"])
        region = self.region()
        (self.queue.root / self.entry["path"]).write_text(self.raw + "\n")
        with self.assertRaisesRegex(ReviewError, "queue file changed"):
            self.save(region, "Conflicting queue.\n")

    def test_tampered_ranges_overlap_text_and_paths_are_refused(self):
        region = self.region()
        for data in [region | {"start": True}, region | {"end": 900}, region | {"before": "not the original"},
                     region | {"new": "CRLF\r\n"}, region | {"path": "../main.tex"}, region | {"path": "review_queue/one.tex"},
                     region | {"start": 0, "end": 3, "before": "".join(self.base.splitlines(keepends=True)[:3])}]:
            with self.subTest(data=data), self.assertRaises(ReviewError):
                self.save(data, data.get("new", "Changed.\n"))
        (self.repo / "link.tex").symlink_to(self.repo / "main.tex")
        with self.assertRaises(ReviewError):
            self.store.source_region("link.tex", 1)
        self.assertEqual(self.store.get(self.proposal["id"])["revision"], self.proposal["revision"])

    def test_very_long_sections_are_bounded_and_malformed_region_requests_are_refused(self):
        (self.repo / "long.tex").write_text("\\section{Long}\n" + "Context line.\n" * 600)
        region = self.store.source_region("long.tex", 350, scope="section")
        self.assertEqual(region["end"] - region["start"], 400)
        self.assertTrue(region["start"] <= 349 < region["end"])
        for line, scope, proposal, revision in [(True, "paragraph", None, None), (999, "paragraph", None, None),
                                               (5, [], None, None), (5, "paragraph", [], 1),
                                               (5, "paragraph", self.proposal["id"], True)]:
            with self.subTest(line=line, scope=scope), self.assertRaises(ReviewError):
                self.store.source_region("main.tex", line, proposal, revision, scope)

    def test_failure_to_save_queue_rolls_back_new_changes(self):
        region = self.region()
        with patch.object(self.queue, "_replace_file", side_effect=OSError("Write failed")):
            with self.assertRaises(OSError):
                self.save(region, "Cannot be saved.\n")
        current = self.store.get(self.proposal["id"])
        self.assertEqual(current["files"], self.proposal["files"])
        self.assertGreater(current["revision"], self.proposal["revision"])
        self.assertEqual(self.queue.text(self.entry["path"]), self.raw)

    def test_block_boundaries_and_missing_final_newline_are_preserved(self):
        region = self.region()
        result = self.save(region, "Replacement without typed newline.")
        self.assertIn("Replacement without typed newline.\n\n\\section", self.store.render(result["proposal"], "all")["main.tex"])
        (self.repo / "body.tex").write_text("No final newline")
        region = self.region(line=1, path="body.tex", current=False)
        result = self.save(region, "Still no final newline", current=False)
        self.assertEqual(self.store.render(result["proposal"], "all")["body.tex"], "Still no final newline")

    def test_preview_mapping_undoes_insertions_deletions_and_rejected_offsets(self):
        proposal = {"files": [{"path": "main.tex", "hunks": [
            {"id": "insert", "start": 1, "end": 1, "old": "", "new": "a\nb\n", "decision": "pending"},
            {"id": "delete", "start": 3, "end": 4, "old": "old\n", "new": "", "decision": "accepted"},
            {"id": "rejected", "start": 5, "end": 6, "old": "old\n", "new": "x\ny\nz\n", "decision": "rejected"}]}]}
        self.assertEqual(PreviewManager._baseline_line(proposal, "main.tex", 2, "proposed"), ("insert", 2))
        self.assertEqual(PreviewManager._baseline_line(proposal, "main.tex", 4, "proposed"), (None, 2))
        self.assertEqual(PreviewManager._baseline_line(proposal, "main.tex", 6, "proposed"), (None, 5))
        self.assertEqual(PreviewManager._baseline_line(proposal, "main.tex", 7, "proposed"), ("rejected", 6))
        self.assertEqual(PreviewManager._baseline_line(proposal, "body.tex", 4, "proposed"), (None, 4))


if __name__ == "__main__":
    unittest.main()
