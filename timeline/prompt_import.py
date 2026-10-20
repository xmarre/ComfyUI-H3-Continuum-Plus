"""Strict, import-only prompt parsing; native sampler fallback is unchanged."""

from __future__ import annotations

import json
import re
from fractions import Fraction

from .model import rational

_CLOCK = r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?"
_TIME = re.compile(
    rf"^\s*\[?\s*(?P<a>{_CLOCK})\s*(?:seconds?|secs?|s)?\s*(?:[-–—]|\bto\b)\s*(?P<b>{_CLOCK})\s*(?:seconds?|secs?|s)?\s*\]?\s*:?\s*$",
    re.IGNORECASE,
)
_CHUNK = re.compile(
    r"^\s*(?:#{1,6}\s*)?\[?\s*(?:chunk|clip)\s+(\d+)\s*\]?\s*:?\s*$", re.IGNORECASE
)
# Classify only explicit header syntax. Numeric prose and tag lists are prompts.
_LIKE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\[\s*(?:(?:chunk|clip)\b|\d[\w:.]*\s*(?:s(?:ec(?:ond)?s?)?\s*)?(?:[-–—]|\bto\b))|(?:chunk|clip)\s+\S+\s*:|\d[\d:.]*\s*(?:s\s*)?(?:[-–—]|\bto\b)[^\n]*:)",
    re.IGNORECASE,
)


def header(line):
    """Return (time match, chunk match, inline body), or no header."""
    value = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            return None, None, ""
        head, body = (
            value[: close + 1],
            value[close + 1 :].lstrip().removeprefix(":").lstrip(),
        )
    else:
        # A colon after the complete interval/ordinal separates inline text;
        # clock colons are consumed as part of the interval.
        match = re.match(
            rf"^((?:{_CLOCK}\s*(?:seconds?|secs?|s)?\s*(?:[-–—]|\bto\b)\s*{_CLOCK}\s*(?:seconds?|secs?|s)?|(?:chunk|clip)\s+\d+))\s*:\s*(.*)$",
            value,
            re.IGNORECASE,
        )
        head, body = (match[1], match[2]) if match else (value, "")
    timed, chunk = _TIME.fullmatch(head), _CHUNK.fullmatch(head)
    return timed, chunk, body


def clock(text):
    parts = text.split(":")
    if len(parts) > 1 and any(Fraction(x) >= 60 for x in parts[1:]):
        raise ValueError("Clock minutes and seconds must be below 60")
    value = Fraction(0)
    for part in parts:
        value = value * 60 + Fraction(part)
    return value


