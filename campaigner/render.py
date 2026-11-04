"""Template rendering. Bodies are written as plain text with light formatting, then:
- email: light HTML that looks hand-typed in Outlook;
- LinkedIn: plain text (LinkedIn messages don't support formatting), with Unicode bold/italic and "•" bullets.

Formatting: **bold**, *italic* (or _italic_), lines starting "- " (bullets) or "1. " (numbered),
[text](https://...) links, and bare URLs. A blank line starts a new paragraph.
"""
from __future__ import annotations

import html
import json
import re
from typing import Callable

from jinja2 import Environment, StrictUndefined

ENV = Environment(undefined=StrictUndefined, autoescape=False)
# Outlook's default compose font, so mail looks hand-typed rather than templated.
WRAP = '<div style="font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt">{}</div>'
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>\"]+)")
TRAILING = ".,;:!?)'\""
CORE = ("email", "first_name", "last_name", "company", "title", "linkedin_url")
PIXEL = '<img src="{}" width="1" height="1" alt="" style="display:block;border:0;width:1px;height:1px">'


def lead_context(lead: dict) -> dict:
    ctx = dict(json.loads(lead.get("data") or "{}"))
    ctx.update({k: lead.get(k) or "" for k in CORE})
    ctx["name"] = f"{ctx['first_name']} {ctx['last_name']}".strip()
    return ctx


def render_text(template: str | None, ctx: dict) -> str:
    return ENV.from_string(template or "").render(ctx).strip()


BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w])")
BULLET_RE = re.compile(r"^\s*[-*•]\s+")
NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+")


def _inline(escaped: str) -> str:
    escaped = BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return ITALIC_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)


def _linkify(p: str, link: Callable[[str], str] | None) -> str:
    out, pos = [], 0
    for m in LINK_RE.finditer(p):
        text, url = (m.group(1), m.group(2)) if m.group(2) else (None, m.group(3))
        tail = ""
        if text is None:  # bare URL: don't swallow sentence punctuation
            while url and url[-1] in TRAILING:
                tail, url = url[-1] + tail, url[:-1]
            text = url
        href = link(url) if link else url
        out.append(_inline(html.escape(p[pos:m.start()])))
        out.append(f'<a href="{html.escape(href)}">{_inline(html.escape(text))}</a>{html.escape(tail)}')
        pos = m.end()
    out.append(_inline(html.escape(p[pos:])))
    return "".join(out)


LIST_STYLE = 'style="margin:0 0 10px 0;padding-left:22px"'


def text_to_html(text: str, link: Callable[[str], str] | None = None) -> str:
    out = []
    for para in (p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()):
        # Within a paragraph, runs of "- " / "1. " lines become lists; other lines stay as text with line breaks.
        runs: list[tuple[str, list[str]]] = []
        for line in para.strip().split("\n"):
            kind = "ul" if BULLET_RE.match(line) else "ol" if NUMBER_RE.match(line) else "p"
            if runs and runs[-1][0] == kind:
                runs[-1][1].append(line)
            else:
                runs.append((kind, [line]))
        for kind, lines in runs:
            if kind == "p":
                out.append("<p>" + "<br>".join(_linkify(l, link) for l in lines) + "</p>")
            else:
                strip = BULLET_RE if kind == "ul" else NUMBER_RE
                items = "".join(f"<li>{_linkify(strip.sub('', l, count=1), link)}</li>" for l in lines)
                out.append(f"<{kind} {LIST_STYLE}>{items}</{kind}>")
    return "".join(out)


def _styled(text: str, upper: int, lower: int, digits: int | None) -> str:
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(upper + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(lower + ord(ch) - 97))
        elif digits and "0" <= ch <= "9":
            out.append(chr(digits + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def linkedin_text(text: str) -> str:
    """LinkedIn messages are plain text: bold/italic become Unicode sans-serif bold/italic letters (as people
    do in posts), bullets become "•", and [text](url) becomes "text (url)". Screen readers may read the
    styled letters awkwardly, so use them sparingly."""
    lines = []
    for line in text.split("\n"):
        line = BULLET_RE.sub("• ", line, count=1)
        line = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", line)
        line = BOLD_RE.sub(lambda m: _styled(m.group(1), 0x1D5D4, 0x1D5EE, 0x1D7EC), line)
        line = ITALIC_RE.sub(lambda m: _styled(m.group(1) or m.group(2), 0x1D608, 0x1D622, None), line)
        lines.append(line)
    return "\n".join(lines)


def email_html(body: str, signature: str, ctx: dict, *, link: Callable[[str], str] | None = None,
               pixel: str | None = None) -> str:
    """link: rewrites each URL (click tracking). pixel: open-tracking image URL."""
    sig = render_text(signature, ctx)
    inner = text_to_html(render_text(body, ctx), link) + (text_to_html(sig, link) if sig else "")
    return WRAP.format(inner + (PIXEL.format(html.escape(pixel)) if pixel else ""))
