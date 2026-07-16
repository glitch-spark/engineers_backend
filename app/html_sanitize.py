"""HTML sanitization for user-uploaded resume templates.

Strips XSS vectors while keeping the visual layout. Allows everything needed
for a static resume: inline styles, classes, semantic tags, external images,
Google Fonts <link>. Blocks: <script>, <iframe>, <object>, <embed>, <form>,
event handlers (onclick=…), and javascript: URLs.
"""

from __future__ import annotations

import re

# Block these tags entirely (content + tag). re.DOTALL matches multi-line bodies.
_BLOCK_TAGS = ("script", "iframe", "object", "embed", "form", "noscript", "applet")

_BLOCK_TAG_RE = re.compile(
    r"<(?P<tag>" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>.*?</(?P=tag)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
# Self-closing variants
_BLOCK_TAG_SELF_RE = re.compile(
    r"<(" + "|".join(_BLOCK_TAGS) + r")\b[^>]*/?>",
    flags=re.IGNORECASE,
)

# Inline event handlers: onclick=, onerror=, etc.
_EVENT_HANDLER_RE = re.compile(r"\s+on[a-z]+\s*=\s*\"[^\"]*\"", flags=re.IGNORECASE)
_EVENT_HANDLER_RE_SQ = re.compile(r"\s+on[a-z]+\s*=\s*'[^']*'", flags=re.IGNORECASE)
_EVENT_HANDLER_RE_UQ = re.compile(r"\s+on[a-z]+\s*=\s*[^\s>]+", flags=re.IGNORECASE)

# javascript: / data:text/html URIs — match the whole attribute value so the
# payload after the colon doesn't leak out.
_BAD_URI_DQ = re.compile(
    r"(href|src)\s*=\s*\"\s*(?:javascript:|data:text/html)[^\"]*\"",
    flags=re.IGNORECASE,
)
_BAD_URI_SQ = re.compile(
    r"(href|src)\s*=\s*'\s*(?:javascript:|data:text/html)[^']*'",
    flags=re.IGNORECASE,
)
_BAD_URI_UQ = re.compile(
    r"(href|src)\s*=\s*(?:javascript:|data:text/html)[^\s>]*",
    flags=re.IGNORECASE,
)


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


def sanitize_template_html(html: str) -> str:
    if not html:
        return ""
    out = html
    out = _BLOCK_TAG_RE.sub("", out)
    out = _BLOCK_TAG_SELF_RE.sub("", out)
    out = _EVENT_HANDLER_RE.sub("", out)
    out = _EVENT_HANDLER_RE_SQ.sub("", out)
    out = _EVENT_HANDLER_RE_UQ.sub("", out)
    # Neutralize javascript: + data:text/html URIs by rewriting to "#".
    out = _BAD_URI_DQ.sub(r'\1="#"', out)
    out = _BAD_URI_SQ.sub(r"\1='#'", out)
    out = _BAD_URI_UQ.sub(r'\1=#', out)
    return out.strip()
