#!/usr/bin/env python3
"""Preview/apply an explicit delivery plan for completed Workspace notes.

No model transport or archive reading. Example plan:
{"items": [{"item_id": "...", "title": "标题", "path": "现有分类/标题.md"},
           {"item_id": "...", "title": "补稿", "target_id": "..."}]}
Append is the default; replace requires revised_markdown (a complete local file).
For a long topic split across completed jobs, use additional_item_ids plus an
absolute combined_markdown file. Every constituent draft is still validated.
Preview writes private snapshots and returns expected_token; apply requires it.
"""
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent / "obsidian-workflow/src"))

from chatgpt_pipeline import bulk, notes
from chatgpt_pipeline.verify import require, sha
from markdown_library import Library, LibraryError
from markdown_library.core import _split, _read, _hash

SUBJECTS = ("题目与我的作答", "复盘与重做")


def headings(body):
    """Find actual headings, ignoring fenced code (including longer fences)."""
    result, offset, fence = [], 0, None
    for line in body.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", bare)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", bare):
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        else:
            match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", bare)
            if match:
                result.append((offset, offset + len(line), len(match[1]), match[2]))
        offset += len(line)
    return result


def without_title(body):
    found = headings(body)
    if found and found[0][2] == 1 and not body[:found[0][0]].strip():
        return body[found[0][1]:].strip()
    return body.strip()


def demote(body):
    for start, end, level, _ in reversed(headings(body)):
        if level < 6:
            line = body[start:end]
            body = body[:start] + re.sub(r"^( {0,3})#", r"\1##", line) + body[end:]
    return body


def interview_sections(body):
    found = [entry for entry in headings(body) if entry[2] == 2]
    require([entry[3] for entry in found] == list(SUBJECTS),
            "Interview notes require exactly two H2 subjects: " + " / ".join(SUBJECTS))
    return found


def supplement(original, generated, *, interview=False, section=None):
    _, incoming = _split(generated)
    incoming = without_title(incoming)
    require(incoming, "Supplement has no body")
    if interview:
        _, body = _split(original)
        positions = interview_sections(body)
        prefix = original[:-len(body)] if body else original
        if section:
            require(section in SUBJECTS, "Unknown interview subject")
            additions = {section: demote(incoming)}
        else:
            incoming_sections = interview_sections(incoming)
            require(not incoming[:incoming_sections[0][0]].strip(),
                    "Interview supplement must place all prose under its two subjects")
            additions = {}
            for index, (_, end, _, title) in enumerate(incoming_sections):
                stop = incoming_sections[index + 1][0] if index + 1 < len(incoming_sections) else len(incoming)
                additions[title] = incoming[end:stop].strip()
        for index in reversed(range(len(positions))):
            stop = positions[index + 1][0] if index + 1 < len(positions) else len(body)
            addition = additions.get(positions[index][3], "")
            if addition:
                body = body[:stop].rstrip() + "\n\n" + addition + "\n\n" + body[stop:]
        interview_sections(body)
        return prefix + body
    return original.rstrip() + "\n\n## 补充\n\n" + demote(incoming) + "\n"


