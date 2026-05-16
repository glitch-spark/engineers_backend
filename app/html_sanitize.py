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
_BODY_INNER_RE = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", flags=re.IGNORECASE | re.DOTALL)


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
    without_styles = _STYLE_BLOCK_RE.sub("", html)
    m = _BODY_INNER_RE.search(without_styles)
    body = m.group(1) if m else without_styles
    return css.strip(), body.strip()


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
