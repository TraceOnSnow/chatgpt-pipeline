"""Offline auditing and readable reports for completed sample requests."""
from decimal import Decimal
import os
from pathlib import Path

from .index import now
from .samples import save, validate
from .verify import encoded, loads, require, sha


def audit_evidence(result, messages):
    """Collect every citation error; formatting differences remain strict failures."""
    by_id = {message["id"]: message for message in messages}
    groups = [("overview", result.get("evidence", []), None)]
    for i, fragment in enumerate(result.get("fragments", []), 1):
        for j, claim in enumerate(fragment.get("claims", []), 1):
            role = claim.get("attribution")
            groups.append((f"fragment-{i}/claim-{j}", claim.get("evidence", []),
                           role if role in ("user", "assistant") else None))
    checked, issues = [], []
    for location, items, role in groups:
        if not isinstance(items, list):
            issues.append({"location": location, "type": "invalid_evidence_array"})
            continue
        if not 1 <= len(items) <= 3:
            issues.append({"location": location, "type": "evidence_count", "count": len(items)})
        for item in items:
            if not isinstance(item, dict):
                issues.append({"location": location, "type": "invalid_evidence_item"})
                continue
            alias, quote = item.get("message_id"), item.get("quote")
            message = by_id.get(alias) if isinstance(alias, str) else None
            exact = bool(message and isinstance(quote, str) and quote in message["text"])
            citation = {"location": location, "message_id": alias, "quote": quote,
                        "source_message_id": message.get("source_message_id") if message else None,
                        "role": message.get("role") if message else None, "verbatim": exact}
            checked.append(citation)
            if message is None:
                issues.append({"location": location, "type": "unknown_message", "message_id": alias})
            elif not exact:
                issues.append({"location": location, "type": "non_verbatim_quote", "evidence": item})
            if isinstance(quote, str) and not 8 <= len(quote) <= 120:
                issues.append({"location": location, "type": "quote_length", "characters": len(quote)})
            if message and role and message["role"] != role:
                issues.append({"location": location, "type": "role_mismatch", "message_id": alias})
    return {"checked": checked, "issues": issues}


def report(output):
    """Read existing responses only. Never issues or retries a model request."""
    output = Path(output).resolve()
    manifest = loads((output / "manifest.json").read_bytes())
    semantic_path = output / "semantic-review.json"
    semantic = loads(semantic_path.read_bytes()) if semantic_path.exists() else {}
    rows, lines = [], ["DeepSeek 片段工作流：真实结果与复核", "",
                      "以下模型输出是档案整理候选；程序校验与语义复核分别记录。",
                      "语义复核由 Codex 对照原文完成，个人价值判断仍待用户确认。", ""]
    for job in manifest["jobs"]:
        directory = output / job["name"]
        source = loads((directory / "input.json").read_bytes())
        payload = loads((directory / "request.json").read_bytes())
        require(sha(encoded(source, sort=True)) == job["input_sha256"]
                and sha(encoded(payload, sort=True)) == job["request_sha256"], "Prepared artifact changed")
        source_valid = sha(Path(source["source"]["source_path"]).read_bytes()) == source["source"]["byte_sha256"]
        require(source_valid, "Original source bytes changed")
        state = loads((directory / "status.json").read_bytes())
        row = {"job": job["name"], "source": source["source"], "state": state["state"],
               "source_bytes_match": source_valid, "usage": state.get("usage"), "response_id": state.get("response_id"),
               "returned_model": state.get("returned_model"), "strict_validation_passed": False}
        candidate = None
        try:
            raw = (directory / "response.raw").read_bytes()
            row["response_sha256"] = sha(raw)
            response = loads(raw)
            require(response == loads((directory / "response.json").read_bytes()), "Parsed response differs from raw response")
            require(response.get("id") == row["response_id"], "Response identity mismatch")
            choice = response["choices"][0]
            require(choice.get("finish_reason") == "stop", "Incomplete generation")
            candidate = loads(choice["message"]["content"])
            require(isinstance(candidate, dict), "Candidate is not an object")
            row["disposition"] = candidate.get("disposition")
            row["fragment_count"] = len(candidate.get("fragments", []))
            row["evidence_audit"] = audit_evidence(candidate, source["messages"])
            validate(candidate, source["messages"])
            row["strict_validation_passed"] = True
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            row["validation_error"] = str(error)
        row["semantic_review"] = semantic.get("jobs", {}).get(job["name"], {"state": "pending"})
        rows.append(row)
        lines.extend([job["name"] + "：" + str(source["source"]["title"]),
                      "原聊天：" + str(source["source"]["url"]),
                      "原始正文：" + source["source"]["source_path"],
                      "程序状态：" + state["state"],
                      "严格校验：" + ("通过" if row["strict_validation_passed"] else "未通过"),
                      "调用编号：" + str(row["response_id"])])
        if row.get("validation_error"):
            lines.append("首个校验问题：" + row["validation_error"])
        for issue in row.get("evidence_audit", {}).get("issues", []):
            lines.append("引用检查问题：" + encoded(issue).decode())
        lines.append("实际用量与费用：" + encoded(row["usage"]).decode())
        if candidate:
            lines.extend(["", "以下保留模型原文；未通过校验的部分仅供复核：",
                          "筛选：" + str(candidate.get("disposition")), "理由：" + str(candidate.get("reason")),
                          "概览：" + str(candidate.get("overview"))])
            for index, fragment in enumerate(candidate.get("fragments", []), 1):
                lines.extend(["", f"片段 {index}：{fragment.get('title')}（{fragment.get('kind')}）",
                              str(fragment.get("summary"))])
                for claim in fragment.get("claims", []):
                    lines.append(f"- [{claim.get('attribution')}/{claim.get('status')}] {claim.get('text')}")
                    for evidence in claim.get("evidence", []):
                        original = next((m for m in source["messages"] if m["id"] == evidence.get("message_id")), {})
                        lines.append(f"  来源 {evidence.get('message_id')} / {original.get('source_message_id')}：{evidence.get('quote')}")
        lines.extend(["", "Codex 对照原文的语义复核：", encoded(row["semantic_review"]).decode(), "", "=" * 60, ""])
    usage_complete = all(isinstance(r["usage"], dict) and r["usage"].get("cost") is not None for r in rows)
    cost = sum((Decimal(str(r["usage"]["cost"])) for r in rows), Decimal(0)) if usage_complete else None
    totals = {"calls_with_responses": sum(bool(r.get("response_id")) for r in rows),
              "strict_validation_passed": sum(r["strict_validation_passed"] for r in rows),
              "fragments": sum(r.get("fragment_count", 0) for r in rows),
              "prompt_tokens": sum((r["usage"] or {}).get("prompt_tokens", 0) for r in rows),
              "completion_tokens": sum((r["usage"] or {}).get("completion_tokens", 0) for r in rows),
              "reported_cost_usd": str(cost) if cost is not None else None,
              "cost_complete": usage_complete, "user_acceptance": "pending"}
    audit = {"reviewed_at": now(), "totals": totals, "jobs": rows}
    save(output / "audit.json", audit)
    lines[4:4] = ["实际汇总：" + encoded(totals).decode(), ""]
    fd = os.open(output / "report.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    return {"report": str(output / "report.txt"), **totals}
