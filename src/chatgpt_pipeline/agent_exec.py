"""Run sealed selection jobs with an agent instead of the HTTP provider.

The job, its frozen request (system prompt, rendered chat, refs) and the validator are unchanged;
only the executor differs. An agent reads the exported task, writes the JSON answer, and
``record`` validates it with ``bulk.validate`` before saving. Nothing here sends network requests.
"""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import re

from . import bulk
from .index import now
from .verify import loads, require, sha

EXECUTOR = re.compile(r"[A-Za-z0-9_.:/-]{1,100}")
RECEIPT = "agent-receipt.json"
RESPONSE = "agent-response.json"
CONTRACT = ("Reply with exactly one JSON object and nothing else: "
            '{"disposition":"keep|review|skip","reason":"...","items":[{"title":"...","summary":"...",'
            '"user_message_ids":["m001"],"related_message_ids":[]}]}. Message IDs are the m### labels in the '
            "user content; user_message_ids must point at user messages and every item needs at least one "
            "[本段] reference. skip means zero items; keep/review need 1-6 items.")


@contextmanager
def _runner_lock(store, wait=False):
    lock_file = (store / "runner.lock").open("a")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError("A selection runner already holds the global lock")
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _open(directory):
    return {name: (directory / name).exists() for name in ("attempt.json", RECEIPT)}


def tasks(output, out_dir):
    """Export every unattempted job of the batch (screen, then reviews of finished screens) as task files."""
    output, manifest, store = bulk.batch(output)
    out_dir = Path(out_dir)
    with _runner_lock(store):
        jobs, reviews = bulk.member_jobs(manifest, store, create_reviews=True)
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        listed = []
        for job_id in jobs:
            directory, definition, data, request = bulk.load_job(store, job_id)
            state = bulk.read(directory / "state.json")
            if state["state"] != "prepared" or any(_open(directory).values()):
                continue
            stage = definition["identity"]["stage"]
            task = {"job_id": job_id, "stage": stage, "conversation_id": data["source"]["conversation_id"],
                    "title": data["source"].get("title", ""), "request_sha256": definition["request_sha256"],
                    "system": request["messages"][0]["content"], "user": request["messages"][1]["content"],
                    "output_contract": CONTRACT}
            bulk.save(out_dir / f"{job_id}.json", task)
            listed.append({"job_id": job_id, "stage": stage, "conversation_id": task["conversation_id"],
                           "title": task["title"], "characters": len(task["system"]) + len(task["user"])})
        index = {"batch": str(output), "created_at": now(), "tasks": listed,
                 "review_jobs_known": len(reviews), "network_requests": 0}
        bulk.save(out_dir / "tasks.json", index)
    return {"tasks": len(listed), "screen": sum(t["stage"] == "screen" for t in listed),
            "review": sum(t["stage"] == "review" for t in listed), "out": str(out_dir)}


def record(output, job_id, response_path, executor, check_only=False):
    """Validate an agent answer for one job and, unless check_only, save it as the job's result."""
    require(EXECUTOR.fullmatch(executor or "") is not None, "Executor must name the agent and model")
    output, manifest, store = bulk.batch(output)
    raw = Path(response_path).read_bytes()
    # Concurrent agents queue on the same lock that excludes the HTTP runner.
    with _runner_lock(store, wait=True):
        jobs, _ = bulk.member_jobs(manifest, store)
        require(job_id in jobs, "Job is not a member of this batch")
        directory, definition, data, request = bulk.load_job(store, job_id)
        state = bulk.read(directory / "state.json")
        opened = _open(directory)
        require(state["state"] == "prepared" and not any(opened.values()),
                "Job already attempted or finished; results are never replaced")
        result = bulk.validate(loads(raw), data["messages"])
        summary = {"job_id": job_id, "stage": definition["identity"]["stage"],
                   "disposition": result["disposition"], "items": len(result["items"]), "recorded": False}
        if check_only:
            return summary
        bulk.atomic_bytes(directory / RESPONSE, raw)
        bulk.save(directory / RECEIPT, {
            "at": now(), "executor": executor, "request_sha256": definition["request_sha256"],
            "response_sha256": sha(raw), "prompt": "request.json system and user messages, unchanged",
            "pipeline_network_requests": 0, "provider_cost_usd": None,
            "cost_note": "Agent executor; not billed per call to the selection ledger"})
        bulk.save(directory / "result.json", result)
        bulk.save(directory / "state.json", {
            **state, "state": "complete", "disposition": result["disposition"], "executor": executor,
            "semantic_review": "agent review only; not verified external facts", "finished_at": now()})
        return {**summary, "recorded": True}
