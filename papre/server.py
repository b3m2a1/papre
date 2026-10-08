"""Paper Patch Review Editor's loopback HTTP API. Run with `papre --demo`."""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import threading
from urllib.parse import parse_qs, urlsplit

from .core import Repository, ReviewError, ReviewStore, run_git
from .preview import PreviewManager
from .queue import PatchQueue
from .picker import pick_directory
from .system import default_state_dir, print_tool_status

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
ASSETS = Path(__file__).resolve().parent / "assets"
ASSET_TYPES = {
    "papre-logo.svg": "image/svg+xml",
    "favicon.svg": "image/svg+xml",
    "favicon.ico": "image/vnd.microsoft.icon",
    "favicon-16.png": "image/png",
    "favicon-32.png": "image/png",
    "favicon-48.png": "image/png",
    "favicon-64.png": "image/png",
}
DEMO = Path(__file__).resolve().parent / "demo"

PROMPT = """Review this scientific LaTeX manuscript for the requested changes.
Preserve all numerical values, scientific meaning, equations, citations, labels, and LaTeX commands
unless I explicitly ask to change them. Do not invent evidence or references. Explain uncertainty.
Return a standard unified diff against the supplied CURRENT working-tree text, using
--- a/path and +++ b/path headers and @@ hunks. Modify existing source files only.
Alternatively return valid JSON: {\"title\":\"…\",\"rationale\":\"…\",\"edits\":[
{\"path\":\"main.tex\",\"search\":\"exact unique source text\",\"replace\":\"replacement text\"}]}.
Escape LaTeX backslashes correctly in JSON. Keep the patch separate from your explanation.
Requested changes: [describe the edit here]
"""


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store=None, main="main.tex", demo=False, browse_root=None, state_base=None, browse_start=None):
        self.stores = []
        self.preview_managers = []
        super().__init__(address, Handler)
        self.lock = threading.RLock()
        self.browse_root = Path(browse_root).expanduser().resolve() if browse_root else None
        self.browse_start = Path(browse_start or self.browse_root or (store.repo.root if store else Path.cwd())).expanduser().resolve()
        self.picker_lock = threading.Lock()
        self.state_base = Path(state_base or default_state_dir()).resolve()
        self.store = store
        self.queue = PatchQueue(store) if store else None
        self.previews = PreviewManager(store) if store else None
        if store:
            self.stores.append(store)
            self.preview_managers.append(self.previews)
        self.main = main
        self.demo = demo
        self.token = secrets.token_urlsafe(32)

    def browse(self, value=None):
        path = Path(value or self.browse_start).expanduser().resolve()
        if self.browse_root and not path.is_relative_to(self.browse_root):
            raise ReviewError("Choose a directory inside the configured browser root.")
        if not path.is_dir():
            raise ReviewError("This directory does not exist or is not accessible.")
        try:
            children = sorted((p for p in path.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")),
                              key=lambda p: p.name.casefold())
        except OSError as exc:
            raise ReviewError("Cannot read this directory.") from exc
        return {"path": str(path), "parent": str(path.parent) if path not in (self.browse_root, path.parent) else None,
                "is_repo": (path / ".git").exists(), "root": str(self.browse_root) if self.browse_root else None,
                "directories": [{"name": p.name, "path": str(p), "is_repo": (p / ".git").exists()} for p in children[:500]]}

    def choose_directory(self, value=None):
        if value is not None and not isinstance(value, str):
            raise ReviewError("Invalid directory path.")
        start = Path(self.browse(value)["path"])
        if not self.picker_lock.acquire(blocking=False):
            raise ReviewError("A folder picker is already open. Finish that selection first.", 409)
        try:
            selected = pick_directory(start)
            return {"cancelled": selected is None, "directory": self.browse(str(selected)) if selected else None}
        finally:
            self.picker_lock.release()

    def attach(self, path, allow_write=False, main="main.tex"):
        if not isinstance(path, str) or not isinstance(allow_write, bool) or not isinstance(main, str):
            raise ReviewError("Invalid repository selection.")
        chosen = Path(path).expanduser().resolve()
        self.browse(str(chosen))
        repo = Repository(chosen)
        key = hashlib.sha256(str(repo.root).encode()).hexdigest()[:16]
        store = ReviewStore(repo, self.state_base / "repositories" / key, allow_write)
        try:
            store.recover()
            queue = PatchQueue(store)
            previews = PreviewManager(store)
        except Exception:
            store.close()
            raise
        with self.lock:
            if self.previews:
                self.previews.close()
            self.store, self.queue, self.previews = store, queue, previews
            self.main, self.demo = main, False
            self.token = secrets.token_urlsafe(32)
            self.stores.append(store)
            self.preview_managers.append(previews)
        return self.session()

    def session(self):
        with self.lock:
            store = self.store
            files = store.repo.files() if store else []
            main = self.main if self.main in files else next((f for f in files if f.endswith(".tex")), "")
            return {"token": self.token, "repo": store.repo.status() if store else None,
                    "repo_key": hashlib.sha256(str(store.repo.root).encode()).hexdigest() if store else None,
                    "files": files, "main": main, "allow_write": store.allow_write if store else False,
                    "demo": self.demo, "browse_root": str(self.browse_root) if self.browse_root else None,
                    "browse_start": str(self.browse_start), "compiler": PreviewManager.available(),
                    "queue": self.queue.list() if self.queue else {"entries": [], "archives": []},
                    "proposals": store.list() if store else []}

    def server_close(self):
        super().server_close()
        for manager in self.preview_managers:
            manager.close()
        for store in self.stores:
            store.close()


class Handler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def log_message(self, fmt, *args):
        # URLs contain no keys, but avoid logging manuscript contents or request bodies.
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _validate_request(self, mutate=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise ReviewError("This server only accepts local requests.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ReviewError("Cross-site requests are not allowed.", 403)
        if mutate:
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in hosts}:
                raise ReviewError("Invalid request origin.", 403)
            if not secrets.compare_digest(self.headers.get("X-Review-Token", ""), self.server.token):
                raise ReviewError("Reload this page before submitting changes.", 403)
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ReviewError("Expected a JSON request.", 415)
            key = self.headers.get("X-Repository-Key")
            if key and self.server.store and key != hashlib.sha256(str(self.server.store.repo.root).encode()).hexdigest():
                raise ReviewError("The attached repository changed. Reload before continuing.", 409)

    def send(self, payload, status=200, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; frame-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ReviewError("Invalid request length.") from exc
        if not 0 < length <= 20 * 1024 * 1024:
            raise ReviewError("Request must be between 1 byte and 20 MB.", 413)
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewError("Invalid JSON request.") from exc
        if not isinstance(data, dict):
            raise ReviewError("Expected a JSON object.")
        return data

    def do_GET(self):
        try:
            self._validate_request()
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            store = self.server.store
            if path == "/api/session":
                return self.send(self.server.session())
            if path == "/api/directories":
                return self.send(self.server.browse(query.get("path", [None])[0]))
            if path == "/api/queue":
                return self.send(self.server.queue.list() if store else {"entries": [], "archives": []})
            if path == "/api/queue/text":
                if not store:
                    raise ReviewError("Choose a repository first.", 409)
                return self.send({"text": self.server.queue.text(query.get("path", [""])[0])})
            if path == "/api/file":
                if not store:
                    raise ReviewError("Choose a repository first.", 409)
                name = query.get("path", [""])[0]
                return self.send({"path": name, "text": store.repo.read(name)})
            if path == "/api/proposals":
                return self.send(store.list() if store else [])
            if path == "/api/events":
                return self.send(store.events() if store else [])
            if path.startswith("/api/proposals/"):
                parts = path.split("/")
                proposal = store.get(parts[3])
                if len(parts) == 4:
                    return self.send(proposal | {"counts": store.counts(proposal)})
                if len(parts) == 5 and parts[4] == "patch":
                    selection = query.get("selection", ["accepted"])[0]
                    patch = store.patch(proposal, selection)
                    return self.send(patch.encode(), content_type="text/plain; charset=utf-8",
                                     extra={"Content-Disposition": 'attachment; filename="reviewed.patch"'})
                if len(parts) == 5 and parts[4] == "recovery":
                    return self.send(json.dumps(proposal, indent=2).encode(),
                                     extra={"Content-Disposition": 'attachment; filename="review-recovery.json"'})
            if path.startswith("/api/previews/") and self.server.previews:
                parts = path.split("/")
                if len(parts) == 4:
                    return self.send(self.server.previews.get(parts[3]))
                if len(parts) == 5 and parts[4] == "pdf":
                    return self.send(self.server.previews.pdf(parts[3]).read_bytes(), content_type="application/pdf")
                if len(parts) == 6 and parts[4] == "pages":
                    return self.send(self.server.previews.page(parts[3], parts[5]).read_bytes(), content_type="image/png")
            if path == "/api/demo-patch" and self.server.demo:
                return self.send({"patch": (DEMO / "example.patch").read_text()})
            static_files = {"/": ("index.html", "text/html; charset=utf-8"),
                            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                            "/components.js": ("components.js", "text/javascript; charset=utf-8"),
                            "/layout.js": ("layout.js", "text/javascript; charset=utf-8"),
                            "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in static_files:
                filename, content_type = static_files[path]
                return self.send((STATIC / filename).read_bytes(), content_type=content_type)
            if path == "/favicon.ico":
                return self.send((ASSETS / "favicon.ico").read_bytes(), content_type=ASSET_TYPES["favicon.ico"])
            if path.startswith("/assets/"):
                filename = path.removeprefix("/assets/")
                if filename in ASSET_TYPES:
                    return self.send((ASSETS / filename).read_bytes(), content_type=ASSET_TYPES[filename])
            raise ReviewError("Not found.", 404)
        except ReviewError as exc:
            self.send({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send({"error": f"Server error: {type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        try:
            self._validate_request(mutate=True)
            data = self.body()
            path = urlsplit(self.path).path
            store = self.server.store
            if path == "/api/directory-picker":
                return self.send(self.server.choose_directory(data.get("path")))
            if path == "/api/repository":
                return self.send(self.server.attach(data.get("path"), data.get("allow_write", False), data.get("main", "main.tex")))
            if not store:
                raise ReviewError("Choose a repository first.", 409)
            if path == "/api/import":
                title, rationale = data.get("title", ""), data.get("rationale", "")
                if not isinstance(title, str) or not isinstance(rationale, str):
                    raise ReviewError("Title and rationale must be text.")
                if "edits" in data:
                    text = json.dumps(data, indent=2, ensure_ascii=False)
                else:
                    text = data.get("patch", "")
                return self.send(self.server.queue.enqueue(text, title, rationale, data.get("filename")), 201)
            if path.startswith("/api/queue/"):
                parts = path.split("/")
                if len(parts) != 5:
                    raise ReviewError("Not found.", 404)
                entry_id, action = parts[3:]
                queue = self.server.queue
                if action == "open":
                    return self.send(queue.open(entry_id))
                if action == "review":
                    return self.send(queue.review(entry_id, data.get("revision"), data.get("decisions")))
                if action == "skip":
                    return self.send(queue.skip(entry_id))
                if action == "archive":
                    return self.send(queue.archive(entry_id))
                if action in {"apply", "undo"}:
                    return self.send(queue.apply(entry_id, data.get("revision"), undo=action == "undo"))
                raise ReviewError("Not found.", 404)
            if path == "/api/context":
                names = data.get("files", [])
                if not isinstance(names, list) or not names or len(names) > 100:
                    raise ReviewError("Select at least one source file to copy.")
                text = PROMPT
                for name in dict.fromkeys(names):
                    text += f"\n--- CURRENT FILE: {name} ---\n" + store.repo.read(name) + "\n"
                if len(text.encode()) > 4 * 1024 * 1024:
                    raise ReviewError("Selected manuscript context is larger than 4 MB.")
                return self.send({"text": text})
            if path == "/api/previews/cancel":
                self.server.previews.cancel()
                return self.send({"cancelled": True})
            if path == "/api/previews":
                return self.send(self.server.previews.start(data.get("main", ""), data.get("engine", "pdflatex"),
                                 data.get("selection", "working"), data.get("proposal"), data.get("revision"), data.get("strict", False)), 202)
            if path.startswith("/api/previews/") and path.endswith("/source"):
                parts = path.split("/")
                if len(parts) != 5:
                    raise ReviewError("Not found.", 404)
                return self.send(self.server.previews.source_at(parts[3], data.get("page"), data.get("x"), data.get("y")))
            if path.startswith("/api/proposals/"):
                parts = path.split("/")
                if len(parts) != 5:
                    raise ReviewError("Not found.", 404)
                proposal_id, action = parts[3:]
                queued = next((row for row in self.server.queue._rows() if row["proposal"] == proposal_id), None)
                if queued:
                    if action == "review":
                        result = self.server.queue.review(queued["id"], data.get("revision"), data.get("decisions"))
                    elif action in {"apply", "undo"}:
                        result = self.server.queue.apply(queued["id"], data.get("revision"), undo=action == "undo")
                    else:
                        raise ReviewError("Not found.", 404)
                    return self.send(result["proposal"])
                if action == "review":
                    proposal = store.update(proposal_id, data.get("revision"), data.get("decisions"))
                elif action == "apply":
                    proposal = store.apply(proposal_id, data.get("revision"))
                elif action == "undo":
                    proposal = store.undo(proposal_id, data.get("revision"))
                else:
                    raise ReviewError("Not found.", 404)
                return self.send(proposal | {"counts": store.counts(proposal)})
            raise ReviewError("Not found.", 404)
        except ReviewError as exc:
            self.send({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send({"error": f"Server error: {type(exc).__name__}: {exc}"}, 500)


def make_demo(state_base: Path) -> Path:
    repo = state_base / "demo-repo"
    if repo.exists():
        return repo
    repo.mkdir(parents=True)
    for name in ("main.tex", "references.bib", ".gitignore"):
        shutil.copyfile(DEMO / name, repo / name)
    for args in (("init",), ("symbolic-ref", "HEAD", "refs/heads/main"), ("add", "."),
                 ("-c", "user.name=Demo Author", "-c", "user.email=demo@example.invalid",
                  "-c", "core.hooksPath=/dev/null", "commit", "-m", "Initial demo manuscript")):
        result = run_git(repo, *args)
        if result.returncode:
            raise ReviewError("Cannot initialize demo repository: " + result.stderr.decode(errors="replace"))
    return repo


def main():
    parser = argparse.ArgumentParser(prog="papre", description="Paper Patch Review Editor: review LaTeX patches against a local Git repository.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", type=Path, help="Root of an existing manuscript Git repository")
    group.add_argument("--demo", action="store_true", help="Create a demo manuscript in the review state directory")
    parser.add_argument("--main", default="main.tex", help="Repository-relative main document")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-write", action="store_true", help="Enable explicit Apply and Undo actions")
    parser.add_argument("--state-dir", type=Path, help="Parent of review state folders, outside the manuscript repository")
    parser.add_argument("--browse-root", type=Path, help="Optional boundary for repository selection; by default any local directory is accessible")
    parser.add_argument("--check", action="store_true", help="Report available Git, LaTeX engines, and PDF tools without starting a server")
    args = parser.parse_args()
    if args.check:
        print_tool_status()
        return
    try:
        if not shutil.which("git"):
            raise ReviewError("Git is required. Install Git, add it to PATH, and restart papre.")
        state_base = (args.state_dir or default_state_dir()).expanduser().resolve()
        store = None
        if args.repo or args.demo:
            repo = Repository(make_demo(state_base) if args.demo else args.repo.expanduser())
            key = hashlib.sha256(str(repo.root).encode()).hexdigest()[:16]
            state = state_base / "repositories" / key
            store = ReviewStore(repo, state, args.allow_write or args.demo)
            store.recover()
        server = ReviewHTTPServer(("127.0.0.1", args.port), store, args.main, args.demo,
                                  args.browse_root, state_base)
        if args.demo and not server.queue.list()["entries"]:
            server.queue.enqueue((DEMO / "example.patch").read_text(), "Example manuscript edits")
    except (ReviewError, OSError) as exc:
        parser.exit(1, f"Could not start: {exc}\n")
    print(f"Paper Patch Review Editor: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Repository: {store.repo.root if store else 'Choose in browser'}\nBrowser boundary: {server.browse_root or 'unrestricted'}", flush=True)
    compiler = PreviewManager.available()
    print("LaTeX engines: " + (", ".join(compiler["engines"]) or "none detected") +
          "; latexmk: " + ("available" if compiler["latexmk"] else "not found"), flush=True)
    if not compiler["can_compile"]:
        print(compiler["installation"]["text"] + " " + compiler["installation"]["url"], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
