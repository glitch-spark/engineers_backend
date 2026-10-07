"""HTML sanitization for user-uploaded resume templates (and the model's
rewritten resume body).

Strips XSS vectors while keeping the visual layout. Allows everything needed
for a static resume: <style> blocks, inline styles, classes, semantic tags,
images, Google Fonts <link>. Blocks: <script>, frames, <object>/<embed>,
<form>, <base>, <meta http-equiv> (refresh), any other <link>, event handlers
(onclick=… after any separator: space, "/", quote), and javascript: /
vbscript: / non-image data: URLs. Templates are whole documents, so this is
pattern-based rather than a DOM allow-list; it repeats until nothing changes
so removing one tag can't splice a new one together.
"""

from __future__ import annotations

import html as html_lib
import re

# Block these tags entirely (content + tag). re.DOTALL matches multi-line bodies.
_BLOCK_TAGS = (
    "script", "iframe", "frame", "frameset", "object", "embed", "form", "noscript", "noembed", "noframes",
    "applet", "portal",
)

_BLOCK_TAG_RE = re.compile(
    r"<(?P<tag>" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>.*?</(?P=tag)\b[^>]*>",
    flags=re.IGNORECASE | re.DOTALL,
)
# Unpaired opening / closing / self-closing variants are dropped too, plus <base> (rebases every relative URL).
_DROP_TAGS = frozenset(_BLOCK_TAGS + ("base",))

# Where a tag starts, and its name; the tag's end is found by _tag_end.
_TAG_START_RE = re.compile(r"</?[a-zA-Z]")
# "<" isn't part of the name here, so "<scr<script>" still shows the inner <script.
_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][^\s/><]*)")
# Removing a tag: up to its first ">" (what's left of a longer tag turns into plain text).
_TAG_HEAD_RE = re.compile(r"[^>]*>?")
_SCAN_LIMIT = 2048  # a <meta>/<link> longer than this is dropped rather than read
# Tokenizer pieces for _tag_end.
_WS = " \t\n\r\f"
_TAG_NAME_END_RE = re.compile(r"/?[^\s/>]*")
_ATTR_NAME_RE = re.compile(r"[^\s/>=]*")
_UNQUOTED_RE = re.compile(r"[^\s>]*")

# An attribute value as the browser reads it: quoted, or unquoted up to whitespace / ">".
_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s>]*)"
# An attribute starts after whitespace, "/" (<svg/onload=…>) or the quote ending the previous value.
_ATTR_START = r"(?<=[\s/\"'])"

# Inline event handlers: onclick=, onerror=, … whatever separator precedes them.
_EVENT_HANDLER_RE = re.compile(_ATTR_START + r"on[a-z0-9_-]+\s*=\s*" + _VALUE, flags=re.IGNORECASE)

# Any attribute: its decoded value is checked for script / document URLs.
_ATTR_RE = re.compile(
    _ATTR_START + r"(?P<name>[^\s/>\"'=]+)(?P<eq>\s*=\s*)(?P<value>" + _VALUE + r")",
    flags=re.IGNORECASE,
)
_URL_ATTRS = frozenset({
    "href", "xlink:href", "src", "action", "formaction", "data", "poster", "background", "ping", "lowsrc",
    "dynsrc", "codebase", "cite", "longdesc", "manifest", "icon",
})
_SCRIPT_SCHEME = re.compile(r"(?:java|vb)script:", flags=re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x20\x7f]+")

_HTTP_EQUIV_RE = re.compile(r"http-equiv\s*=\s*(" + _VALUE + r")", flags=re.IGNORECASE)
_SAFE_HTTP_EQUIV = frozenset({"content-type", "x-ua-compatible"})

# A <link> survives only as Google Fonts: every attribute is one of these, href is on a Google Fonts host with
# nothing after it but a path (so no "@other.host"), rel is stylesheet or a connection hint.
_GF_HREF = r"https://fonts\.(?:googleapis|gstatic)\.com"
_LINK_REL = r"(?:stylesheet|preconnect|dns-prefetch|preload)"
_LINK_ATTR = (
    r"\s+(?:"
    r"rel\s*=\s*(?:\"" + _LINK_REL + r"\"|'" + _LINK_REL + r"'|" + _LINK_REL + r"(?=[\s/>]))"
    r"|href\s*=\s*(?:\"" + _GF_HREF + r"(?:/[^\"<>]*)?\"|'" + _GF_HREF + r"(?:/[^'<>]*)?')"
    r"|crossorigin(?:\s*=\s*(?:\"[\w-]*\"|'[\w-]*'|[\w-]+))?"
    r"|(?:type|media|as)\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'|[\w/-]+)"
    r")"
)
_FONTS_LINK_RE = re.compile(r"<link(?:" + _LINK_ATTR + r")+\s*/?>", flags=re.IGNORECASE)
_LINK_HREF_RE = re.compile(r"\shref\s*=", flags=re.IGNORECASE)

