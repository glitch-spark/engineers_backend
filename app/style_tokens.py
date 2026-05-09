"""Mirror of engineersapp/src/lib/styleTokens.ts.

Bidirectional bridge between the structured-mode `styleConfig` dict and the
markdown-mode tagged markdown. Markdown is the canonical source — backend
parses tokens, runs the structured serializer to ship strict LLM-contract
prose, then appends any free-prose user notes verbatim.

Token grammar: `<!-- @style key.path=value -->` (one per line).
Free prose under `## User notes` is preserved across mode toggles.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

_TOKEN_RE = re.compile(r"<!--\s*@style\s+([\w.]+)\s*=\s*([^>]+?)\s*-->")
_HEADER_RE = re.compile(
    r"<!--\s*This file is generated from the Structured editor[^>]*-->",
    re.IGNORECASE,
)
_NOTES_HEADING = "## User notes"
_HEADER_COMMENT = (
    '<!-- This file is generated from the Structured editor. Edit tokens '
    'directly to round-trip; free prose under "## User notes" is preserved. -->'
)


def _parse_value(raw: str) -> Any:
    v = raw.strip()
    if v == "true":
        return True
    if v == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    if (v.startswith("[") and v.endswith("]")) or (v.startswith("{") and v.endswith("}")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    return v


def _unflatten(flat: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for path, raw in flat.items():
        keys = path.split(".")
        cur: Dict[str, Any] = out
        for k in keys[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = raw
    return out


def _flatten(obj: Any, prefix: str = "") -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(v, dict):
            out.update(_flatten(v, key))
        elif v is not None:
            out[key] = str(v).lower() if isinstance(v, bool) else str(v)
    return out


def _deep_merge(base: Any, override: Any) -> Any:
    if override is None:
        return base
    if isinstance(override, list):
        return override
    if not isinstance(override, dict):
        return override
    if not isinstance(base, dict):
        return override
    out: Dict[str, Any] = dict(base)
    for k, v in override.items():
        out[k] = _deep_merge(out.get(k), v)
    return out


_NOTES_HEADING_RE = re.compile(r"(?m)^##\s+User notes\s*$")


def extract_notes(md: str) -> str:
    if not md:
        return ""
    m = _NOTES_HEADING_RE.search(md)
    if not m:
        return ""
    return md[m.end():].strip()


def strip_tokens(md: str) -> str:
    """Drop @style tokens + auto-header. Used to expose user-authored prose."""
    cleaned = _TOKEN_RE.sub("", md or "")
    cleaned = _HEADER_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def parse_tagged_markdown(md: str) -> Tuple[Dict[str, Any], str, int]:
    """Returns (config_dict, notes, token_count). Untagged markdown returns
    (empty config, notes-or-prose, 0)."""
    flat: Dict[str, Any] = {}
    for m in _TOKEN_RE.finditer(md or ""):
        flat[m.group(1)] = _parse_value(m.group(2))
    cfg = _unflatten(flat)
    return cfg, extract_notes(md or ""), len(flat)


def serialize_to_tagged_markdown(cfg: Dict[str, Any], prev_md: str = "") -> str:
    """Build tagged markdown from structured config. Preserves user notes from
    prev_md if present."""
    flat = _flatten(cfg)
    token_lines = [f"<!-- @style {k}={v} -->" for k, v in sorted(flat.items())]
    notes = extract_notes(prev_md)
    out = [_HEADER_COMMENT, "", *token_lines]
    if notes:
        out.extend(["", _NOTES_HEADING, "", notes])
    return "\n".join(out)


def deep_merge(base: Any, override: Any) -> Any:
    return _deep_merge(base, override)


def free_prose(md: str) -> str:
    """Return user prose with tokens, header, and notes-heading marker stripped
    BUT notes content kept. Used to harvest free-form style guidance the user
    wrote alongside (or under) the tokens, to append to the LLM prompt."""
    if not md:
        return ""
    body = strip_tokens(md)
    # Drop the notes-heading line itself; keep the prose under it.
    body = re.sub(r"^\s*##\s+User notes\s*", "", body, flags=re.MULTILINE).strip()
    return body
