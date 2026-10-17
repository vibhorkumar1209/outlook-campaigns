"""Template rendering. Bodies are written as plain text (blank line = new paragraph) and sent as light HTML."""
from __future__ import annotations

import html
import json
import re

from jinja2 import Environment, StrictUndefined

ENV = Environment(undefined=StrictUndefined, autoescape=False)
# Outlook's default compose font, so mail looks hand-typed rather than templated.
WRAP = '<div style="font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt">{}</div>'
URL_RE = re.compile(r"(https?://[^\s<]+)")
CORE = ("email", "first_name", "last_name", "company", "title", "linkedin_url")


def lead_context(lead: dict) -> dict:
    ctx = dict(json.loads(lead.get("data") or "{}"))
    ctx.update({k: lead.get(k) or "" for k in CORE})
    ctx["name"] = f"{ctx['first_name']} {ctx['last_name']}".strip()
    return ctx


def render_text(template: str | None, ctx: dict) -> str:
    return ENV.from_string(template or "").render(ctx).strip()


def text_to_html(text: str) -> str:
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    out = []
    for p in paras:
        p = URL_RE.sub(r'<a href="\1">\1</a>', html.escape(p.strip()))
        out.append("<p>" + p.replace("\n", "<br>") + "</p>")
    return "".join(out)


def email_html(body: str, signature: str, ctx: dict) -> str:
    sig = render_text(signature, ctx)
    return WRAP.format(text_to_html(render_text(body, ctx)) + (text_to_html(sig) if sig else ""))