_MAX_PASSES = 50


_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", flags=re.IGNORECASE | re.DOTALL)
# Full tag match (group 0) for re-injection; group 1 is CSS body.
_STYLE_TAG_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", flags=re.IGNORECASE | re.DOTALL)
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", flags=re.IGNORECASE)
_BODY_INNER_RE = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", flags=re.IGNORECASE | re.DOTALL)


def strip_style_tags(html: str) -> str:
    """Remove every <style>…</style> block from html."""
    if not html:
        return ""
    return _STYLE_TAG_RE.sub("", html)


def iter_style_tags(html: str) -> list[str]:
    """Return full original <style…>…</style> strings in document order."""
    if not html:
        return []
    return [m.group(0) for m in _STYLE_TAG_RE.finditer(html)]


def body_styles_vs_head(html: str) -> tuple[list[str], list[str]]:
    """Split style tags into (body_styles, head_styles) by position vs first <body>.

    Tags whose start index is at/after the first <body…> opening tag are body
    styles; earlier tags are treated as head/shell styles.
    """
    if not html:
        return [], []
    body_m = _BODY_OPEN_RE.search(html)
    body_start = body_m.start() if body_m else len(html)
    head_styles: list[str] = []
    body_styles: list[str] = []
    for m in _STYLE_TAG_RE.finditer(html):
        if m.start() >= body_start:
            body_styles.append(m.group(0))
        else:
            head_styles.append(m.group(0))
    return body_styles, head_styles


def body_html_for_llm(template: str) -> str:
    """Body inner HTML with all <style> blocks removed — safe LLM input.

    Uses the first <body> region when present; otherwise the whole document
    minus style tags. CSS never enters the model context.
    """
    if not template:
        return ""
    without_styles = strip_style_tags(template)
    m = _BODY_INNER_RE.search(without_styles)
    body = m.group(1) if m else without_styles
    return body.strip()


def wrap_body_for_llm(body_inner: str) -> str:
    """Wrap style-stripped body inner HTML as a <body>…</body> fragment."""
    return f"<body>{body_inner or ''}</body>"


def split_template(html: str) -> tuple[str, str]:
    """Split a sanitized HTML template into (css_text, body_inner_html).

    - css_text concatenates the content of every <style> block (head or body),
      preserving order. Used to inject byte-for-byte into the final document.
    - body_inner_html is the inside of the first <body>; if no <body>, falls
      back to the whole document (minus the extracted <style> blocks).
    The LLM only sees body_inner_html as the structural reference; css_text
    bypasses the LLM entirely so it can't paraphrase the CSS."""
    if not html:
        return "", ""
    css = "\n".join(m.group(1).strip() for m in _STYLE_BLOCK_RE.finditer(html))
    return css.strip(), body_html_for_llm(html)


def _attr_value(raw: str) -> str:
    """An attribute value as the browser sees it: unquoted, entities decoded."""
    if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
        raw = raw[1:-1]
    return html_lib.unescape(raw)


def _dangerous_url(name: str, value: str) -> bool:
    """javascript:/vbscript: in any attribute (SVG <set to=…>/<animate values=…> included), a data: URL other
    than an image in a URL attribute, and any srcdoc. Browsers ignore tabs/newlines inside a URL, so control
    characters and spaces are dropped before checking."""
    name = name.lower()
    if name == "srcdoc":
        return True
    flat = _CONTROL.sub("", value).lower()
    if _SCRIPT_SCHEME.search(flat):
        return True
    return name in _URL_ATTRS and flat.startswith("data:") and not flat.startswith("data:image/")


def _clean_attr(m: re.Match) -> str:
    if not _dangerous_url(m.group("name"), _attr_value(m.group("value"))):
        return m.group(0)
    return f'{m.group("name")}{m.group("eq")}"#"'


