"""Conservative current-branch normalization; never executes archive content."""
from .verify import encoded, sha

VERSION = "current-branch-v1"


def normalize(package):
    issues = []
    mapping = package["raw"].get("mapping")
    branch = []
    seen = set()
    node = package["raw"].get("current_node")
    if not isinstance(mapping, dict) or node is None:
        issues.append("missing_branch")
    else:
        while node is not None:
            if not isinstance(node, str) or node in seen or node not in mapping:
                issues.append("broken_branch")
                break
            seen.add(node)
            item = mapping[node]
            if not isinstance(item, dict):
                issues.append("invalid_branch_node")
                break
            message = item.get("message")
            if message is not None:
                if not isinstance(message, dict) or not isinstance(message.get("author"), dict):
                    issues.append("invalid_raw_message")
                elif message["author"].get("role") in ("user", "assistant"):
                    branch.append((node, message))
            if "parent" not in item:
                issues.append("missing_parent")
            node = item.get("parent")
    branch.reverse()
    messages = package.get("messages")
    if not isinstance(messages, list):
        issues.append("invalid_messages")
        messages = []
    if package.get("readable_status") != "complete_current_branch":
        issues.append("incomplete_readable_branch")
    if [m.get("id") if isinstance(m, dict) else None for m in messages] != [m.get("id") for _, m in branch]:
        issues.append("branch_message_mismatch")
    by_id = {m.get("id"): (n, m) for n, m in branch if isinstance(m.get("id"), str)}
    normalized = []
    ids = set()
    for position, message in enumerate(messages):
        local = []
        if not isinstance(message, dict) or not isinstance(message.get("id"), str) or not message["id"]:
            issues.append(f"invalid_message:{position}")
            continue
        identity = message["id"]
        if identity in ids:
            issues.append(f"duplicate_message:{identity}")
        ids.add(identity)
        node_id, raw = by_id.get(identity, (None, {}))
        role = message.get("role")
        if raw.get("content") != message.get("content") or raw.get("author", {}).get("role") != role:
            local.append("raw_message_mismatch")
        content = message.get("content")
        text, attachments = [], []
        kind = content.get("content_type") if isinstance(content, dict) else None
        if kind in ("text", "multimodal_text"):
            parts = content.get("parts")
            if not isinstance(parts, list):
                local.append("invalid_parts")
            else:
                for part in parts:
                    if isinstance(part, str):
                        text.append(part)
                    elif isinstance(part, dict) and kind == "multimodal_text":
                        attachments.append(part)
                        part_kind = part.get("content_type")
                        if part_kind in ("text", "audio_transcription"):
                            if isinstance(part.get("text"), str):
                                text.append(part["text"])
                            else:
                                local.append("invalid_multimodal_text")
                        elif part_kind not in ("image_asset_pointer", "audio_asset_pointer",
                                              "real_time_user_audio_video_asset_pointer"):
                            local.append("unknown_multimodal_part")
                    else:
                        local.append("unknown_content_part")
        elif kind == "code":
            if isinstance(content.get("text"), str):
                text.append(content["text"])
            else:
                local.append("invalid_code")
        elif kind not in ("thoughts", "reasoning_recap"):
            local.append("unknown_content_type")
        channel, recipient = raw.get("channel"), raw.get("recipient")
        exclusion = None
        if kind in ("thoughts", "reasoning_recap") or channel in ("analysis", "justify", "confidence"):
            exclusion = "reasoning"
        elif recipient not in (None, "all"):
            exclusion = "tool_call"
        elif channel not in (None, "final", "commentary"):
            exclusion = "unknown_channel"
            local.append(exclusion)
        elif role not in ("user", "assistant"):
            exclusion = "unknown_role"
            local.append(exclusion)
        if not text:
            exclusion = exclusion or "no_available_text"
        normalized.append({"id": identity, "position": position, "role": role,
                           "node_id": node_id, "channel": channel, "recipient": recipient,
                           "content_type": kind, "text": "\n".join(text),
                           "attachments": attachments, "metadata": message.get("metadata"),
                           "created": message.get("created"), "source_content": content,
                           "exclusion": exclusion, "issues": local})
        issues.extend(f"{identity}:{item}" for item in local)
    analysis = [{"id": m["id"], "role": m["role"], "text": m["text"]}
                for m in normalized if not m["exclusion"]]
    fingerprint = sha(encoded({"version": VERSION, "messages": analysis}, sort=True))
    return {"version": VERSION, "branch": "current", "current_node": package["raw"].get("current_node"),
            "messages": normalized, "analysis_messages": analysis, "input_fingerprint": fingerprint,
            "issues": issues, "state": "blocked" if issues else "ready" if analysis else "empty"}
