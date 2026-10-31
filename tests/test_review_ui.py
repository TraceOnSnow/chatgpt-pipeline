"""Review drafts remain readable and explicit imports keep their review metadata."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from chatgpt_pipeline import bulk, notes, workbench
from chatgpt_pipeline.verify import loads, sha
import test_workbench_import as fixtures


class ReviewInboxTests(unittest.TestCase):
    setUp = fixtures.WorkbenchImportTests.setUp
    respond_selection = fixtures.WorkbenchImportTests.respond_selection
    response = fixtures.WorkbenchImportTests.response
    library = fixtures.WorkbenchImportTests.library
    server = fixtures.WorkbenchImportTests.server
    read = fixtures.WorkbenchImportTests.read
    post = fixtures.WorkbenchImportTests.post

    def review_draft(self):
        job = self.workspace.request(self.item)
        directory = self.directory / "jobs" / job["id"]
        content = "# 我的实验\n\n这里保留可读初稿，独立复核尚未完成。\n".encode()
        (directory / "note.md").write_bytes(content)
        bulk.save(directory / "result.json", {"title": "我的实验", "markdown_sha256": sha(content)})
        with self.workspace.db() as db:
            db.execute("UPDATE jobs SET state='review',error='review transport unavailable' WHERE id=?", (job["id"],))
        metadata = {"status": "unavailable", "reason": "独立复核暂未完成，初稿已保留。",
                    "warnings": ["代码来源尚待核对。"]}
        self.addCleanup(patch.stopall)
        patch.object(notes.Workspace, "review_status", return_value=metadata).start()
        return metadata

    def test_review_draft_list_preview_and_download_without_model_or_import(self):
        metadata = self.review_draft()
        app, base = self.server()
        with patch.object(notes.Workspace, "request", side_effect=AssertionError("No model request")), \
                patch.object(notes.Workspace, "run", side_effect=AssertionError("No model execution")):
            result = self.read(base + "/api/items?view=review")
            self.assertEqual(result["counts"]["review"], 1)
            self.assertEqual(result["items"][0]["review_status"], metadata)
            note = self.read(base + f"/api/items/{self.item}/note")
            self.assertEqual(note["review_status"], metadata)
            self.assertIsNone(note["imported"])
            self.assertIn("我的实验", note["markdown"])
            with urllib.request.urlopen(base + f"/api/items/{self.item}/download") as response:
                self.assertEqual(response.read().decode(), note["markdown"])
        self.assertEqual(self.workspace.usage()["calls"], 0)
        self.assertIsNone(app.receipt(self.item))

    def test_review_import_requires_confirmation_and_preserves_source_and_edits(self):
        metadata = self.review_draft()
        vault = self.library()
        app, base = self.server(vault)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(app, base)
        self.assertIn("明确确认", loads(caught.exception.read())["error"])
        self.assertIsNone(app.receipt(self.item))
        with patch.object(app.library, "import_note", wraps=app.library.import_note) as imported, \
                patch.object(notes.Workspace, "run", side_effect=AssertionError("No model execution")):
            headers = {"X-Workbench-Token": app.token, "X-Workbench-Review-Confirmed": "true"}
            first = self.post(app, base, headers=headers)
            self.assertEqual(imported.call_args.kwargs["source"]["review_status"], metadata)
            self.assertEqual(imported.call_args.kwargs["source_id"], "chatgpt-workbench:" + self.item)
            destination = vault / first["path"]
            edited = destination.read_text() + "\n用户补充，必须保留。\n"
            destination.write_text(edited)
            second = self.post(app, base, headers=headers)
            self.assertEqual(second["outcome"], "existing")
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(destination.read_text(), edited)
        self.assertEqual(self.workspace.job(self.workspace.item(self.item)["job_id"])["state"], "review")
        self.assertEqual(self.workspace.usage()["calls"], 0)

    def test_markdown_failure_cannot_enter_legacy_repair(self):
        job = self.workspace.request(self.item)
        with self.workspace.db() as db:
            db.execute("UPDATE jobs SET state='attention',error='fixture network failure' WHERE id=?", (job["id"],))
        app = workbench.Application(self.directory, enabled=True)
        self.addCleanup(app.close)
        with patch.object(notes.Workspace, "uses_content", return_value=True), \
                patch("chatgpt_pipeline.note_recovery.CompleteRecovery.prepare", side_effect=AssertionError("Legacy repair forbidden")):
            item = app.list("attention", "", 1)["items"][0]
            self.assertFalse(item["recovery_available"])
            self.assertIn("原记录已保留", item["recovery_unavailable_reason"])
            with self.assertRaisesRegex(ValueError, "不能转入旧格式修复"):
                app.recover(self.item)
        self.assertEqual(self.workspace.usage()["calls"], 0)

    def test_ui_confirms_current_metadata_and_keeps_warning_visible(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
class Node {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.textContent='';this.dataset={};this.hidden=false;this.isConnected=true;this.classList={add(){},remove(){},toggle(){}};}
  append(...children){this.children.push(...children);} addEventListener(){} setAttribute(){} removeAttribute(){}
  replaceChildren(...children){this.children=children;} contains(){return false;} focus(){} closest(){return null;}
}
const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
const document={getElementById:get,createElement:tag=>new Node(tag),createTextNode:text=>({textContent:text}),body:new Node()};document.activeElement=document.body;
const context=vm.createContext({document,console,URL,URLSearchParams,AbortController,addEventListener(){},setInterval(){},setTimeout,clearTimeout,confirm:()=>false});
vm.runInContext(SOURCE.replace(/start\(\);setInterval\([\s\S]*$/, ''),context);
vm.runInContext(`session={token:'fixture',vault_enabled:true};items=[{id:'item',status:'done'}];renderItems=()=>{};notice=()=>{};refresh=async()=>{};`,context);
const review={status:'needs_review',reason:'独立复核发现归属仍需核对。',warnings:['代码并非已验证的原文。']};
let posts=0,reads=0,confirmed=false;
context.api=async(path,action,signal,headers)=>{if(!action){reads++;return {title:'初稿',markdown:'# 初稿\n正文',review_status:review};}posts++;assert.equal(headers['X-Workbench-Review-Confirmed'],'true');return {id:'library-id',path:'note.md'};};
context.confirm=text=>{assert.match(text,/归属/);assert.match(text,/代码并非/);return confirmed;};
(async()=>{
  await vm.runInContext("importNote('item')",context);assert.equal(posts,0);assert.equal(reads,1);
  confirmed=true;await vm.runInContext("importNote('item')",context);assert.equal(posts,1);assert.equal(reads,2);
  vm.runInContext('previewStatus(null,'+JSON.stringify(review)+')',context);assert.match(get('preview-status').textContent,/待复核/);assert.match(get('preview-status').textContent,/代码并非/);
  const card=vm.runInContext('cardFor({id:"draft",title:"标题",summary:"摘要",status:"review",review_status:'+JSON.stringify(review)+'})',context);
  const strings=[];function walk(n){if(n.textContent)strings.push(n.textContent);for(const child of n.children||[])walk(child);}walk(card);
  assert.ok(strings.includes('查看初稿'));assert.ok(strings.includes('下载 Markdown'));assert.ok(strings.some(text=>text.includes('代码并非')));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        source = (workbench.ASSETS / "app.js").read_text()
        result = subprocess.run([node, "-e", "const SOURCE=" + json.dumps(source) + ";\n" + script],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
