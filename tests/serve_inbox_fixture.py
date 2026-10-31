"""Disposable browser fixture. All model responses are local mocks."""
import argparse
import hashlib
import json
import signal
from http.server import ThreadingHTTPServer

from chatgpt_pipeline import workbench
from chatgpt_pipeline import bulk
from test_workbench_import import WorkbenchImportTests


def extend(fixture):
    """Synthetic pagination and long-note cases, never new model work."""
    original = fixture.workspace.item(fixture.item)
    second_id = hashlib.sha256(b"browser-second-note").hexdigest()
    job_id = hashlib.sha256(b"browser-second-job").hexdigest()
    with fixture.workspace.db() as db:
        for index in range(35):
            item_id = hashlib.sha256(f"browser-todo-{index}".encode()).hexdigest()
            title = f"阅读材料 {index + 1:02d}"
            if index == 0:
                title += ' <img src=x onerror="globalThis.pwned=true">'
            db.execute("INSERT INTO items(id,ordinal,title,summary,candidate,origin) VALUES(?,?,?,?,?,?)",
                       (item_id, index + 2, title, "供隔离浏览器检查搜索、分页和略过。", original["candidate"], original["origin"]))
        db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?)", (second_id, 1, "第二篇：长笔记", "安全 Markdown 阅读检查。",
                   original["candidate"], original["origin"], "todo", job_id))
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)", (job_id, second_id, "done", None, "fixture", "fixture"))
    markdown = '# 第二篇：长笔记\n\n' + '\n\n'.join(f'第 {index} 段：保留阅读位置和固定操作入口。' for index in range(60))
    markdown += '\n\n**重点** 和 `内联代码`。\n\n1. 第一步\n2. 第二步\n\n> 引用片段\n\n'
    markdown += '<img src=x onerror="globalThis.pwned=true"><script>globalThis.pwned=true</script>\n\n'
    markdown += '[危险](javascript:alert(1)) [安全链接](https://example.org/read)\n\n'
    markdown += '````md\n```html\n<script>globalThis.pwned=true</script>\n```\n````\n'
    destination = fixture.directory / "jobs" / job_id
    destination.mkdir()
    content = markdown.encode()
    (destination / "note.md").write_bytes(content)
    bulk.save(destination / "result.json", {"title": "第二篇：长笔记", "markdown_sha256": hashlib.sha256(content).hexdigest()})
    return second_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-vault", action="store_true")
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--review", action="store_true")
    args = parser.parse_args()
    fixture = WorkbenchImportTests("test_no_vault_keeps_generated_download_available")
    application = server = None
    try:
        fixture.setUp()
        fixture.generated()
        if args.review:
            job_id = fixture.workspace.item(fixture.item)["job_id"]
            result_file = fixture.directory / "jobs" / job_id / "result.json"
            result = bulk.read(result_file)
            bulk.save(result_file, {**result, "review": {
                "status": "needs_review", "reason": "原文中的建议与用户观点仍需核对。",
                "warnings": ["有一段代码尚未确认来自原文。"]}})
            with fixture.workspace.db() as db:
                db.execute("UPDATE jobs SET state='review' WHERE id=?", (job_id,))
        second_id = extend(fixture) if args.extended else None
        vault = None if args.no_vault else fixture.library()
        application = workbench.Application(fixture.directory, vault=vault)
        server = ThreadingHTTPServer(("127.0.0.1", 0), workbench.handler(application))
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                          "vault": str(vault) if vault else None, "item_id": fixture.item, "second_id": second_id}), flush=True)

        def stop(_signum, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        if server:
            server.server_close()
        if application:
            application.close()
        fixture.doCleanups()


if __name__ == "__main__":
    main()