def parse_prompt_source(source):
    """Return global text and ordered ordinal/timed entries without guessing."""
    if isinstance(source, str):
        text = source.lstrip("\ufeff").strip()
        if text.startswith(("{", "[")):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                if text.startswith("{"):
                    raise ValueError("Invalid JSON prompt document")
            else:
                return parse_prompt_source(value)
        lines = text.splitlines()
        headers = []
        for n, line in enumerate(lines):
            timed, chunk, inline = header(line)
            if timed:
                try:
                    a, b = clock(timed["a"]), clock(timed["b"])
                    if b <= a:
                        raise ValueError("End must be after start")
                except ValueError as exc:
                    raise ValueError(f"Line {n + 1}: {line}\n{exc}") from exc
                headers.append(
                    (n, {"start_seconds": str(a), "end_seconds": str(b)}, inline)
                )
            elif chunk:
                index = int(chunk[1])
                if index < 1:
                    raise ValueError(f"Line {n + 1}: chunk numbers start at 1: {line}")
                headers.append((n, {"chunk": index}, inline))
            elif _LIKE.match(line):
                raise ValueError(
                    f"Line {n + 1}: "
                    + (
                        "unbalanced"
                        if line.lstrip().startswith("[") and "]" not in line
                        else "unsupported or malformed"
                    )
                    + f" prompt header: {line}"
                )
        if headers:
            entries = []
            for i, (n, entry, inline) in enumerate(headers):
                stop = headers[i + 1][0] if i + 1 < len(headers) else len(lines)
                entries.append(
                    {
                        **entry,
                        "text": "\n".join(
                            ([inline] if inline else []) + lines[n + 1 : stop]
                        ).strip(),
                        "line": n + 1,
                    }
                )
            result = {
                "global": "\n".join(lines[: headers[0][0]]).strip(),
                "global_present": bool(headers[0][0]),
                "entries": entries,
                "format": "headers",
            }
        elif re.search(r"^\s*---+\s*$", text, re.MULTILINE):
            values = re.split(r"^\s*---+\s*$", text, flags=re.MULTILINE)
            result = {
                "global": "",
                "global_present": False,
                "entries": [
                    {"chunk": i + 1, "text": v.strip()} for i, v in enumerate(values)
                ],
                "format": "list",
            }
        else:
            result = {
                "global": text,
                "global_present": True,
                "entries": [],
                "format": "fixed",
            }
    elif isinstance(source, list):
        if not all(isinstance(x, (str, dict)) for x in source):
            raise ValueError("JSON prompt list entries must be strings or objects")
        result = {
            "global": "",
            "global_present": False,
            "entries": [_entry(x, i) for i, x in enumerate(source)],
            "format": "json-list",
        }
    elif isinstance(source, dict):
        global_key = next(
            (k for k in ("global_prefix", "global", "global_prompt") if k in source),
            None,
        )
        global_text = source.get(global_key, "") if global_key else ""
        if not isinstance(global_text, str):
            raise ValueError("Global prompt must be text")
        chunks = source.get("segments", source.get("chunks", []))
        if isinstance(chunks, dict):
            try:
                chunks = [
                    {"chunk": int(k), "text": v}
                    if isinstance(v, str)
                    else {"chunk": int(k), **v}
                    for k, v in sorted(chunks.items(), key=lambda x: int(x[0]))
                ]
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "Chunk object keys must be positive chunk numbers"
                ) from exc
        if not isinstance(chunks, list):
            raise ValueError("segments/chunks must be an array or numbered object")
        if not global_key and "segments" not in source and "chunks" not in source:
            raise ValueError("JSON document has no global, chunks or segments")
        result = {
            "global": global_text,
            "global_present": global_key is not None,
            "entries": [_entry(x, i) for i, x in enumerate(chunks)],
            "format": "manifest" if "segments" in source else "json-object",
        }
        if "audio_filename" in source:
            result["audio_filename"] = source["audio_filename"]
    else:
        raise ValueError("Prompt source must be text, a JSON array or a prompt object")
    if len(result["entries"]) > 65536:
        raise ValueError("Prompt import contains too many entries")
    seen = set()
    for entry in result["entries"]:
        key = (
            ("time", entry["start_seconds"], entry["end_seconds"])
            if "start_seconds" in entry
            else ("chunk", entry["chunk"])
        )
        if key in seen:
            raise ValueError(f"Duplicate prompt interval or chunk: {key}")
        seen.add(key)
    timed = sorted(
        (rational(e["start_seconds"]), rational(e["end_seconds"]))
        for e in result["entries"]
        if "start_seconds" in e
    )
    if any(a < previous_b for (_, previous_b), (a, _) in zip(timed, timed[1:])):
        raise ValueError("Imported timed prompt ranges overlap")
    return result


def _entry(value, index):
    if isinstance(value, str):
        return {"chunk": index + 1, "text": value}
    if not isinstance(value, dict):
        raise ValueError("Prompt entry must be text or an object")
    text = value.get("prompt", value.get("text", ""))
    if not isinstance(text, str):
        raise ValueError("Chunk prompt must be text")
    entry = {"text": text}
    if "start_seconds" in value or "end_seconds" in value:
        try:
            a, b = rational(value["start_seconds"]), rational(value["end_seconds"])
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise ValueError(
                "Timed entries need valid start_seconds and end_seconds"
            ) from exc
        if a < 0 or b <= a:
            raise ValueError("Timed entry must satisfy 0 <= start < end")
        if "duration_seconds" in value and rational(value["duration_seconds"]) != b - a:
            raise ValueError("duration_seconds disagrees with the timed interval")
        entry.update(start_seconds=str(a), end_seconds=str(b))
    else:
        n = value.get("chunk", value.get("index", index + 1))
        if type(n) is not int or n < 1:
            raise ValueError("Chunk number must be a positive integer")
        entry["chunk"] = n
    if value.get("filename") is not None:
        entry["filename"] = value["filename"]
    return entry


def connected_import_warning(text):
    """Keep native fallback permissive, but make extended import syntax visible."""
    from ..constants import PROMPT_MODE_TIMELINE
    from ..v2.prompts import detect_prompt_mode

    try:
        document = parse_prompt_source(text)
    except (ValueError, TypeError) as exc:
        return f"Connected prompt import syntax needs attention: {exc}. Use Import to inspect and adopt it."
    if (
        document["entries"]
        and document["format"] == "headers"
        and detect_prompt_mode(text) != PROMPT_MODE_TIMELINE
    ):
        return "Connected prompt headers are not recognized by the native sampler and may use Fixed fallback. Use Import → Read connected Text Box to preview and adopt the timed/chunk mapping."
    return ""
