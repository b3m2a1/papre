"""Real-browser verification of the review queue using disposable draft files."""
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from playwright.sync_api import sync_playwright, expect
from papre.core import ReviewError, run_git
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
        (repo_path / "error-preview.tex").write_text("\\documentclass{article}\n\\begin{document}\nA recoverable error: \\PapreUnknownCommand.\nThe preview still contains this text.\n\\end{document}\n")
        (repo_path / "missing-package.tex").write_text("\\documentclass{article}\n\\usepackage{papre-missing-package}\n\\begin{document}\nNo preview.\n\\end{document}\n")
        for args in [("init",), ("symbolic-ref", "HEAD", "refs/heads/main"), ("add", "."),
                     ("-c", "user.name=QA", "-c", "user.email=qa@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-m", "demo")]:
            result = run_git(repo_path, *args)
            assert result.returncode == 0, result.stderr.decode()
        before = (repo_path / "main.tex").read_text()
        original_patch = (DEMO / "example.patch").read_text()
        queue = repo_path / "review_queue"
        launch = base / "app-launch-directory"
        launch.mkdir()
        server = ReviewHTTPServer(("127.0.0.1", 0), browse_start=launch, state_base=base / "state")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                # Full Chromium includes its PDF viewer; the headless shell does not.
                browser = playwright.chromium.launch(headless=True, channel="chromium")
                context = browser.new_context(viewport={"width": 1600, "height": 1100})
                page = context.new_page()
                errors = []
                preview_jobs = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def record_preview(response):
                    if response.url.endswith("/api/previews") and response.request.method == "POST" and response.status == 202:
                        preview_jobs.append(response.json())
                page.on("response", record_preview)
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page).to_have_title("Paper Patch Review Editor")
                logo = page.get_by_role("img", name="Paper Patch Review Editor", exact=True)
                expect(logo).to_be_visible()
                logo.evaluate("image => image.decode()")
                assert logo.evaluate("image => image.naturalWidth") == 256
                assert logo.bounding_box()["width"] == 28
                for filename, mime in [("favicon.svg", "image/svg+xml"), ("favicon.ico", "image/vnd.microsoft.icon")]:
                    icon = page.locator(f'link[rel="icon"][href="/assets/{filename}"]')
                    assert icon.count() == 1
                    response = context.request.get(f"http://127.0.0.1:{server.server_port}/assets/{filename}")
                    assert response.ok and response.headers["content-type"] == mime
                expect(page.locator("#repo-dialog dialog")).to_be_visible()
                page.screenshot(path=str(qa / "directory-picker.png"))
                with patch("papre.server.pick_directory", return_value=None):
                    page.get_by_role("button", name="Browse…", exact=True).click()
                    expect(page.get_by_role("button", name="Browse…", exact=True)).to_be_enabled()
                assert not queue.exists()
                with patch("papre.server.pick_directory", side_effect=ReviewError("System picker unavailable", 503)):
                    page.get_by_role("button", name="Browse…", exact=True).click()
                    expect(page.locator("#repo-error")).to_contain_text("System picker unavailable")
                # Leaving the launch folder must be possible without a browser-root option.
                expect(page.get_by_role("button", name="Up", exact=True)).to_be_enabled()
                page.get_by_role("button", name="Up", exact=True).click()
                page.locator("#directory-list").get_by_role("button", name="manuscript Git repository").click()
                expect(page.get_by_role("button", name="Attach repository", exact=True)).to_be_enabled()
                page.locator("#directory-path").fill(str(base / "missing"))
                expect(page.get_by_role("button", name="Attach repository", exact=True)).to_be_disabled()
                page.get_by_role("button", name="Go", exact=True).click()
                expect(page.locator("#repo-error")).to_contain_text("does not exist")
                page.locator("#directory-path").fill(str(launch))
                page.get_by_role("button", name="Go", exact=True).click()
                expect(page.locator("#repo-error")).not_to_be_visible()
                with patch("papre.server.pick_directory", return_value=repo_path):
                    page.get_by_role("button", name="Browse…", exact=True).click()
                    expect(page.locator("#directory-path")).to_have_value(str(repo_path))
                assert not queue.exists()
                page.screenshot(path=str(qa / "directory-picker-fallback.png"))
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

                def open_settings():
                    if not page.locator("#preview-settings").is_visible():
                        page.get_by_role("button", name="Compilation settings", exact=True).click()
                    expect(page.locator("#preview-settings")).to_be_visible()
                    expect(page.locator("#toggle-preview-settings button")).to_have_attribute("aria-expanded", "true")

                def show_preview():
                    if not page.locator("#preview-pane").is_visible():
                        page.get_by_role("button", name="PDF preview", exact=True).click()
                    expect(page.locator("#preview-pane")).to_be_visible()

                def compile_preview():
                    open_settings()
                    page.get_by_role("button", name="Compile preview", exact=True).click()
                    expect(page.locator("#preview-settings")).not_to_be_visible()

                def check_pdf_bounds():
                    frame = page.locator("#pdf-frame").bounding_box()
                    content = page.locator(".preview-content").bounding_box()
                    for key in ["x", "y", "width", "height"]:
                        assert abs(frame[key] - content[key]) < 1, (frame, content)
                    assert page.locator("#pdf-frame").evaluate("element => getComputedStyle(element).padding") == "0px"
                    pane = page.locator("#preview-pane").bounding_box()
                    for selector in ["#main-file", "#open-pdf", "#expand-preview", "#toggle-preview-settings"]:
                        box = page.locator(selector).bounding_box()
                        assert pane["x"] <= box["x"] and box["x"] + box["width"] <= pane["x"] + pane["width"] + 1, (selector, box, pane)
                    return frame

                import_patch("Example manuscript edits", original_patch)
                expect(page.locator("article.hunk")).to_have_count(3)
                expect(page.locator("#build-badge")).to_contain_text("Ready", timeout=45000)
                expect(page.locator("#preview-pane")).to_be_visible()
                expect(page.locator("#pdf-frame")).to_be_visible()
                expect(page.locator("#preview-settings")).not_to_be_visible()
                assert page.locator("#pdf-frame").get_attribute("src").split('/')[-2] == server.previews.latest
                expect(page.locator("#expand-preview button")).to_have_attribute("title", "Expand PDF")
                expect(page.locator("#expand-preview svg")).to_be_visible()
                assert page.locator("#expand-preview").bounding_box()["width"] < 45
                assert preview_jobs, "Opening the patch should start a background build"
                assert preview_jobs[-1]["selection"] == "proposed"
                assert (queue / "Example-manuscript-edits.patch").read_text() == original_patch
                assert (repo_path / "main.tex").read_text() == before
                expect(page.locator('.hunk .change-location').first).to_contain_text('PDF p. 1')
                job = server.previews.get(server.previews.latest)
                assert all(change['highlighted'] for change in job['changes'])
                assert [change['page'] for change in job['changes']] == [1, 1, 2]
                page.locator('.hunk').first.get_by_role('button', name='Scroll to Section', exact=True).click()
                expect(page.locator('#pdf-frame')).to_have_attribute('src', job['pdf'] + '#page=1')
                expect(page.locator('#pdf-location')).to_contain_text('source ¶')
                # The native viewer remains default; the image alternative supports reverse navigation.
                open_settings()
                page.locator('#pdf-viewer').select_option('pages')
                page.get_by_role('button', name='Compilation settings', exact=True).click()
                change = job['changes'][-1]
                page.locator('.hunk').last.get_by_role('button', name='Scroll to Section', exact=True).click()
                image = page.locator(f'#pdf-pages img[data-page="{change["page"]}"]')
                image.evaluate('image => image.decode()')
                factor = image.bounding_box()['width'] / image.evaluate('image => image.naturalWidth') * 120 / 72
                image.dblclick(position={'x': (change['x'] + 2) * factor, 'y': (change['y'] - 2) * factor})
                expect(page.locator(f'article[data-hunk="{change["id"]}"]')).to_have_class(re.compile('pdf-linked'))
                assert page.evaluate('location.hash') == '#change-' + change['id']
                page.screenshot(path=str(qa / 'green-proposed-edits.png'), full_page=True)
                open_settings()
                page.locator('#pdf-viewer').select_option('browser')
                page.get_by_role('button', name='Compilation settings', exact=True).click()
                page.evaluate("history.replaceState(null, '', '/')")
                page.screenshot(path=str(qa / "review-desktop.png"), full_page=True)

                # Shared dividers support dragging, keyboard resizing, and bounded widths.
                def width(selector):
                    return page.locator(selector).first.bounding_box()["width"]

                def drag(selector, delta):
                    box = page.locator(selector).bounding_box()
                    x, y = box["x"] + box["width"] / 2, box["y"] + 160
                    page.mouse.move(x, y)
                    page.mouse.down()
                    page.mouse.move(x + delta, y, steps=8)
                    page.mouse.up()
                    assert "resizing-panels" not in page.locator("body").get_attribute("class")

                original_review_width = width(".review-pane")
                page.get_by_role("button", name="Hide sidebar", exact=True).click()
                expect(page.locator("#sidebar")).not_to_be_visible()
                expect(page.locator("#sidebar-splitter")).not_to_be_visible()
                assert width(".review-pane") > original_review_width + 200
                page.get_by_role("button", name="Show sidebar", exact=True).click()
                drag("#sidebar-splitter", 65)
                assert abs(width("#sidebar") - 300) < 2
                page.locator("#sidebar-splitter").press("ArrowRight")
                assert abs(width("#sidebar") - 310) < 2
                show_preview()
                expect(page.locator("#preview-settings")).not_to_be_visible()
                expect(page.locator("#toggle-preview-settings button")).to_have_attribute("aria-expanded", "false")
                drag("#preview-splitter", -220)
                assert abs(width("#preview-pane") - 600) < 2
                page.locator("#preview-splitter").press("ArrowRight")
                assert abs(width("#preview-pane") - 590) < 2
                page.locator("#preview-splitter").press("End")
                assert width(".review-pane") >= 359
                page.set_viewport_size({"width": 1024, "height": 1100})
                assert width(".review-pane") >= 359
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.set_viewport_size({"width": 1600, "height": 1100})
                page.locator("#preview-splitter").press("Home")
                drag("#preview-splitter", -400)
                assert abs(width("#preview-pane") - 700) < 2
                page.get_by_role("button", name="Hide sidebar", exact=True).click()
                page.get_by_role("button", name="PDF preview", exact=True).click()
                expect(page.locator("#preview-splitter")).not_to_be_visible()

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
                expect(page.locator("#build-badge")).to_contain_text("Ready", timeout=45000)
                expect(page.locator("#preview-pane")).not_to_be_visible()
                assert page.locator("#pdf-frame").get_attribute("src").split('/')[-2] == server.previews.latest
                latest = server.previews.get(server.previews.latest)
                assert latest["revision"] == server.store.get(latest["proposal"])["revision"]
                assert "higher" not in (server.previews.root / latest["id"] / "source/main.tex").read_text()
                assert (server.previews.root / latest["id"] / "source/main.tex").read_text() != before
                assert any(job["status"] == "cancelled" for job in server.previews.jobs.values())

                # Persistence and compilation from an isolated accepted-changes snapshot.
                page.reload()
                expect(page.locator("article.hunk.accepted")).to_have_count(2)
                expect(page.locator("article.hunk.rejected")).to_have_count(1)
                expect(page.locator("#sidebar")).not_to_be_visible()
                page.get_by_role("button", name="Show sidebar", exact=True).click()
                assert abs(width("#sidebar") - 310) < 2
                assert (repo_path / "main.tex").read_text() == before
                show_preview()
                assert abs(width("#preview-pane") - 700) < 2
                expect(page.locator("#compiler-status")).to_contain_text("pdflatex")
                expect(page.locator("#latex-install")).to_have_attribute("href", server.session()["compiler"]["installation"]["url"])
                open_settings()
                page.locator("#preview-selection").select_option("accepted")
                expect(page.locator("#build-badge")).to_contain_text("Ready", timeout=45000)
                assert page.locator("#pdf-frame").get_attribute("src").split('/')[-2] == server.previews.latest
                warmed_id = server.previews.latest
                compile_preview()
                expect(page.locator("#build-badge")).to_contain_text("Ready", timeout=45000)
                assert server.previews.latest == warmed_id, "Compile preview must reuse the prepared PDF"
                expect(page.locator("#pdf-frame")).to_be_visible()
                expect(page.locator("#pdf-frame")).to_have_attribute("src", page.locator("#open-pdf").get_attribute("href"))
                expect(page.locator("#pdf-pages")).not_to_be_visible()
                expect(page.locator(".preview-toolbar #main-file")).to_have_value("main.tex")
                expect(page.locator(".preview-toolbar #pdf-viewer")).to_have_count(0)
                expect(page.locator("#preview-settings #pdf-viewer")).to_have_value("browser")
                expect(page.locator(".preview-toolbar #open-pdf")).to_be_visible()
                check_pdf_bounds()
                if not any(frame.url.startswith("chrome-extension://") for frame in page.frames):
                    page.wait_for_event("framenavigated", predicate=lambda frame: frame.url.startswith("chrome-extension://"), timeout=15000)
                pdf_viewer = page.frame(url=re.compile(r"^chrome-extension://"))
                expect(pdf_viewer.get_by_role("textbox", name="Page number", exact=True)).to_have_value("1", timeout=15000)
                expect(pdf_viewer.get_by_role("button", name="Zoom in", exact=True)).to_be_visible()
                page.screenshot(path=str(qa / "browser-pdf.png"))
                page.get_by_role("button", name="Expand PDF", exact=True).click()
                expect(page.locator("#expand-preview button")).to_have_attribute("title", "Restore split view")
                expect(page.locator("#expand-preview svg")).to_be_visible()
                assert width("#expand-preview") < 45
                pdf_viewer.wait_for_function("() => window.innerWidth > 1400", timeout=15000)
                frame_bounds = check_pdf_bounds()
                assert frame_bounds["height"] > page.locator("#preview-pane").bounding_box()["height"] * .9
                page.screenshot(path=str(qa / "browser-pdf-expanded.png"))
                open_settings()
                assert check_pdf_bounds() == frame_bounds, "Settings must overlay, without shrinking the PDF"
                expect(page.locator("#preview-settings #toggle-log")).to_be_visible()
                page.get_by_role("button", name="Show log", exact=True).click()
                expect(page.locator("#preview-settings #build-log")).to_be_visible()
                page.screenshot(path=str(qa / "pdf-settings-overlay.png"))
                page.keyboard.press("Escape")
                expect(page.locator("#preview-settings")).not_to_be_visible()
                expect(page.locator("#toggle-preview-settings button")).to_be_focused()
                expect(page.locator(".review-pane")).not_to_be_visible()
                open_settings()
                # Wait for the native PDF's composited hit regions to update, too.
                page.locator(".drawer-backdrop button").click(position={"x": 20, "y": 30})
                expect(page.locator("#preview-settings")).not_to_be_visible()
                open_settings()
                page.locator("#preview-settings").get_by_role("button", name="Close", exact=True).click()
                expect(page.locator("#toggle-preview-settings button")).to_be_focused()
                page.get_by_role("button", name="Compilation settings", exact=True).press("Space")
                expect(page.locator("#preview-settings")).to_be_visible()
                page.get_by_role("button", name="Compilation settings", exact=True).click()
                expect(page.locator("#preview-settings")).not_to_be_visible()
                page.get_by_role("button", name="Restore split view", exact=True).click()
                expect(page.locator("#expand-preview button")).to_have_attribute("title", "Expand PDF")
                viewer_build = server.previews.latest
                open_settings()
                page.locator("#pdf-viewer").select_option("pages")
                page.get_by_role("button", name="Compilation settings", exact=True).click()
                assert server.previews.latest == viewer_build, "Switching viewers must not recompile"
                expect(page.locator("#pdf-pages img").first).to_be_visible()
                page.wait_for_function("document.querySelector('#pdf-pages img')?.naturalWidth > 0")
                response = context.request.get(f"http://127.0.0.1:{server.server_port}" + page.locator("#open-pdf").get_attribute("href"))
                assert response.body().startswith(b"%PDF")
                assert (repo_path / "main.tex").read_text() == before
                page.screenshot(path=str(qa / "review-with-pdf.png"), full_page=True)

                # Expanded viewing keeps review decisions, zoom, and the normal layout intact.
                normal_pdf_width = width("#pdf-pages img")
                page.get_by_role("button", name="Expand PDF", exact=True).click()
                expect(page.locator("#sidebar")).not_to_be_visible()
                expect(page.locator(".review-pane")).not_to_be_visible()
                expect(page.locator("#preview-splitter")).not_to_be_visible()
                assert abs(width("#preview-pane") - 1600) < 2
                assert width("#pdf-pages img") > normal_pdf_width * 2
                assert not page.locator("#preview-settings").evaluate("element => element.open")
                page.screenshot(path=str(qa / "expanded-pdf.png"))
                open_settings()
                page.locator("#pdf-zoom").select_option("200")
                expect(page.get_by_role("button", name="Compile preview", exact=True)).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator("#preview-settings")).not_to_be_visible()
                expect(page.locator(".review-pane")).not_to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator("#sidebar")).to_be_visible()
                expect(page.locator(".review-pane")).to_be_visible()
                assert abs(width("#preview-pane") - 700) < 2
                expect(page.locator("#pdf-zoom")).to_have_value("200")
                open_settings()
                page.locator("#pdf-zoom").select_option("fit")
                page.get_by_role("button", name="Expand PDF", exact=True).click()
                page.get_by_role("button", name="Restore split view", exact=True).click()
                expect(page.locator("article.hunk.accepted")).to_have_count(2)
                expect(page.locator("article.hunk.rejected")).to_have_count(1)
                page.get_by_role("button", name="Hide sidebar", exact=True).click()
                page.get_by_role("button", name="Expand PDF", exact=True).click()
                page.get_by_role("button", name="PDF preview", exact=True).click()
                expect(page.locator("#preview-pane")).not_to_be_visible()
                expect(page.locator(".review-pane")).to_be_visible()
                expect(page.locator("#sidebar")).not_to_be_visible()
                page.get_by_role("button", name="Show sidebar", exact=True).click()
                page.get_by_role("button", name="PDF preview", exact=True).click()
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

                # Recoverable errors display a fresh PDF, while missing packages cannot.
                open_settings()
                page.locator("#preview-selection").select_option("working")
                page.locator("#main-file").select_option("error-preview.tex")
                expect(page.locator("#compile-strict")).not_to_be_checked()
                compile_preview()
                expect(page.locator("#build-badge")).to_have_text("Preview with errors", timeout=45000)
                expect(page.locator("#build-warning")).to_contain_text("preview may be incomplete")
                expect(page.locator("#preview-settings")).not_to_be_visible()
                expect(page.locator("#pdf-pages img").first).to_be_visible()
                response = context.request.get(f"http://127.0.0.1:{server.server_port}" + page.locator("#open-pdf").get_attribute("href"))
                assert response.body().startswith(b"%PDF")
                page.screenshot(path=str(qa / "preview-with-errors.png"), full_page=True)
                open_settings()
                expect(page.locator("#build-log")).to_be_visible()
                page.locator("#compile-strict").check()
                compile_preview()
                expect(page.locator("#build-badge")).to_have_text("Build failed", timeout=45000)
                expect(page.locator("#open-pdf")).not_to_be_visible()
                open_settings()
                page.locator("#compile-strict").uncheck()
                page.locator("#main-file").select_option("missing-package.tex")
                compile_preview()
                expect(page.locator("#build-badge")).to_have_text("Build failed", timeout=45000)
                expect(page.locator("#build-warning")).to_contain_text("papre-missing-package.sty")
                expect(page.locator("#open-pdf")).not_to_be_visible()
                page.locator("#main-file").select_option("main.tex")
                compile_preview()
                expect(page.locator("#build-badge")).to_contain_text("Ready", timeout=45000)
                expect(page.locator("#build-warning")).not_to_be_visible()

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
                page.set_viewport_size({"width": 390, "height": 844})
                open_settings()
                page.locator("#pdf-viewer").select_option("browser")
                page.get_by_role("button", name="Expand PDF", exact=True).click()
                expect(page.locator("#preview-settings")).not_to_be_visible()
                check_pdf_bounds()
                page.screenshot(path=str(qa / "pdf-toolbar-mobile.png"))
                open_settings()
                assert width(".ui-drawer-panel") <= 390
                page.wait_for_function("() => document.querySelector('.ui-drawer-panel').getBoundingClientRect().right <= window.innerWidth + 1")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(qa / "pdf-settings-mobile.png"))
                page.keyboard.press("Escape")
                page.get_by_role("button", name="PDF preview", exact=True).click()
                page.screenshot(path=str(qa / "review-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile viewport overflows"
                assert not errors, errors
                browser.close()
            print("Browser workflow passed: directory selection, queue import, split diff, full context, edit/reject, backups, persistence, PDF, panel resizing, sidebar toggle, expanded PDF, compact toolbar, settings overlay and dismissal, edge-to-edge iframe, apply/undo, skip/archive, external discovery, mobile.")
            print(f"Screenshots: {qa}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
