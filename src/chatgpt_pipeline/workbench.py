"""Private loopback inbox with explicit, optional Markdown library import."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, quote, urlsplit

from .notes import Workspace, locked
from . import bulk
from .index import now
from .verify import encoded, loads, require, sha

ASSETS = Path(__file__).parent / "web"


class Application:
    def __init__(self, workspace, key_file=None, enabled=False, vault=None):
        self.workspace = Workspace(workspace)
        self.key_file, self.enabled = key_file, enabled
        self.vault = Path(vault).expanduser().absolute() if vault is not None else None
        self.library = None
        self.receipts = None
        if self.vault is not None:
            try:
                from markdown_library import Library, LibraryError
            except ImportError as error:
                raise ValueError("--vault requires markdown-library; install the sibling obsidian-workflow package first") from error
            try:
                self.library = Library(self.vault).validate()
            except (LibraryError, OSError) as error:
                raise ValueError(f"Cannot open initialized Markdown Vault: {error}") from error
            self.receipts = self.workspace.directory / "vault-imports" / sha(str(self.vault).encode())
        self.token = secrets.token_urlsafe(32)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notes")

    def receipt(self, item_id):
        self.workspace.item(item_id)
        receipt = self.receipts / f"{item_id}.json" if self.receipts else None
        return bulk.read(receipt) if receipt and receipt.exists() else None

    def list(self, view, query, page):
        from .note_recovery import CompleteRecovery
        revision = CompleteRecovery.revision
        result = self.workspace.list(view, query, page)
        for item in result["items"]:
            item["imported"] = self.receipt(item["id"])
            if item["status"] in ("done", "review"):
                item["review_status"] = self.workspace.review_status(item["id"])
            item["recovery_version"] = revision
            uses_content = bool(item["status"] == "attention" and item["job_id"] and
                                self.workspace.uses_content(item["job_id"]))
            if uses_content:
                item["recovery_unavailable_reason"] = "这条材料仍需检查来源或调用结果，现有修复入口不适用；原记录已保留。"
            item["recovery_available"] = bool(item["status"] == "attention" and item["job_id"] and
                not uses_content and
                not (self.workspace.directory / "jobs" / item["job_id"] / f"{revision}.json").exists())
        # Receipts describe a successful handoff, not whether a file is still at that path.
        result["imported_count"] = len(list(self.receipts.glob("*.json"))) if self.receipts else 0
        return result

    def import_note(self, item_id, *, review_confirmed=False):
        require(self.library is not None, "未配置 Markdown Vault；仍可下载初稿，手动放入你的笔记库。")
        from markdown_library import LibraryError
        # Serialize import and receipt publication. A crash before receipt is repaired by
        # library source-id idempotency; this action never requests or runs a model job.
        with locked(self.workspace.directory / "vault-import.lock"):
            item = self.workspace.item(item_id)
            title, content = self.workspace.note(item_id)
            review_status = self.workspace.review_status(item_id)
            require(review_status is not None and
                    (review_status["status"] == "passed" or review_confirmed),
                    "初稿仍待复核；请阅读提示并明确确认后入库。")
            try:
                result = self.library.import_note(content.decode("utf-8"),
                    source_id="chatgpt-workbench:" + item_id, title=title, kind="note",
                    source={"type": "chatgpt-workbench", "item_id": item_id, "job_id": item["job_id"],
                            "review_status": review_status})
            except (LibraryError, OSError) as error:
                raise ValueError(f"导入未完成，初稿仍保留，可重试；不会调用模型：{error}") from error
            receipt = self.receipt(item_id)
            if receipt is None:
                receipt = {**{key: result[key] for key in ("outcome", "id", "path", "sha256")},
                           "vault": str(self.vault), "imported_at": now()}
                self.receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
                bulk.save(self.receipts / f"{item_id}.json", receipt)
            return {**receipt, "outcome": result["outcome"]}

    def submit(self, job_id):
        require(self.enabled, "Model calls are disabled; restart with --call")
        self.executor.submit(self.workspace.run, job_id, self.key_file)

    def recover(self, item_id):
        from .note_recovery import CompleteRecovery
        require(self.enabled, "Model calls are disabled; restart with --call")
        # Serialize double clicks through preparation and submission. Recovery's
        # worker lock separately protects saved attempts and actual model calls.
        with locked(self.workspace.directory / "recovery-submit.lock"):
            item = self.workspace.item(item_id)
            require(item["decision"] != "skipped", "Restore this item before processing")
            require(item["job_id"] is not None, "Recovery requires a failed note")
            job = self.workspace.job(item["job_id"])
            require(job["state"] == "attention", "Recovery requires a failed note")
            require(not self.workspace.uses_content(job["id"]),
                    "这条材料使用 Markdown 整理流程；请先检查来源、模型回复或网络错误，不能转入旧格式修复。")
            require(not (self.workspace.directory / "jobs" / job["id"] / f"{CompleteRecovery.revision}.json").exists(),
                    "Recovery record preserved; no further automatic attempt")
            recovery = CompleteRecovery(self.workspace)
            job = recovery.prepare(job["id"], allow_calls=True)
            if job["state"] == "queued":
                self.executor.submit(recovery.run, job["id"], self.key_file)
            return {"job_id": job["id"], "state": job["state"]}

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)


def handler(application):
    # Keep a running server on one UI version until its controlled refresh.
    # All three resources are captured before accepting any request.
    assets = {name: (ASSETS / name).read_bytes() for name in ("index.html", "app.js", "style.css")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Avoid logging searches, chat text, tokens or request bodies.
            pass

        def send(self, code, body, content_type="application/json; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            host = urlsplit("//" + self.headers.get("Host", ""))
            require(host.hostname in ("127.0.0.1", "localhost", "::1") and host.username is None, "Only localhost is supported")
            return host

        def do_GET(self):
            try:
                self.trusted_host()
                path = urlsplit(self.path)
                if path.path in ("/", "/app.js", "/style.css"):
                    name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path.path]
                    kind = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                    return self.send(200, assets[name], kind + "; charset=utf-8")
                if path.path == "/api/session":
                    return self.send(200, encoded({"token": application.token, "model_enabled": application.enabled,
                        "vault_enabled": application.library is not None, "vault_path": str(application.vault) if application.vault else None}))
                if path.path == "/api/items":
                    args = parse_qs(path.query)
                    result = application.list(args.get("view", ["todo"])[0], args.get("q", [""])[0][:300],
                                                        int(args.get("page", ["1"])[0]))
                    return self.send(200, encoded(result))
                match = re.fullmatch(r"/api/items/([a-f0-9]{64})(/note|/download)?", path.path)
                require(match is not None, "Unknown route")
                item_id, action = match.groups()
                if action:
                    title, content = application.workspace.note(item_id)
                    if action == "/note":
                        return self.send(200, encoded({"title": title, "markdown": content.decode(),
                            "review_status": application.workspace.review_status(item_id),
                            "imported": application.receipt(item_id)}))
                    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", title).strip(". ")[:100] or "note"
                    return self.send(200, content, "text/markdown; charset=utf-8", {
                        "Content-Disposition": "attachment; filename=note.md; filename*=UTF-8''" + quote(title + ".md", safe="")})
                return self.send(200, encoded({**application.workspace.details(item_id), "imported": application.receipt(item_id)}))
            except (ValueError, KeyError, TypeError) as error:
                self.send(400, encoded({"error": str(error)}))
            except Exception:
                self.send(500, encoded({"error": "读取失败，原记录已保留；请检查服务器日志或状态文件。"}))

        def do_POST(self):
            try:
                host = self.trusted_host()
                origin = self.headers.get("Origin")
                if origin:
                    parsed = urlsplit(origin)
                    require(parsed.scheme == "http" and parsed.netloc == host.netloc, "Origin mismatch")
                require(self.headers.get("X-Workbench-Token") == application.token, "Invalid action token")
                length = int(self.headers.get("Content-Length", "0"))
                require(0 <= length <= 1024, "Request body too large")
                body = loads(self.rfile.read(length) or b"{}")
                require(body == {}, "Actions take no model instructions or paths")
                require(not self.headers.get("Transfer-Encoding"), "Transfer encoding unsupported")
                match = re.fullmatch(r"/api/items/([a-f0-9]{64})/(skip|restore|process|import|recover)", self.path)
                require(match is not None, "Unknown action")
                item_id, action = match.groups()
                if action == "import":
                    return self.send(200, encoded(application.import_note(item_id,
                        review_confirmed=self.headers.get("X-Workbench-Review-Confirmed") == "true")))
                if action == "recover":
                    return self.send(202, encoded(application.recover(item_id)))
                if action == "process":
                    require(application.enabled, "Model calls disabled")
                    job = application.workspace.request(item_id)
                    if job["state"] in ("queued", "running"):
                        application.submit(job["id"])
                    return self.send(202, encoded({"job_id": job["id"], "state": job["state"]}))
                return self.send(200, encoded(application.workspace.decide(item_id, action)))
            except (ValueError, KeyError, TypeError) as error:
                self.send(400, encoded({"error": str(error)}))
            except Exception:
                self.send(500, encoded({"error": "操作未完成，记录已保留；不会自动重复调用模型。"}))
    return Handler


def serve(directory, port=8766, key_file=None, call=False, vault=None, resume_existing=True):
    application = Application(directory, key_file, call, vault)
    try:
        with locked(application.workspace.directory / "server.lock", blocking=False):
            with ThreadingHTTPServer(("127.0.0.1", port), handler(application)) as server:
                server.daemon_threads = True
                if call and resume_existing:
                    for job_id in application.workspace.unfinished():
                        application.submit(job_id)
                print(json.dumps({"url": f"http://127.0.0.1:{port}", "pid": __import__("os").getpid(),
                                  "model_calls_enabled": call, "workspace": str(application.workspace.directory),
                                  "vault": str(application.vault) if application.vault else None}, ensure_ascii=False), flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    finally:
        application.close()
