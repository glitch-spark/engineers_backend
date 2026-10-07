"""Template / model-HTML sanitizer: XSS payloads are stripped, real resume templates come through unchanged."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app.html_sanitize import sanitize_template_html  # noqa: E402
from app.resume_service import _finalize_resume_html, _render_request_allowed  # noqa: E402

# A realistic uploaded template: full document, Google Fonts embed snippet, head + body <style>, inline SVG icons,
# a data: image, mailto/tel/https links, entities and text that merely looks like attributes.
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
  <title>Jane Doe — Resume</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&amp;family=Lora:ital@0;1&display=swap" rel="stylesheet">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto&display=swap');
    @page { size: A4; margin: 12mm 14mm; }
    :root { --accent: #1f6feb; }
    body { font-family: 'Inter', sans-serif; color: #222; }
    h1 > span.role::before { content: "\\2014  "; }
    a[href^="mailto:"] { color: var(--accent); }
    .skills li:not(:last-child)::after { content: " · "; }
    @media print { .no-print { display: none; } }
  </style>
</head>
<body class="page" data-theme="light">
  <style>.y{color:blue}</style>
  <header>
    <img class="avatar" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==" alt="Jane">
    <h1>Jane Doe <span class="role">Senior Engineer</span></h1>
    <p class="contact">
      <svg width="12" height="12" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 4h20v16H2z" fill="none" stroke="currentColor"/></svg>
      <a href="mailto:jane@example.com">jane@example.com</a> ·
      <a href="tel:+15551234567">+1 555 123 4567</a> ·
      <a href="https://github.com/janedoe" target="_blank" rel="noopener">github.com/janedoe</a>
    </p>
  </header>
  <section id="experience">
    <h2>Experience</h2>
    <div class="job"><h3>Acme &amp; Co</h3><span class="dates">2021 – now</span>
      <ul>
        <li>Cut p95 latency from 800ms -> 120ms; one = two is not an attribute, nor is on=off.</li>
        <li>Led the migration on AWS &lt;script&gt; text is just text.</li>
      </ul>
    </div>
  </section>
</body>
</html>"""


def test_real_template_is_unchanged():
    assert sanitize_template_html(TEMPLATE) == TEMPLATE.strip()


def test_sanitizer_is_idempotent():
    once = sanitize_template_html('<p><img/src=x/onerror=alert(1)>hi</p><scr<script>x</script>ipt>alert(1)</script>')
    assert sanitize_template_html(once) == once


@pytest.mark.parametrize(
    "payload",
    [
        "<img/src=x/onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        '<img src="x"onerror="alert(1)">',
        "<img src='x'onerror='alert(1)'>",
        "<body\nonload=alert(1)>",
        "<img src=x OnErRoR=alert(1)>",
        '<img alt="a>b" onerror=alert(1)>',  # a ">" inside a quoted value doesn't end the tag
        "<img a=b=\"  c='x\" >' onerror=alert(1)>",  # quote after an unquoted value: browser tokenizer rules
    ],
)
def test_event_handlers_stripped_after_any_separator(payload):
    out = sanitize_template_html(payload).lower()
    assert "onerror" not in out and "onload" not in out


@pytest.mark.parametrize(
    "payload",
    [
        "<scr<script>x</script>ipt>alert(1)</script>",
        "<scr<scr<script></script>ipt>ipt>alert(1)</script>",
        "<script>alert(1)</script>",
        "<SCRIPT src=//evil.example/x.js></SCRIPT>",
        "<script>alert(1)",
        "<noscript><p>shown when JS is off</p></noscript>",
    ],
)
def test_nested_script_cannot_be_rebuilt(payload):
    out = sanitize_template_html(payload).lower()
    assert "<script" not in out and "noscript" not in out


@pytest.mark.parametrize(
    "payload",
    [
        '<meta http-equiv="refresh" content="0;url=https://evil.example">',
        '<meta http-equiv="&#114;efresh" content="0;url=https://evil.example">',
        "<meta content=\"0;url=https://evil.example\" http-equiv=refresh>",
        '<meta content="a>b" http-equiv=refresh>',
        '<meta http-equiv="content-type" http-equiv="refresh" content="0;url=x">',
        '<meta http-equiv="set-cookie" content="a=b">',
    ],
)
def test_meta_refresh_removed(payload):
    assert "<meta" not in sanitize_template_html(payload).lower()


