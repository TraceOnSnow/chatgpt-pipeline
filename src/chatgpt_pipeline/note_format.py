"""Conservative, offline representation repairs before semantic note validation.

This module neither approves a review nor invents prose or general citations.
Changes contain metadata and offsets only, so an audit need not copy private text.
"""
from copy import deepcopy
import re


VERSION = "note-format-v1"
_PLACEHOLDER = re.compile(r"\[\[CODE:([^\]]+)\]\]")
_OPEN = re.compile(r"^(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|$)")


def _fences(markdown):
    """Return complete, top-level fenced blocks; refuse unclosed fences.

    Offsets exclude the closing line's newline, matching saved material.codes.
    A shorter nested delimiter never closes a longer outer delimiter.
    """
    lines = markdown.splitlines(keepends=True)
    blocks, offset, index = [], 0, 0
    while index < len(lines):
        opening = _OPEN.fullmatch(lines[index])
        if not opening:
            offset += len(lines[index])
            index += 1
            continue
        fence, info = opening.groups()
        if fence[0] == "`" and "`" in info:
            return None
        start = offset
        offset += len(lines[index])
        index += 1
        closing = re.compile(re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*")
        while index < len(lines):
            line = lines[index]
            bare = line.removesuffix("\n").removesuffix("\r")
            if closing.fullmatch(bare):
                end = offset + len(bare)
                blocks.append((start, end, markdown[start:end]))
                offset += len(line)
                index += 1
                break
            offset += len(line)
            index += 1
        else:
            return None
    return blocks


def _unique_code(block, codes, refs, declared):
    matches = [key for key, value in codes.items() if value["text"] == block]
    if len(matches) == 1:
        return matches[0]
    if len(matches) < 2:
        return None
    # Existing explicit identifiers may select a duplicate. If both forms of
    # metadata narrow it, they must agree rather than silently overriding one.
    by_id = [key for key in matches if key in declared]
    by_ref = [key for key in matches if codes[key]["message_id"] in refs]
    if len(by_id) == 1 and len(by_ref) == 1 and by_id != by_ref:
        return None
    if len(by_id) == 1:
        return by_id[0]
    if len(by_ref) == 1:
        return by_ref[0]
    return None


def canonicalize(result, material, stage):
    """Return ``(copy, changes)``; callers must still run notes.validate.

    Only the known ``type=json_object`` decoration, unused missing code array,
    exact original fenced blocks, stale valid code IDs, and explicit code-source
    links are supported. Unknown fields, malformed IDs and incomplete reviews
    are left for the validator; no review verdict or prose is synthesized.
    """
    if stage not in ("draft", "review"):
        raise ValueError("Unknown note stage")
    normalized, changes = deepcopy(result), []
    fields = {"title", "markdown", "source_message_ids", "verbatim_code_ids"}
    if stage == "review":
        fields |= {"verdict", "review_reason"}
    if not isinstance(normalized, dict):
        return normalized, changes
    keys = set(normalized)
    if normalized.get("type") == "json_object":
        keys -= {"type"}
    if keys not in (fields, fields - {"verbatim_code_ids"}):
        return normalized, changes
    if "type" in normalized and normalized["type"] != "json_object":
        return normalized, changes
    if "type" in normalized:
        del normalized["type"]
        changes.append({"operation": "remove_json_object_type"})
    markdown = normalized.get("markdown")
    if not isinstance(markdown, str):
        return normalized, changes
    if "verbatim_code_ids" not in normalized:
        if not any(marker in markdown for marker in ("[[CODE:", "```", "~~~")):
            normalized["verbatim_code_ids"] = []
            changes.append({"operation": "add_unused_code_ids", "after": []})
        return normalized, changes

    codes = material.get("codes", {})
    refs, declared = normalized["source_message_ids"], normalized["verbatim_code_ids"]
    allowed_refs = {message["id"] for message in material["messages"]}
    if not (isinstance(refs, list) and refs and
            all(isinstance(key, str) for key in refs) and
            len(refs) == len(set(refs)) and set(refs) <= allowed_refs and
            isinstance(declared, list) and
            all(isinstance(key, str) for key in declared) and
            len(declared) == len(set(declared)) and set(declared) <= set(codes)):
        return normalized, changes
    placeholders = _PLACEHOLDER.findall(markdown)
    # Do not turn an unknown/malformed placeholder into valid metadata.
    if not set(placeholders) <= set(codes) or "[[CODE:" in _PLACEHOLDER.sub("", markdown):
        return normalized, changes
    blocks = _fences(markdown)
    if blocks is None:
        return normalized, changes
    replacements = []
    for start, end, block in blocks:
        key = _unique_code(block, codes, refs, declared)
        if key is None:
            return normalized, changes
        replacements.append((start, end, key))
    for start, end, key in reversed(replacements):
        markdown = markdown[:start] + f"[[CODE:{key}]]" + markdown[end:]
    # The validator forbids fence markers anywhere, including inline snippets.
    # Leave such unsupported Markdown untouched instead of rewriting prose.
    if any(marker in markdown for marker in ("```", "~~~")):
        return normalized, changes
    normalized["markdown"] = markdown
    for start, end, key in replacements:
        changes.append({"operation": "replace_exact_code_block", "start": start,
                        "end": end, "code_id": key, "message_id": codes[key]["message_id"]})
    actual_ids = list(dict.fromkeys(_PLACEHOLDER.findall(markdown)))
    if set(actual_ids) != set(declared):
        normalized["verbatim_code_ids"] = actual_ids
        changes.append({"operation": "derive_code_ids", "before": declared, "after": actual_ids})
    for key in actual_ids:
        source = codes[key]["message_id"]
        if source not in refs:
            # Only the existing immutable code map supplies an added citation.
            if source not in allowed_refs:
                continue
            refs.append(source)
            changes.append({"operation": "add_code_source_ref", "code_id": key, "message_id": source})
    return normalized, changes
