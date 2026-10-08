"""Real-browser verification of the review queue using disposable draft files."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from playwright.sync_api import sync_playwright, expect
from papre.core import run_git
from papre.server import DEMO, ROOT, ReviewHTTPServer


def main():
    qa = ROOT / ".runtime/qa"
    qa.mkdir(parents=True, exist_ok=True)
    temp_parent = ROOT / ".runtime/tests"
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_parent) as folder:
        base = Path(folder)
        repo_path = base / "manuscript"
        repo_path.mkdir()
        for name in ["main.tex", "references.bib", ".gitignore"]:
            shutil.copyfile(DEMO / name, repo_path / name)
        for args in [("init",), ("symbolic-ref", "HEAD", "refs/heads/main"), ("add", "."),
                     ("-c", "user.name=QA", "-c", "user.email=qa@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-m", "demo")]:
            result = run_git(repo_path, *args)
            assert result.returncode == 0, result.stderr.decode()
        before = (repo_path / "main.tex").read_text()
        original_patch = (DEMO / "example.patch").read_text()
        queue = repo_path / "review_queue"
        server = ReviewHTTPServer(("127.0.0.1", 0), browse_root=base, state_base=base / "state")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1600, "height": 1100})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page).to_have_title("Paper Patch Review Editor")
                expect(page.locator("#repo-dialog dialog")).to_be_visible()
                page.screenshot(path=str(qa / "directory-picker.png"))
                page.locator("#directory-list").get_by_role("button", name="manuscript Git repository").click()
                page.locator("#repo-write").check()
                page.get_by_role("button", name="Attach repository", exact=True).click()
                expect(page.locator("#repo-dialog dialog")).not_to_be_visible()
                assert (queue / "processed").is_dir() and (queue / "superseded").is_dir()
                assert page.evaluate("getComputedStyle(document.body).backgroundColor") in ["rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"]
                assert page.evaluate("getComputedStyle(document.documentElement).backgroundColor") == "rgb(255, 255, 255)"

                def import_patch(title, patch):
                    page.get_by_role("button", name="Import LLM patch", exact=True).click()
                    page.locator("#import-title").fill(title)
                    page.locator("#patch-input").fill(patch)
                    page.get_by_role("button", name="Add to queue", exact=True).click()
                    expect(page.locator("#import-dialog dialog")).not_to_be_visible()

                import_patch("Example manuscript edits", original_patch)
                expect(page.locator("article.hunk")).to_have_count(3)
                assert (queue / "Example-manuscript-edits.patch").read_text() == original_patch
                assert (repo_path / "main.tex").read_text() == before
                page.screenshot(path=str(qa / "review-desktop.png"), full_page=True)

                # Context beyond the patch is expandable, and full source stays read-only.
                page.locator("#context-size").select_option("3")
                expect(page.locator(".context-gap").first).to_be_visible()
                page.locator(".context-gap").first.get_by_role("button").click()
                expect(page.locator("#changes")).to_contain_text("documentclass")
                page.get_by_role("button", name="Open full file", exact=True).click()
                expect(page.locator("#source-text")).to_contain_text("bibliographystyle")
                expect(page.locator("#source-view textarea")).to_have_count(0)
                page.get_by_role("button", name="Back to patch", exact=True).click()
                page.locator("#context-size").select_option("all")
                expect(page.locator(".context-gap")).to_have_count(0)
                page.locator("#context-size").select_option("10")

                # Save modifies the queued patch, preserves the prior version, and allows Reject.
                first = page.locator("article.hunk").nth(0)
                first.get_by_role("button", name="Edit", exact=True).click()
                replacement = "For a positive transition distance, the model predicts a higher\nreaction rate as the applied force increases.\n"
                expect(first.locator(".editor-original textarea")).to_have_count(0)
                first.get_by_role("textbox").fill(replacement)
                page.screenshot(path=str(qa / "edit-update.png"), full_page=True)
                first.get_by_role("button", name="Save edit", exact=True).click()
                expect(first.locator(".hunk-meta")).to_contain_text("edited")
                assert any(p.read_text() == original_patch for p in (queue / "superseded").glob("*.patch"))
                assert "higher" in (queue / "Example-manuscript-edits.patch").read_text()
                first.get_by_role("button", name="Reject", exact=True).click()
                expect(page.locator("article.hunk.rejected")).to_have_count(1)
                page.locator("article.hunk").nth(1).get_by_role("button", name="Accept", exact=True).click()
                expect(page.locator("article.hunk.accepted")).to_have_count(1)
                page.locator("article.hunk").nth(2).get_by_role("button", name="Accept", exact=True).click()
                expect(page.locator("article.hunk.accepted")).to_have_count(2)
                expect(page.get_by_role("button", name="Apply accepted changes", exact=True)).to_be_enabled()
                final_path = queue / "processed/Example-manuscript-edits.patch"
                assert final_path.is_file() and not (queue / final_path.name).exists()
                assert final_path.with_name(final_path.name + ".review.json").is_file()
                assert "higher" not in final_path.read_text()
                assert "coupling term" in final_path.read_text()

                # Persistence and compilation from an isolated accepted-changes snapshot.
                page.reload()
                expect(page.locator("article.hunk.accepted")).to_have_count(2)
                expect(page.locator("article.hunk.rejected")).to_have_count(1)
                assert (repo_path / "main.tex").read_text() == before
                page.get_by_role("button", name="PDF preview", exact=True).click()
                expect(page.locator("#compiler-status")).to_contain_text("pdflatex")
                expect(page.locator("#latex-install")).to_have_attribute("href", server.session()["compiler"]["installation"]["url"])
                page.locator("#preview-selection").select_option("accepted")
                page.get_by_role("button", name="Compile preview", exact=True).click()
                expect(page.locator("#build-badge")).to_contain_text("Ready", timeout=45000)
                expect(page.locator("#pdf-pages img").first).to_be_visible()
                page.wait_for_function("document.querySelector('#pdf-pages img')?.naturalWidth > 0")
                response = context.request.get(f"http://127.0.0.1:{server.server_port}" + page.locator("#open-pdf").get_attribute("href"))
                assert response.body().startswith(b"%PDF")
                assert (repo_path / "main.tex").read_text() == before
                page.screenshot(path=str(qa / "review-with-pdf.png"), full_page=True)
                with page.expect_download() as download_info:
                    page.locator("#export-patch").click()
                download_info.value.save_as(str(qa / "accepted.patch"))
                exported = (qa / "accepted.patch").read_text()
                assert "higher" not in exported and "coupling term" in exported
                page.get_by_role("button", name="Apply accepted changes", exact=True).click()
                expect(page.get_by_role("button", name="Undo apply", exact=True)).to_be_visible()
                assert "We find that the reaction gets faster" in (repo_path / "main.tex").read_text()
                assert "coupling term" in (repo_path / "main.tex").read_text()
                assert run_git(repo_path, "diff", "--cached").stdout == b""
                page.get_by_role("button", name="Undo apply", exact=True).click()
                expect(page.locator("#apply-summary")).to_have_text("Apply undone")
                assert (repo_path / "main.tex").read_text() == before

                # Skip keeps a file queued; invalid context shows the Archive warning.
                import_patch("Later", original_patch)
                expect(page.locator("#proposal-title")).to_have_text("Later")
                page.locator("#skip").get_by_role("button", name="Skip", exact=True).click()
                expect(page.locator("#skipped-count")).to_have_text("1")
                assert (queue / "Later.patch").is_file()
                page.locator("#skipped-section summary").click()
                page.locator("#skipped-list").get_by_role("button").click()
                expect(page.locator("article.hunk")).to_have_count(3)
                page.locator("#skip").get_by_role("button").click()
                invalid = "--- a/main.tex\n+++ b/main.tex\n@@ -1 +1 @@\n-This source text never existed.\n+Replacement.\n"
                import_patch("Outdated", invalid)
                expect(page.locator("#warning-dialog dialog")).to_be_visible()
                expect(page.locator("#warning-text")).to_contain_text("apply")
                page.locator("#warning-dialog").get_by_role("button", name="Archive", exact=True).click()
                expect(page.locator("#warning-dialog dialog")).not_to_be_visible()
                assert not (queue / "Outdated.patch").exists()
                assert (queue / "superseded/Outdated.patch").read_text() == invalid

                # A file dropped in the directory is discovered, as is an external edit.
                external = queue / "External.patch"
                external.write_text(original_patch)
                expect(page.locator("#queue-list")).to_contain_text("External", timeout=10000)
                page.locator("#queue-list").get_by_role("button", name="External External.patch · pending").click()
                expect(page.locator("article.hunk")).to_have_count(3)
                external.write_text(original_patch.replace("an increase in the", "a substantial increase in the"))
                expect(page.locator("#changes")).to_contain_text("a substantial increase", timeout=10000)
                assert any(p.read_text() == original_patch for p in (queue / "superseded").glob("External*.patch"))
                page.get_by_role("button", name="Copy LLM context", exact=True).click()
                expect(page.locator("#context-files input:checked")).to_have_count(2)
                page.locator("#context-dialog").get_by_role("button", name="Close", exact=True).click()
                page.get_by_role("button", name="Git status and history", exact=True).click()
                expect(page.locator("#git-status")).to_contain_text("review_queue/")
                page.locator("#git-dialog").get_by_role("button", name="Close", exact=True).click()
                page.get_by_role("button", name="PDF preview", exact=True).click()
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(qa / "review-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile viewport overflows"
                assert not errors, errors
                browser.close()
            print("Browser workflow passed: directory selection, queue import, split diff, full context, edit/reject, backups, persistence, PDF, apply/undo, skip/archive, external discovery, mobile.")
            print(f"Screenshots: {qa}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