@pytest.mark.parametrize(
    "payload",
    [
        '<a href="javascript:alert(1)">x</a>',
        '<a href=" JaVaScRiPt:alert(1)">x</a>',
        '<a href="jav&#x09;ascript:alert(1)">x</a>',
        '<a href="&#106;&#97;vascript:alert(1)">x</a>',
        "<a href=javascript:alert(1)>x</a>",
        '<a href="vbscript:msgbox(1)">x</a>',
        '<a href="data:text/html,<script>alert(1)</script>">x</a>',
        '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
        '<button formaction="javascript:alert(1)">b</button>',
        "<svg><a><set attributeName=href to=javascript:alert(1) /></a></svg>",
        '<div srcdoc="<script>alert(1)</script>"></div>',
    ],
)
def test_script_urls_neutralized(payload):
    out = sanitize_template_html(payload).lower()
    assert "javascript:" not in out and "vbscript:" not in out and "data:text/html" not in out
    assert "srcdoc=\"<" not in out


@pytest.mark.parametrize(
    "payload",
    [
        '<iframe src="https://evil.example"></iframe>',
        "<iframe src=x>",
        "<frameset><frame src=x></frameset>",
        '<object data="x.swf"></object>',
        '<embed src="x.swf">',
        '<base href="https://evil.example/">',
        '<form action="https://evil.example"><input name=q></form>',
        '<link rel="stylesheet" href="https://evil.example/x.css">',
        '<link rel="stylesheet" href="https://fonts.googleapis.com@evil.example/x.css">',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter" onload="alert(1)">',
        '<link rel="import" href="https://fonts.googleapis.com/x">',
        '<link rel="stylesheet" href="http://fonts.googleapis.com/css2?family=Inter">',
    ],
)
def test_dangerous_tags_removed(payload):
    out = sanitize_template_html(payload).lower()
    for tag in ("<iframe", "<frame", "<object", "<embed", "<base", "<form", "<link"):
        assert tag not in out


def test_google_fonts_links_kept():
    for link in (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&amp;display=swap">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        "<link href='https://fonts.googleapis.com/css?family=Roboto' rel='stylesheet' type='text/css' />",
    ):
        assert sanitize_template_html(link) == link


def test_safe_urls_kept():
    html = '<a href="https://example.com/a?b=1&amp;c=2">x</a><img src="data:image/png;base64,AAAA">'
    assert sanitize_template_html(html) == html


def test_model_body_is_sanitized_into_the_resume():
    template = "<!DOCTYPE html><html><head><style>.x{color:red}</style></head><body><h1>Old</h1></body></html>"
    model = (
        "<body><h1>New</h1><img/src=x/onerror=alert(1)><script>alert(1)</script>"
        '<iframe src="http://169.254.169.254/"></iframe><a href="javascript:alert(1)">x</a></body>'
    )
    out = _finalize_resume_html(template, model).lower()
    assert "<h1>new</h1>" in out and ".x{color:red}" in out
    for bad in ("onerror", "<script", "<iframe", "javascript:"):
        assert bad not in out


@pytest.mark.parametrize(
    "url,ok",
    [
        ("data:image/png;base64,AAAA", True),
        ("about:blank", True),
        ("blob:null/1234", True),
        ("https://fonts.googleapis.com/css2?family=Inter", True),
        ("https://fonts.gstatic.com/s/inter/v12/x.woff2", True),
        ("http://fonts.googleapis.com/css2?family=Inter", False),
        ("https://fonts.googleapis.com:8443/css2", False),
        ("https://fonts.googleapis.com.evil.example/css2", False),
        ("http://169.254.169.254/latest/meta-data/", False),
        ("http://127.0.0.1:8080/", False),
        ("https://example.com/photo.jpg", False),
        ("file:///etc/passwd", False),
    ],
)
def test_pdf_render_network_allow_list(url, ok):
    assert _render_request_allowed(url) is ok
