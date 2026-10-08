from pathlib import Path
import shutil
import tempfile
import unittest

from papre.core import Repository, ReviewError, ReviewStore, git_patch, run_git
from papre.server import ROOT


class CoreTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".runtime/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.repo_path = self.root / "repo"
        self.repo_path.mkdir()
        self.base = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n"
        (self.repo_path / "main.tex").write_text(self.base)
        (self.repo_path / "references.bib").write_text("@misc{demo, title={Demo}}\n")
        (self.repo_path / "unrelated.txt").write_text("keep me\n")
        for args in [("init",), ("symbolic-ref", "HEAD", "refs/heads/main"), ("add", "."),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                      "-c", "core.hooksPath=/dev/null", "commit", "-m", "base")]:
            result = run_git(self.repo_path, *args)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.repo = Repository(self.repo_path)
        self.state = self.root / "state"
        self.store = ReviewStore(self.repo, self.state, True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def proposal(self):
        after = self.base.replace("two\n", "TWO\n").replace("nine\n", "NINE\n")
        return self.store.import_patch(git_patch({"main.tex": (self.base, after)}), "Test proposal")

    def decide(self, p, decisions=None):
        decisions = decisions or [{"id": h["id"], "decision": "accepted"} for f in p["files"] for h in f["hunks"]]
        return self.store.update(p["id"], p["revision"], decisions)

    def test_import_isolated_and_selective_apply_preserves_index_and_dirty_work(self):
        (self.repo_path / "unrelated.txt").write_text("uncommitted user change\n")
        run_git(self.repo_path, "add", "unrelated.txt")
        before_index = run_git(self.repo_path, "diff", "--cached").stdout
        p = self.proposal()
        self.assertEqual(self.repo.read("main.tex"), self.base)
        first, second = p["files"][0]["hunks"]
        p = self.decide(p, [{"id": first["id"], "decision": "accepted", "new": "Edited TWO\n"},
                            {"id": second["id"], "decision": "rejected"}])
        self.assertEqual(self.repo.read("main.tex"), self.base)
        p = self.store.apply(p["id"], p["revision"])
        self.assertEqual(p["status"], "applied")
        self.assertEqual(self.repo.read("main.tex"), self.base.replace("two\n", "Edited TWO\n"))
        self.assertEqual(run_git(self.repo_path, "diff", "--cached").stdout, before_index)
        self.assertEqual((self.repo_path / "unrelated.txt").read_text(), "uncommitted user change\n")

    def test_dirty_source_is_baseline_not_head(self):
        dirty = self.base + "user addition\n"
        (self.repo_path / "main.tex").write_text(dirty)
        p = self.store.import_patch(git_patch({"main.tex": (dirty, dirty.replace("two", "TWO"))}), "Dirty")
        p = self.decide(p)
        self.store.apply(p["id"], p["revision"])
        self.assertEqual(self.repo.read("main.tex"), dirty.replace("two", "TWO"))

    def test_changed_source_refuses_apply_and_preview_baseline(self):
        p = self.decide(self.proposal())
        changed = self.base + "newer edit\n"
        (self.repo_path / "main.tex").write_text(changed)
        with self.assertRaisesRegex(ReviewError, "changed since import"):
            self.store.apply(p["id"], p["revision"])
        self.assertEqual(self.repo.read("main.tex"), changed)
        with self.assertRaises(ReviewError):
            self.store.verify_base(p)

    def test_readonly_refuses_apply(self):
        p = self.decide(self.proposal())
        self.store.allow_write = False
        with self.assertRaisesRegex(ReviewError, "read-only"):
            self.store.apply(p["id"], p["revision"])
        self.assertEqual(self.repo.read("main.tex"), self.base)

    def test_pending_changes_must_be_decided(self):
        p = self.proposal()
        with self.assertRaisesRegex(ReviewError, "every change"):
            self.store.apply(p["id"], p["revision"])

    def test_competing_revision_is_rejected_without_overwriting(self):
        p = self.proposal()
        updated = self.decide(p)
        with self.assertRaisesRegex(ReviewError, "another tab"):
            self.store.update(p["id"], p["revision"], [{"id": p["files"][0]["hunks"][0]["id"], "decision": "rejected"}])
        self.assertEqual(self.store.get(p["id"]), updated)

    def test_undo_only_if_applied_source_unchanged(self):
        p = self.decide(self.proposal())
        p = self.store.apply(p["id"], p["revision"])
        applied = self.repo.read("main.tex")
        (self.repo_path / "main.tex").write_text(applied + "new work\n")
        with self.assertRaisesRegex(ReviewError, "newer work"):
            self.store.undo(p["id"], p["revision"])
        (self.repo_path / "main.tex").write_text(applied)
        p = self.store.undo(p["id"], p["revision"])
        self.assertEqual(p["status"], "undone")
        self.assertEqual(self.repo.read("main.tex"), self.base)

    def test_paths_and_symlinks_are_rejected(self):
        for name in ["../escape.tex", "/tmp/escape.tex", ".git/config", "a/../../main.tex", "a//main.tex", "a\\b.tex"]:
            with self.subTest(name=name), self.assertRaises(ReviewError):
                self.repo.path(name)
        outside = self.root / "outside.tex"
        outside.write_text("secret\n")
        (self.repo_path / "link.tex").symlink_to(outside)
        with self.assertRaisesRegex(ReviewError, "Symlinks"):
            self.repo.read("link.tex")
        malicious = "--- a/../outside.tex\n+++ b/../outside.tex\n@@ -1 +1 @@\n-secret\n+changed\n"
        with self.assertRaises(ReviewError):
            self.store.import_patch(malicious, "Escape")
        self.assertEqual(outside.read_text(), "secret\n")

    def test_modes_new_files_and_binary_changes_are_rejected(self):
        for prefix in ["new file mode 100644", "deleted file mode 100644", "old mode 100644", "GIT binary patch", "rename from main.tex"]:
            with self.subTest(prefix=prefix), self.assertRaises(ReviewError):
                self.store.import_patch(prefix + "\n" + git_patch({"main.tex": (self.base, self.base.upper())}), "Unsupported")

    def test_invalid_context_does_not_modify_source(self):
        patch = "--- a/main.tex\n+++ b/main.tex\n@@ -1 +1 @@\n-does not exist\n+change\n"
        with self.assertRaisesRegex(ReviewError, "could not be applied"):
            self.store.import_patch(patch, "Bad patch")
        self.assertEqual(self.repo.read("main.tex"), self.base)

    def test_json_unique_anchors_and_latex_backslashes(self):
        before = "\\section{Results}\nRepeat.\nRepeat.\n"
        (self.repo_path / "main.tex").write_text(before)
        with self.assertRaisesRegex(ReviewError, "exactly once"):
            self.store.import_json({"edits": [{"path": "main.tex", "search": "Repeat.", "replace": "New."}]}, "Ambiguous")
        p = self.store.import_json({"edits": [{"path": "main.tex", "search": "\\section{Results}", "replace": "\\section{Discussion}"}]}, "LaTeX")
        self.assertEqual(self.store.render(p, "proposed")["main.tex"], before.replace("Results", "Discussion"))

    def test_no_final_newline_patch_roundtrip(self):
        (self.repo_path / "main.tex").write_text("before")
        p = self.store.import_patch(git_patch({"main.tex": ("before", "after")}), "No newline")
        p = self.decide(p)
        self.store.apply(p["id"], p["revision"])
        self.assertEqual(self.repo.read("main.tex"), "after")

    def test_insertion_deletion_and_multiple_files(self):
        targets = {"main.tex": "inserted\n" + self.base.replace("four\n", ""),
                   "references.bib": "@misc{demo, title={Edited}}\n"}
        p = self.store.import_patch(git_patch({name: (self.repo.read(name), after) for name, after in targets.items()}), "Multiple")
        self.assertEqual(self.store.render(p, "proposed"), targets)
        self.assertEqual(self.store.render(p, "accepted")["main.tex"], self.base)
        p = self.decide(p)
        self.store.apply(p["id"], p["revision"])
        for name, after in targets.items():
            self.assertEqual(self.repo.read(name), after)

    def test_spaces_in_filename(self):
        (self.repo_path / "section one.tex").write_text("Old\n")
        p = self.store.import_patch(git_patch({"section one.tex": ("Old\n", "New\n")}), "Spaces")
        p = self.decide(p)
        self.store.apply(p["id"], p["revision"])
        self.assertEqual(self.repo.read("section one.tex"), "New\n")

    def test_decisions_persist_after_restart(self):
        p = self.decide(self.proposal())
        self.store.close()
        self.store = ReviewStore(self.repo, self.state, True)
        self.assertEqual(self.store.get(p["id"]), p)
        self.assertEqual(self.store.events()[0]["action"], "review")

    def test_crash_recovery_classifies_without_writing(self):
        p = self.decide(self.proposal())
        p["recovery_after"] = self.store.render(p)
        p["status"] = "applying"
        self.store._save(p, "simulate-interruption")
        (self.repo_path / "main.tex").write_text(p["recovery_after"]["main.tex"])
        self.store.recover()
        self.assertEqual(self.store.get(p["id"])["status"], "applied")
        self.assertEqual(self.repo.read("main.tex"), p["recovery_after"]["main.tex"])

    def test_ambiguous_crash_recovery_is_locked(self):
        p = self.decide(self.proposal())
        p.update(status="applying", recovery_after=self.store.render(p))
        self.store._save(p, "simulate-interruption")
        (self.repo_path / "main.tex").write_text("later user work\n")
        self.store.recover()
        self.assertEqual(self.store.get(p["id"])["status"], "recovery-needed")
        self.assertEqual(self.repo.read("main.tex"), "later user work\n")

    def test_state_cannot_be_inside_manuscript(self):
        with self.assertRaisesRegex(ReviewError, "outside"):
            ReviewStore(self.repo, self.repo_path / "state")

    def test_import_sandbox_does_not_discover_an_enclosing_git_repo(self):
        # The app itself may be a Git checkout, with .runtime nested beneath it.
        self.assertEqual(run_git(self.root, "init").returncode, 0)
        p = self.proposal()
        self.assertIn("TWO", self.store.render(p, "proposed")["main.tex"])
        self.assertEqual(self.repo.read("main.tex"), self.base)


if __name__ == "__main__":
    unittest.main()