def read_targets(library, state):
    """One read-only scan; unlike Library._scan this creates no history objects."""
    found = {}
    for directory, dirs, files in os.walk(library.vault, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in dirs:
            library._check_components(Path(directory) / name)
        for name in files:
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            relative = (Path(directory) / name).relative_to(library.vault).as_posix()
            markdown = _read(library._path(relative))
            properties, _ = _split(markdown)
            identity = properties.get("id")
            if not isinstance(identity, str) or identity not in state["notes"]:
                continue
            library._validate_properties(properties)
            require(identity not in found, "Duplicate stable id: " + identity)
            found[identity] = {"id": identity, "path": relative, "markdown": markdown,
                               "properties": properties, "sha256": _hash(markdown)}
    return found


def _load_single_job(workspace, entry):
    item = workspace.item(entry["item_id"])
    job = workspace.job(item["job_id"])
    require(job["state"] in ("done", "review"), "Job has no completed draft")
    _, content = workspace.note(item["id"])
    directory = workspace.directory / "jobs" / job["id"]
    material = bulk.read(directory / "input.json")
    definition = bulk.read(directory / "definition.json")
    require(definition["id"] == job["id"] == bulk.digest(definition["identity"])
            and definition["identity"]["input_sha256"] == bulk.digest(material), "Job input changed")
    review = workspace.review_status(item["id"])
    require(review is not None and (review.get("status") == "passed" or entry.get("review_confirmed") is True),
            "Draft requires explicit review_confirmed")
    source = material["source"]
    sources = material.get("sources", [source])
    require(isinstance(sources, list) and sources and sources[0] == source,
            "Invalid bundle source list or primary source")
    require(all(isinstance(s, dict) and s.get("account_key") and s.get("conversation_id")
                for s in sources), "Missing account/conversation identity")
    contexts = []
    for message in material["messages"]:
        source_index = message.get("source_index", 0)
        require(type(source_index) is int and 0 <= source_index < len(sources), "Invalid message source_index")
        require(len(sources) == 1 or "source_index" in message, "Bundled message lacks source_index")
        origin = sources[source_index]
        contexts.append({"source_index": source_index, "account_key": origin["account_key"],
                         "conversation_id": origin["conversation_id"],
                         "message_id": message["source_message_id"], "role": message["role"],
                         "text_sha256": sha(message["text"].encode())})
    aliases = set(material["personal_evidence_ids"])
    evidence = [context for context, message in zip(contexts, material["messages"])
                if message["id"] in aliases]
    require(evidence and all(m["role"] == "user" for m in evidence), "Missing personal user evidence")
    # The source context is audit information, not a claim of exact prose support.
    provenance = {"type": "chatgpt-memory-delivery-v1", "item_id": item["id"], "job_id": job["id"],
                  "account_key": source["account_key"], "conversation_id": source["conversation_id"],
                  "evidence": evidence, "input_context": contexts, "input_sha256": bulk.digest(material),
                  "note_sha256": sha(content), "review": review,
                  "archive_provenance": deepcopy(source.get("zip_provenance")),
                  "source": deepcopy(source), "sources": deepcopy(sources),
                  "bundle_item_ids": deepcopy(material.get("bundle_item_ids") or
                                               material.get("editorial", {}).get("bundle_item_ids", [])),
                  "bundle": deepcopy(material.get("bundle"))}
    topic = entry.get("topic_key") or entry.get("target_id") or entry.get("path")
    require(isinstance(topic, str) and topic.strip(), "Explicit topic target required")
    if len(sources) == 1:
        # Preserve identities already handed off by the original single-source script.
        legacy_evidence = [{k: m[k] for k in ("message_id", "role", "text_sha256")} for m in evidence]
        identity = {"account_key": source["account_key"], "conversation_id": source["conversation_id"],
                    "evidence": sorted(legacy_evidence, key=lambda m: (m["message_id"], m["text_sha256"])), "topic": topic}
    else:
        # Indices describe the input, not identity: source/message order can change.
        origins = sorted({(s["account_key"], s["conversation_id"]) for s in sources})
        messages = [{k: m[k] for k in ("account_key", "conversation_id", "message_id", "role", "text_sha256")}
                    for m in contexts]
        identity = {"sources": origins, "messages": sorted(messages, key=bulk.digest), "topic": topic}
    return content.decode("utf-8"), provenance, "chatgpt-memory-delivery-v1:" + bulk.digest(identity)


def load_job(workspace, entry):
    """Compose explicitly listed, independently validated completed notes."""
    additional = entry.get("additional_item_ids", [])
    require(isinstance(additional, list) and all(isinstance(item, str) and item for item in additional),
            "additional_item_ids must be a list of item IDs")
    item_ids = sorted(set([entry["item_id"], *additional]))
    require(not additional or entry.get("combined_markdown") or
            (entry.get("mode") == "replace" and entry.get("revised_markdown")),
            "Multiple completed notes require combined_markdown or complete revised_markdown")
    loaded = {item: _load_single_job(workspace, {**entry, "item_id": item}) for item in item_ids}
    markdown, provenance, source_id = loaded[entry["item_id"]]
    if len(loaded) > 1:
        jobs = [loaded[item][1] for item in item_ids]
        sources, indices = [], {}
        for job in jobs:
            for source in job["sources"]:
                digest = bulk.digest(source)
                if digest not in indices:
                    indices[digest] = len(sources)
                    sources.append(deepcopy(source))
        def merged_messages(key):
            result = []
            for job in jobs:
                for message in job[key]:
                    origin = job["sources"][message["source_index"]]
                    result.append({**message, "source_index": indices[bulk.digest(origin)],
                                   "job_source_index": message["source_index"],
                                   "item_id": job["item_id"], "job_id": job["job_id"]})
            return result
        contexts = merged_messages("input_context")
        evidence = merged_messages("evidence")
        # Keep complete per-job input/output fingerprints and archive provenance.
        provenance = {"type": "chatgpt-memory-combined-delivery-v1", "item_ids": item_ids,
                      "job_ids": sorted({job["job_id"] for job in jobs}), "jobs": jobs,
                      "sources": sources, "input_context": contexts, "evidence": evidence}
        canonical = {}
        for message in contexts:
            value = {k: message[k] for k in ("account_key", "conversation_id", "message_id", "role", "text_sha256")}
            canonical[bulk.digest(value)] = value
        identity = {"sources": sorted({(source["account_key"], source["conversation_id"]) for source in sources}),
                    "messages": [canonical[key] for key in sorted(canonical)],
                    "topic": entry.get("topic_key") or entry.get("target_id") or entry.get("path")}
        source_id = "chatgpt-memory-combined-delivery-v1:" + bulk.digest(identity)
    if entry.get("combined_markdown"):
        combined = Path(entry["combined_markdown"])
        require(combined.is_absolute(), "combined_markdown requires an absolute file path")
        raw = combined.read_bytes()
        markdown = raw.decode("utf-8")
        require(markdown.strip(), "combined_markdown must have a nonempty body")
        if entry.get("path"):
            # Reject even an empty YAML block: generated new-note input is body only.
            require(not markdown.removeprefix("\ufeff").startswith(("---\n", "---\r\n")),
                    "New combined_markdown must not supply frontmatter")
        provenance = {**provenance, "combined_markdown": str(combined),
                      "combined_markdown_sha256": sha(raw)}
    return markdown, provenance, source_id


def deliver(workspace_path, vault, plan_path, report_dir, *, apply=False, expected_token=None):
    workspace = notes.Workspace(workspace_path)
    library = Library(Path(vault).absolute()).validate()
    plan = bulk.read(plan_path)
    require(isinstance(plan, dict) and isinstance(plan.get("items"), list) and plan["items"], "Plan needs items")
    report_dir = bulk.runtime_path(report_dir)
    require(not report_dir.is_relative_to(library.vault), "Evidence must stay outside the Vault")
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(report_dir, 0o700)
    with library._lock() if apply else nullcontext():
        require(not library._path(".markdown-library/transaction.json").exists(), "Pending Library transaction; resolve first")
        state = library._load() if apply else library._read_state()
        targets = read_targets(library, state)
        prepared, errors, seen_sources, seen_targets, seen_paths = [], [], set(), set(), set()
        for index, entry in enumerate(plan["items"]):
            try:
                require(isinstance(entry, dict), "Plan item must be an object")
                require(isinstance(entry.get("title"), str) and entry["title"].strip(), "Title required")
                require(bool(entry.get("target_id")) != bool(entry.get("path")), "Specify target_id or new path")
                generated, provenance, source_id = load_job(workspace, entry)
                previous = state["sources"].get(source_id)
                require(source_id not in seen_sources, "Duplicate source/topic in plan")
                seen_sources.add(source_id)
                if previous:
                    record = state["notes"].get(previous["id"], {})
                    require(not record.get("purged") and not record.get("deleted"), "Source identity was deleted")
                    current = targets.get(previous["id"])
                    require(current is not None, "Existing source target is missing; do not recreate")
                    require(current["id"] not in seen_targets, "Multiple plan entries resolve to the same target")
                    seen_targets.add(current["id"])
                    prepared.append({"item_id": entry["item_id"], "source_id": source_id,
                                     "outcome": "existing", "id": current["id"], "path": current["path"],
                                     "sha256": current["sha256"], "provenance": provenance})
                    continue
                mode = entry.get("mode", "append")
                require(mode in ("append", "replace"), "Unknown delivery mode")
                target_id = entry.get("target_id")
                current = targets.get(target_id) if target_id else None
                if target_id:
                    require(current is not None and not state["notes"][target_id].get("deleted"), "Target is missing/deleted")
                    require(target_id not in seen_targets, "Multiple supplements to one target require a single revised plan entry")
                    seen_targets.add(target_id)
                    require(not entry.get("expected_hash") or entry["expected_hash"] == current["sha256"], "Target SHA conflict")
                    interview = entry.get("interview") is True or any(h[3] in SUBJECTS for h in headings(_split(current["markdown"])[1]) if h[2] == 2)
                    if mode == "replace":
                        markdown = _read(Path(entry["revised_markdown"]))
                        revised_properties, revised_body = _split(markdown)
                        require(revised_properties == current["properties"], "Replace must preserve all frontmatter properties")
                        # Keep even user YAML comments, formatting and key order byte-for-byte.
                        _, original_body = _split(current["markdown"])
                        prefix = current["markdown"][:-len(original_body)] if original_body else current["markdown"]
                        markdown = prefix + revised_body
                        old_boxes = Counter(re.findall(r"(?m)^.*[-*+] \[[ xX]\].*$", current["markdown"]))
                        new_boxes = Counter(re.findall(r"(?m)^.*[-*+] \[[ xX]\].*$", markdown))
                        require(not (old_boxes - new_boxes), "Replace must preserve user checkboxes")
                        if interview:
                            interview_sections(_split(markdown)[1])
                    else:
                        markdown = supplement(current["markdown"], generated, interview=interview, section=entry.get("section"))
                    row = {"outcome": "supplemented" if mode == "append" else "replaced", "id": target_id,
                           "path": current["path"], "expected_hash": current["sha256"], "markdown": markdown,
                           "original_markdown": current["markdown"]}
                else:
                    require(mode == "append", "New note does not support replace")
                    destination = library._path(entry["path"])
                    require(not destination.exists(), "New path already exists")
                    require(destination.parent.is_dir(), "Choose an existing category directory")
                    require(not any(p.startswith(".") for p in Path(entry["path"]).parts)
                            and destination.suffix.lower() == ".md", "New path must be a visible Markdown file")
                    require(entry["path"] not in seen_paths, "Duplicate new path in plan")
                    seen_paths.add(entry["path"])
                    require(not _split(generated)[0], "New generated draft must not supply frontmatter")
                    if entry.get("interview") is True:
                        interview_sections(generated)
                    row = {"outcome": "created", "path": entry["path"], "markdown": generated, "expected_hash": None}
                prepared.append({**row, "title": entry["title"], "item_id": entry["item_id"],
                                 "source_id": source_id, "provenance": provenance, "sha256": _hash(markdown if target_id else generated)})
            except (ValueError, LibraryError, KeyError, TypeError, OSError) as exc:
                errors.append({"index": index, "item_id": entry.get("item_id") if isinstance(entry, dict) else None,
                               "error": str(exc)})
        token = bulk.digest({"vault": str(library.vault), "plan": plan, "prepared": prepared})
        run_dir = report_dir / token
        run_dir.mkdir(mode=0o700, exist_ok=True)
        for index, row in enumerate(prepared):
            if "original_markdown" in row:
                bulk.atomic_bytes(run_dir / f"{index:04d}-before.md", row["original_markdown"].encode())
            if "markdown" in row:
                bulk.atomic_bytes(run_dir / f"{index:04d}-proposed.md", row["markdown"].encode())
        result = {"applied": False, "expected_token": token, "errors": errors,
                  "items": [{k: v for k, v in row.items() if k not in {"markdown", "original_markdown", "provenance"}}
                            for row in prepared], "report_dir": str(run_dir)}
        bulk.save(run_dir / "preview.json", {**result, "plan": plan, "prepared": prepared})
        if apply:
            require(not errors, "Delivery has errors; inspect private preview.json; nothing applied")
            require(expected_token == token, "Preview token differs; review current source and note snapshots")
            writes = []
            for index, row in enumerate(prepared):
                if row["outcome"] == "existing":
                    continue
                if row["outcome"] == "created":
                    note = library._create(state, row["markdown"], title=row["title"], kind="note", path=row["path"])
                else:
                    current = targets[row["id"]]
                    library._expect({"sha256": _hash(_read(library._path(current["path"])))}, row["expected_hash"])
                    library._remember(state, current, "memory-delivery-before")
                    note = {**current, "markdown": row["markdown"], "sha256": row["sha256"]}
                    library._remember(state, note, "memory-delivery-" + row["outcome"])
                state["sources"][row["source_id"]] = {"id": note["id"], "source": row["provenance"]}
                writes.append((note["path"], note["markdown"], row["expected_hash"]))
                result["items"][index].update({"id": note["id"], "sha256": note["sha256"]})
            if writes:
                library._commit(state, writes)
            for row in result["items"]:
                require(_hash(_read(library._path(row["path"]))) == row["sha256"], "Post-write readback differs")
            result["applied"] = True
            bulk.save(run_dir / "receipt.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("workspace", "vault", "plan", "report-dir"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-token")
    args = parser.parse_args()
    try:
        result = deliver(args.workspace, args.vault, args.plan, args.report_dir,
                         apply=args.apply, expected_token=args.expected_token)
    except (ValueError, LibraryError, OSError) as exc:
        print(json.dumps({"applied": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
