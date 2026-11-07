"""Disposable inbox + real Markdown library integration; no real model transport."""
from pathlib import Path
import builtins
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from chatgpt_pipeline import notes, workbench
from chatgpt_pipeline.verify import encoded, loads
import test_notes as fixtures

try:
    from markdown_library import Library, LibraryError
except ImportError:
    Library = LibraryError = None


class WorkbenchImportTests(unittest.TestCase):
    setUp = fixtures.NoteTests.setUp
    respond_selection = fixtures.NoteTests.respond_selection
    response = fixtures.NoteTests.response
    draft = fixtures.NoteTests.draft
    responses = fixtures.NoteTests.responses

    def generated(self):
        job = self.workspace.request(self.item)
        with patch.object(notes, "credential", return_value="fixture-only"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()):
            self.assertEqual(self.workspace.run(job["id"])["state"], "done")
        return job

    def library(self):
        if Library is None:
            self.skipTest("Install optional markdown-library or add sibling src to PYTHONPATH")
        vault = self.base / "vault"
        Library.initialize(vault)
        return vault

    def server(self, vault=None):
        app = workbench.Application(self.directory, vault=vault)
        self.addCleanup(app.close)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), workbench.handler(app))
        except PermissionError:
            self.skipTest("Loopback sockets unavailable")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return app, f"http://127.0.0.1:{server.server_port}"

    def read(self, url):
        with urllib.request.urlopen(url) as response:
            return loads(response.read())

    def post(self, app, base, body=b"{}", headers=None, action="import"):
        request = urllib.request.Request(base + f"/api/items/{self.item}/{action}", data=body,
            headers=headers if headers is not None else {"X-Workbench-Token": app.token})
        with urllib.request.urlopen(request) as response:
            return loads(response.read())

    def test_no_vault_keeps_generated_download_available(self):
        self.generated()
        app, base = self.server()
        self.assertFalse(self.read(base + "/api/session")["vault_enabled"])
        self.assertEqual(self.read(base + "/api/items?view=done")["imported_count"], 0)
        with urllib.request.urlopen(base + f"/api/items/{self.item}/download") as response:
            self.assertIn("我的实验", response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(app, base)
        self.assertIn("未配置", loads(caught.exception.read())["error"])
        self.assertEqual(self.workspace.list("done")["counts"]["done"], 1)
        self.assertFalse((self.directory / "vault-imports").exists())

    def test_invalid_root_rejected_at_startup_without_creating_anything(self):
        if Library is None:
            self.skipTest("Optional markdown-library unavailable")
        vault = self.base / "missing-vault"
        with self.assertRaisesRegex(ValueError, "initialized Markdown Vault"):
            workbench.Application(self.directory, vault=vault)
        self.assertFalse(vault.exists())
        vault.mkdir()
        with self.assertRaisesRegex(ValueError, "initialized Markdown Vault"):
            workbench.Application(self.directory, vault=vault)
        self.assertEqual(list(vault.iterdir()), [])

    def test_optional_dependency_only_loaded_with_explicit_vault(self):
        original_import = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == "markdown_library":
                raise ImportError("Optional fixture dependency unavailable")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=unavailable):
            app = workbench.Application(self.directory)
            self.addCleanup(app.close)
            self.assertIsNone(app.library)
            with self.assertRaisesRegex(ValueError, "requires markdown-library"):
                workbench.Application(self.directory, vault=self.base / "missing")
        self.assertFalse((self.base / "missing").exists())

    def test_vault_symlink_is_rejected_without_resolving_away_boundary(self):
        vault = self.library()
        link = self.base / "vault-link"
        link.symlink_to(vault, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            workbench.Application(self.directory, vault=link)

    def test_import_repeat_after_edit_and_restart_preserves_file_and_model_jobs(self):
        self.generated()
        vault = self.library()
        app, base = self.server(vault)
        with patch.object(notes.Workspace, "request", side_effect=AssertionError("No model resubmission")), \
             patch.object(notes.Workspace, "run", side_effect=AssertionError("No model execution")):
            first = self.post(app, base, body=b"")
            self.assertEqual(first["outcome"], "created")
            destination = vault / first["path"]
            self.assertIn("我的实验", destination.read_text())
            self.assertNotIn("markdown", first)
            edited = destination.read_text() + "\n\n用户在 Obsidian 中补充的段落。\n"
            destination.write_text(edited)
            again = self.post(app, base)
            self.assertEqual(again["id"], first["id"])
            self.assertEqual(again["outcome"], "existing")
            self.assertEqual(destination.read_text(), edited)
            other = workbench.Application(self.directory, vault=vault)
            self.addCleanup(other.close)
            result = other.list("done", "", 1)
            self.assertEqual(result["imported_count"], 1)
            self.assertEqual(result["items"][0]["imported"]["id"], first["id"])
            # Crash after library commit but before receipt: source-id mapping repairs it.
            (other.receipts / f"{self.item}.json").unlink()
            repaired = other.import_note(self.item)
            self.assertEqual(repaired["outcome"], "existing")
            self.assertEqual(repaired["id"], first["id"])
            self.assertEqual(destination.read_text(), edited)
        self.assertEqual(self.workspace.usage()["calls"], 2)
        self.assertEqual(len(list(destination.parent.glob("*.md"))), 1)

    def test_failure_keeps_generated_and_retry_does_not_run_model(self):
        self.generated()
        app, base = self.server(self.library())
        with patch.object(app.library, "import_note", side_effect=LibraryError("fixture failure")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post(app, base)
            self.assertIn("不会调用模型", loads(caught.exception.read())["error"])
        self.assertIsNone(app.receipt(self.item))
        self.assertEqual(self.workspace.list("done")["counts"]["done"], 1)
        with patch.object(notes.Workspace, "request", side_effect=AssertionError("No model call")):
            self.assertEqual(self.post(app, base)["outcome"], "created")
        self.assertEqual(self.workspace.usage()["calls"], 2)

    def test_import_requires_generated_note_token_origin_and_no_paths(self):
        vault = self.library()
        app, base = self.server(vault)
        with self.assertRaises(urllib.error.HTTPError):
            self.post(app, base)  # Ungenerated item cannot be imported.
        self.generated()
        for body, headers in [
            (b"{}", {}),
            (b"{}", {"X-Workbench-Token": "wrong"}),
            (b"{}", {"X-Workbench-Token": app.token, "Origin": "https://evil.example"}),
            (b"{}", {"X-Workbench-Token": app.token, "Origin": "http://localhost:1"}),
            (encoded({"path": str(vault / "evil.md")}), {"X-Workbench-Token": app.token}),
            (b"[]", {"X-Workbench-Token": app.token}),
            (b"{}", {"X-Workbench-Token": app.token, "Host": "evil.example"}),
        ]:
            with self.subTest(body=body, headers=headers):
                with self.assertRaises(urllib.error.HTTPError):
                    self.post(app, base, body, headers)
        self.assertEqual(list(vault.rglob("*.md")), [])
        self.assertEqual(self.post(app, base, headers={"X-Workbench-Token": app.token, "Origin": base})["outcome"], "created")

    def test_running_handler_keeps_complete_static_snapshot_until_refresh(self):
        assets = self.base / "assets"
        assets.mkdir()
        names = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        for name in names.values():
            (assets / name).write_text("before:" + name)
        with patch.object(workbench, "ASSETS", assets):
            _, base = self.server()
            for name in names.values():
                (assets / name).write_text("after:" + name)
            _, refreshed = self.server()
        for route, name in names.items():
            with self.subTest(route=route):
                with urllib.request.urlopen(base + route) as response:
                    self.assertEqual(response.read().decode(), "before:" + name)
                with urllib.request.urlopen(refreshed + route) as response:
                    self.assertEqual(response.read().decode(), "after:" + name)

    def test_source_xss_stays_data_and_response_has_browser_guards(self):
        payload = '<img src=x onerror="globalThis.pwned=true"><script>alert(1)</script>'
        with self.workspace.db() as db:
            db.execute("UPDATE items SET title=?,summary=? WHERE id=?", (payload, payload, self.item))
        app, base = self.server()
        result = self.read(base + "/api/items")
        self.assertEqual(result["items"][0]["title"], payload)
        with urllib.request.urlopen(base + "/") as response:
            self.assertNotIn(payload, response.read().decode())
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        script = (workbench.ASSETS / "app.js").read_text()
        self.assertNotIn("innerHTML", script)
        self.assertIn("node.textContent=text", script)


if __name__ == "__main__":
    unittest.main()
