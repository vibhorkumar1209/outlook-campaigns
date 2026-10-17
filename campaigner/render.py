"""Template rendering. Bodies are written as plain text (blank line = new paragraph) and sent as light HTML.

Links: bare URLs, or [text](https://...) to show text instead of the address.
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
        out.append(html.escape(p[pos:m.start()]))
        out.append(f'<a href="{html.escape(href)}">{html.escape(text)}</a>{html.escape(tail)}')
        pos = m.end()
    out.append(html.escape(p[pos:]))
    return "".join(out)


def text_to_html(text: str, link: Callable[[str], str] | None = None) -> str:
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + _linkify(p.strip(), link).replace("\n", "<br>") + "</p>" for p in paras)


def email_html(body: str, signature: str, ctx: dict, *, link: Callable[[str], str] | None = None,
               pixel: str | None = None) -> str:
    """link: rewrites each URL (click tracking). pixel: open-tracking image URL."""
    sig = render_text(signature, ctx)
    inner = text_to_html(render_text(body, ctx), link) + (text_to_html(sig, link) if sig else "")
    return WRAP.format(inner + (PIXEL.format(html.escape(pixel)) if pixel else ""))
