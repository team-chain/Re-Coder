"""Validate a complete code proposal before it can reach file review/apply."""
from __future__ import annotations

import json
import posixpath
import re


CODE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "ops": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "edit"]},
                    "file": {"type": "string", "minLength": 1},
                    "language": {"type": "string"},
                    "content": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["action", "file", "content"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "ops"],
    "additionalProperties": False,
}


class CodeOutputError(ValueError):
    """Safe, bounded explanation; never contains model text or project content."""


def parse_code_output(raw: str) -> tuple[dict, list[dict]]:
    text = (raw or "").strip()
    if not text:
        raise CodeOutputError("모델 응답이 비어 있습니다.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Permit prose around one complete object, never salvage fragments from
        # an unfinished outer object or a partially generated list of files.
        start = text.find("{")
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else (None, 0)
        except json.JSONDecodeError:
            raise CodeOutputError("파일 변경 JSON이 완성되지 않았거나 형식이 올바르지 않습니다.") from None
    # Older providers wrapped unparsed text in a successful-looking object.
    if isinstance(data, dict) and len(data) == 1 and ("raw_response" in data or "text" in data):
        wrapped = data.get("raw_response", data.get("text"))
        if isinstance(wrapped, str):
            # Unwrap once; do not recursively accept arbitrary provider envelopes.
            try:
                data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", wrapped.strip(), flags=re.IGNORECASE))
            except json.JSONDecodeError:
                raise CodeOutputError("파일 변경 JSON이 완성되지 않았거나 형식이 올바르지 않습니다.") from None
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list) or not data["ops"]:
        raise CodeOutputError("모델이 적용 가능한 파일 변경 목록을 반환하지 않았습니다.")
    ops, seen = [], set()
    for index, op in enumerate(data["ops"], 1):
        if not isinstance(op, dict) or not isinstance(op.get("file"), str) or not isinstance(op.get("content"), str):
            raise CodeOutputError(f"파일 변경 {index}번에 경로 또는 전체 코드가 없습니다.")
        file = op["file"].strip().replace("\\", "/")
        if not file or file.startswith("/") or ":" in file:
            raise CodeOutputError(f"파일 변경 {index}번의 경로가 올바른 프로젝트 상대경로가 아닙니다.")
        file = posixpath.normpath(file)
        if file == ".." or file.startswith("../"):
            raise CodeOutputError(f"파일 변경 {index}번의 경로가 프로젝트 밖을 가리킵니다.")
        if file == "." or file.casefold() in seen:
            raise CodeOutputError(f"파일 변경 {index}번의 경로가 비어 있거나 중복됩니다.")
        seen.add(file.casefold())
        action = op.get("action", "create")
        if not isinstance(action, str) or action.strip().lower() not in {"create", "edit", "update"}:
            raise CodeOutputError(f"파일 변경 {index}번의 작업 종류가 올바르지 않습니다.")
        ops.append({
            "action": "edit" if action.strip().lower() in {"edit", "update"} else "create",
            "file": file, "content": op["content"],
            "language": op.get("language", "") if isinstance(op.get("language", ""), str) else "",
            "rationale": op.get("rationale", "") if isinstance(op.get("rationale", ""), str) else "",
        })
    return data, ops
