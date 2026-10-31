"""A versioned output contract for new calls; old sent requests remain unchanged."""
from .verify import encoded, require

LEGACY_REVISION = "structured-note-v1"
REVISION = "structured-note-v2"


def structured_payload(stage, material, draft, *, forbid_literal_code=True, require_body=True):
    # Reuse the complete source/provenance prompts and deduplicated code transport.
    from .note_recovery import recovery_payload
    payload = recovery_payload(stage, material, draft)
    properties = {
        "title": {"type": "string", "description": "具体标题，不超过180字符"},
        "markdown": {"type": "string", "description": "以# 标题开头的完整正文；代码只用[[CODE:c编号]]引用原文"},
        "source_message_ids": {"type": "array", "items": {"type": "string", "enum": [m["id"] for m in material["messages"]]},
                               "description": "正文真正依赖的所有原文编号，至少一条个人材料，代码所属消息也必须列入"},
        "verbatim_code_ids": {"type": "array", "items": {"type": "string"},
                              "description": "仅正文实际出现的CODE编号；不需要代码则返回空数组"},
    }
    if material["codes"]:
        properties["verbatim_code_ids"]["items"]["enum"] = list(material["codes"])
    if forbid_literal_code:
        # A regular expression without lookarounds (supported by grammar decoders):
        # retain ordinary inline code while preventing literal backtick fences.
        # Verified source blocks are expanded from CODE aliases only after review.
        properties["markdown"]["pattern"] = r"^([^`]|`[^`]|``[^`])*`{0,2}$"
    if require_body:
        properties["markdown"]["pattern"] = r"^# [^\r\n`]+\r?\n[\r\n \t]*([^ \t\r\n`]|`[^`]|``[^`])([^`]|`[^`]|``[^`])*`{0,2}$"
    if stage == "review":
        properties.update(verdict={"type": "string", "enum": ["ready", "needs_review"]},
                          review_reason={"type": "string", "description": "独立对照原文后的一句具体复核理由"})
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    payload["response_format"] = {"type": "json_schema", "json_schema": {
        "name": "personal_note_" + stage, "strict": True, "schema": schema}}
    payload["messages"][0]["content"] += "\n输出必须符合接口附带的JSON Schema：" + encoded(schema).decode()
    from .notes import MAX_REQUEST
    require(sum(len(m["content"]) for m in payload["messages"]) <= MAX_REQUEST,
            "Structured request exceeds fixed context bound")
    return payload
