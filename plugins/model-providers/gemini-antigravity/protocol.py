"""AGE-11 application protocol, independent of the child and of tool execution."""

import json
import math
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

from providers.agy_policy import AgyError, PROMPT_LIMIT

ARG_LIMIT = 32 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _canonical_tool_call_id(value: object) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AgyError("protocol")
    if value.startswith("call_") and value != "call_":
        # Keep a stable ID for deduplication without passing a native prefix to Hermes.
        return "agy-" + value.removeprefix("call_")
    if value.startswith(("call_", "fc_", "bridge_", "toolu_", "chatcmpl-tool-")):
        raise AgyError("protocol")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _no_constant(_):
    raise ValueError


def _scan_depth(text):
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 32:
                raise ValueError
        elif char in "]}":
            depth -= 1


def strict_object(data):
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        _scan_depth(text)
        row = json.loads(text, object_pairs_hook=_pairs, parse_float=_finite, parse_constant=_no_constant)
        if not isinstance(row, dict):
            raise ValueError
        # JSON escapes can decode to lone surrogates (they are not Unicode scalars).
        encode(row)
        return row
    except (ValueError, TypeError, RecursionError):
        raise AgyError("protocol") from None


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _local_schema(schema):
    if isinstance(schema, list):
        return all(map(_local_schema, schema))
    if not isinstance(schema, dict):
        return True
    # A conservative subset: no references, custom dialects or regex evaluation.
    if {"$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties", "format"} & schema.keys():
        return False
    if schema.get("$schema", Draft202012Validator.META_SCHEMA["$id"]) != Draft202012Validator.META_SCHEMA["$id"]:
        return False
    return all(map(_local_schema, schema.values()))


def _tool_catalog(tools):
    catalog, validators, seen = [], {}, set()
    for tool in tools or ():
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            continue
        function = tool["function"]
        name, schema = function.get("name"), function.get("parameters")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            continue
        if name in seen:
            raise AgyError("unsupported_input")
        seen.add(name)
        if not isinstance(schema, dict):
            continue
        try:
            copy = strict_object(encode(schema))
            if not _local_schema(copy):
                continue
            Draft202012Validator.check_schema(copy)
        except (SchemaError, AgyError, ValueError, RecursionError, TypeError):
            continue
        validators[name] = Draft202012Validator(copy, registry=Registry())
        description = function.get("description")
        catalog.append({"name": name, "parameters": copy, "description": description if isinstance(description, str) else ""})
    return catalog, validators


def _history_call(call):
    fn = call["function"]
    args = fn["arguments"]
    if call.get("type") != "function" or not isinstance(call["id"], str) or not isinstance(fn["name"], str):
        raise AgyError("unsupported_input")
    if not isinstance(args, str) or len(args.encode("utf-8")) > ARG_LIMIT:
        raise AgyError("unsupported_input")
    return {"id": call["id"], "name": fn["name"], "arguments": strict_object(args)}


def _message(row):
    role, content = row["role"], row.get("content")
    if isinstance(content, list):
        if any(not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in content):
            raise AgyError("unsupported_input")
        content = "".join(p["text"] for p in content)
    if (role not in ("system", "developer", "user", "assistant", "tool")
            or (not isinstance(content, str) and not (role == "assistant" and content is None and row.get("tool_calls")))):
        raise AgyError("unsupported_input")
    if "\x00" in (content or ""):
        raise AgyError("unsupported_input")
    result = {"role": role, "content": content}
    if role == "assistant" and row.get("tool_calls"):
        result["tool_calls"] = [_history_call(call) for call in row["tool_calls"]]
    if role == "tool":
        if not isinstance(row.get("tool_call_id"), str):
            raise AgyError("unsupported_input")
        result["tool_call_id"] = row["tool_call_id"]
    return result


def pack_prompt(messages, tools, *, model):
    catalog, validators = _tool_catalog(tools)
    try:
        history = [_message(row) for row in messages]
        packed = encode({"messages": history, "tools": catalog, "model": model})
    except (KeyError, TypeError, ValueError, RecursionError):
        raise AgyError("unsupported_input") from None
    return packed, validators


def prepare(messages, tools, *, model):
    packed, validators = pack_prompt(messages, tools, model=model)
    if len(packed) > PROMPT_LIMIT:
        raise AgyError("input_too_large")
    return packed.decode("utf-8"), validators


def validate_result(row, validators):
    if (set(row) == {"type", "answer"} and row["type"] == "final"
            and isinstance(row["answer"], str) and row["answer"].strip()):
        return row
    if set(row) != {"type", "id", "name", "arguments"} or row["type"] != "tool_call":
        raise AgyError("protocol")
    call_id = _canonical_tool_call_id(row["id"])
    name, args = row["name"], row["arguments"]
    if (not isinstance(name, str) or name not in validators or not isinstance(args, dict)
            or len(encode(args)) > ARG_LIMIT or not validators[name].is_valid(args)):
        raise AgyError("protocol")
    return {**row, "id": call_id}