def _tag_end(html: str, start: int, limit: int | None = None) -> int | None:
    """Index just past the ">" that closes the tag opening at `start`, read with the browser's tokenizer states:
    a quote opens a value only right after `name=`, and a ">" inside a quoted value doesn't close the tag.
    None when the tag doesn't close (within `limit` characters)."""
    n = len(html) if limit is None else min(len(html), start + limit)
    j = _TAG_NAME_END_RE.match(html, start + 1, n).end()
    after_name = False  # else: before an attribute name (also after a quoted value or "/")
    while j < n:
        c = html[j]
        if c == ">":
            return j + 1
        if c in _WS:
            j += 1
        elif c == "/":
            after_name = False
            j += 1
        elif after_name and c == "=":
            j += 1
            while j < n and html[j] in _WS:
                j += 1
            if j < n and html[j] in "\"'":
                close = html.find(html[j], j + 1, n)
                if close < 0:
                    return None
                j = close + 1
            else:
                j = _UNQUOTED_RE.match(html, j, n).end()
            after_name = False
        else:  # an attribute name starts (its first character may be "=")
            j = _ATTR_NAME_RE.match(html, j + 1, n).end()
            after_name = True
    return None


def _meta_ok(tag: str) -> bool:
    """<meta http-equiv=…> (refresh redirects, set-cookie…) only stays when every http-equiv is harmless."""
    if "http-equiv" not in html_lib.unescape(tag).lower():
        return True
    values = [_attr_value(v).strip().lower() for v in _HTTP_EQUIV_RE.findall(tag)]
    return bool(values) and all(v in _SAFE_HTTP_EQUIV for v in values)


def _keep_tag(html: str, start: int, name: str) -> bool:
    if name in _DROP_TAGS:
        return False
    if name not in ("meta", "link") or html[start + 1] == "/":
        return True
    end = _tag_end(html, start, _SCAN_LIMIT)
    if end is None:
        return False
    tag = html[start:end]
    if name == "meta":
        return _meta_ok(tag)
    return bool(_FONTS_LINK_RE.fullmatch(tag)) and bool(_LINK_HREF_RE.search(tag))


def _drop_tags(html: str) -> str:
    """Remove dropped tags, unsafe <meta>s and non-Google-Fonts <link>s wherever a tag could start, including
    inside another tag (a browser may read that spot as markup, e.g. after a </style>)."""
    cut: list[tuple[int, int]] = []
    for m in _TAG_NAME_RE.finditer(html):
        if _keep_tag(html, m.start(), m.group(1).lower()):
            continue
        end = _TAG_HEAD_RE.match(html, m.end()).end()
        if cut and m.start() <= cut[-1][1]:
            cut[-1] = (cut[-1][0], max(cut[-1][1], end))
        else:
            cut.append((m.start(), end))
    if not cut:
        return html
    parts, pos = [], 0
    for a, b in cut:
        parts.append(html[pos:a])
        pos = b
    parts.append(html[pos:])
    return "".join(parts)


def _clean_tag(tag: str) -> str:
    tag = _EVENT_HANDLER_RE.sub("", tag)
    # Neutralize javascript: / vbscript: / data:text/html URIs by rewriting the value to "#".
    return _ATTR_RE.sub(_clean_attr, tag)


def _clean_attrs(html: str) -> str:
    """Strip handlers and script URLs inside every tag (text between tags is left alone)."""
    parts, pos = [], 0
    while (m := _TAG_START_RE.search(html, pos)) is not None:
        end = _tag_end(html, m.start()) or len(html)
        parts.append(html[pos:m.start()])
        parts.append(_clean_tag(html[m.start():end]))
        pos = end
    parts.append(html[pos:])
    return "".join(parts)


def _sanitize_pass(out: str) -> str:
    out = _BLOCK_TAG_RE.sub("", out)
    out = _drop_tags(out)
    return _clean_attrs(out)


def sanitize_template_html(html: str) -> str:
    if not html:
        return ""
    out = html
    for _ in range(_MAX_PASSES):
        cleaned = _sanitize_pass(out)
        if cleaned == out:
            return out.strip()
        out = cleaned
    # Still changing after many passes (crafted nesting): keep it as text rather than half-cleaned markup.
    return html_lib.escape(out).strip()
