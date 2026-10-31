"""The explicit recovery UI action keeps the existing HTTP authorization boundary."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from chatgpt_pipeline import workbench
from chatgpt_pipeline.note_recovery import CompleteRecovery

REVISION = CompleteRecovery.revision


class RecoveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.item_id, self.job_id = "a" * 64, "b" * 64
        self.workspace = Mock()
        self.workspace.uses_content.return_value = False  # These fixtures represent legacy repair jobs.
        self.workspace.directory = Path(self.temp.name)
        self.item = {"id": self.item_id, "decision": "todo", "job_id": self.job_id}
        self.job = {"id": self.job_id, "state": "attention"}
        self.workspace.item.return_value = self.item
        self.workspace.job.return_value = self.job
        self.directory = self.workspace.directory / "jobs" / self.job_id
        self.directory.mkdir(parents=True)
        with patch.object(workbench, "Workspace", return_value=self.workspace):
            self.application = workbench.Application(self.workspace.directory, enabled=True)
        self.addCleanup(self.application.close)
        self.submit = patch.object(self.application.executor, "submit").start()
        self.addCleanup(patch.stopall)
        self.recovery = Mock()
        self.recovery.prepare.side_effect = self.prepare
        self.recovery_class = patch("chatgpt_pipeline.note_recovery.CompleteRecovery", return_value=self.recovery).start()
        self.recovery_class.revision = REVISION

    def prepare(self, job_id, *, allow_calls):
        self.assertEqual(job_id, self.job_id)
        self.assertTrue(allow_calls)
        self.job["state"] = "queued"
        return self.job

    def server(self):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), workbench.handler(self.application))
        except PermissionError:
            self.skipTest("Loopback sockets are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return "http://127.0.0.1:" + str(server.server_port)

    def post(self, url, *, body=None, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Workbench-Token"] = self.application.token
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(url, method="POST", headers=headers,
                                         data=json.dumps(body or {}).encode())
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def list_items(self, status="attention"):
        self.workspace.list.return_value = {"items": [{"id": self.item_id, "job_id": self.job_id, "status": status}]}
        return self.application.list("all", "", 1)["items"][0]

    def test_reading_list_exposes_availability_without_preparing_or_running(self):
        item = self.list_items()
        self.assertTrue(item["recovery_available"])
        self.assertEqual(item["recovery_version"], REVISION)
        for status in ("todo", "working", "done", "skipped"):
            self.assertFalse(self.list_items(status)["recovery_available"])
        (self.directory / f"{REVISION}.json").write_text("{}")
        self.assertFalse(self.list_items()["recovery_available"])
        self.recovery.prepare.assert_not_called()
        self.submit.assert_not_called()

    def test_explicit_authorized_http_action_prepares_then_enqueues_one_worker(self):
        url = self.server() + f"/api/items/{self.item_id}/recover"
        status, result = self.post(url)
        self.assertEqual(status, 202)
        self.assertEqual(result, {"job_id": self.job_id, "state": "queued"})
        self.recovery.prepare.assert_called_once_with(self.job_id, allow_calls=True)
        self.submit.assert_called_once_with(self.recovery.run, self.job_id, None)
        self.workspace.run.assert_not_called()
        self.workspace.note.assert_not_called()
        # A second click while queued cannot create another worker.
        self.assertEqual(self.post(url)[0], 400)
        self.assertEqual(self.submit.call_count, 1)

    def test_http_recovery_requires_token_matching_origin_and_empty_body(self):
        url = self.server() + f"/api/items/{self.item_id}/recover"
        for kwargs in ({"token": False}, {"origin": "https://other.example"},
                       {"body": {"instructions": "do anything"}}, {"body": {"path": "/tmp/other"}}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.post(url, **kwargs)[0], 400)
        self.recovery.prepare.assert_not_called()
        self.submit.assert_not_called()

    def test_disabled_model_mode_cannot_prepare_or_queue_recovery(self):
        self.application.enabled = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.application.recover(self.item_id)
        self.recovery.prepare.assert_not_called()
        self.submit.assert_not_called()

    def test_skipped_missing_unfailed_or_already_attempted_jobs_are_protected(self):
        changes = ((self.item, "decision", "skipped"), (self.item, "job_id", None),
                   (self.job, "state", "done"), (self.job, "state", "running"))
        for target, key, value in changes:
            previous = target[key]
            target[key] = value
            with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                self.application.recover(self.item_id)
            target[key] = previous
        (self.directory / f"{REVISION}.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "record preserved"):
            self.application.recover(self.item_id)
        self.recovery.prepare.assert_not_called()
        self.submit.assert_not_called()

    def test_a_nonqueued_preparation_is_not_submitted(self):
        self.recovery.prepare.side_effect = None
        self.recovery.prepare.return_value = {"id": self.job_id, "state": "done"}
        self.assertEqual(self.application.recover(self.item_id)["state"], "done")
        self.submit.assert_not_called()

    def test_ui_exposes_explicit_confirmation_and_model_disable_behavior(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable for the isolated UI behavior check")
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};}
  append(...nodes){this.children.push(...nodes);}
  addEventListener(){}
}
let confirmation='',calls=0;
const context=vm.createContext({document:{createElement:tag=>new Element(tag)},
  confirm:message=>{confirmation=message;return false;},fetch:()=>{++calls;throw Error('unexpected request');}});
const source=fs.readFileSync(process.argv[1],'utf8').split("$('import-preview').addEventListener")[0];
vm.runInContext(source,context);
vm.runInContext("session={model_enabled:true};",context);
const item={id:'item',title:'A note',summary:'Context',status:'attention',error:'Invalid note fields',recovery_available:true,recovery_version:'recovery-v1'};
function card(value){context.item=value;return vm.runInContext('cardFor(item,{})',context);}
function flatten(node){return [node,...(node.children||[]).flatMap(flatten)];}
let nodes=flatten(card(item));
assert(nodes.some(n=>n.textContent==='修复并重新复核'&&!n.disabled));
assert(nodes.some(n=>n.textContent?.includes('缺少必要内容')));
vm.runInContext("session.model_enabled=false",context);
assert(flatten(card(item)).some(n=>n.textContent==='修复并重新复核'&&n.disabled));
nodes=flatten(card({...item,recovery_available:false}));
assert(!nodes.some(n=>n.textContent==='修复并重新复核'));
assert(nodes.some(n=>n.textContent?.includes('本轮修复记录已保留')));
vm.runInContext("recoverNote('item')",context);
assert.equal(confirmation,'仅重新处理失败阶段，最多两次模型调用，费用计入现有预算；不会自动入库。');
assert.equal(calls,0);
"""
        completed = subprocess.run([node, "-e", script, str(workbench.ASSETS / "app.js")],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
