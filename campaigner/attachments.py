"""Files attached to email steps. Stored under data/attachments (git-ignored) and sent with every email of the step."""
from __future__ import annotations

import mimetypes
import re
import secrets
from pathlib import Path

from .db import DB_PATH

DIR = DB_PATH.parent / "attachments"
MAX_BYTES = 3 * 1024 * 1024        # Microsoft Graph's limit for a simple (non-upload-session) attachment
MAX_PER_EMAIL = 10 * 1024 * 1024   # keep emails reasonable; large files are better as links


class AttachmentError(ValueError):
    pass


def _safe(name: str) -> str:
    name = Path(name or "file").name
    return re.sub(r"[^\w.\- ()]+", "_", name)[:120] or "file"


def save(name: str, data: bytes) -> dict:
    if not data:
        raise AttachmentError("empty file")
    if len(data) > MAX_BYTES:
        raise AttachmentError(f"{name} is {len(data) / 1048576:.2f} MB; attachments are limited to 3 MB each. "
                              "For bigger files, put a link in the email instead.")
    DIR.mkdir(parents=True, exist_ok=True)
    aid = secrets.token_hex(6)
    clean = _safe(name)
    (DIR / f"{aid}__{clean}").write_bytes(data)
    return {"id": aid, "name": clean, "size": len(data),
            "type": mimetypes.guess_type(clean)[0] or "application/octet-stream"}


def path(meta: dict) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", meta.get("id", "")):
        raise AttachmentError("bad attachment id")
    return DIR / f"{meta['id']}__{_safe(meta.get('name', ''))}"


def load(metas: list[dict]) -> list[dict]:
    """[{name, type, data}] ready to send. Raises if a file has gone missing."""
    out, total = [], 0
    for m in metas:
        p = path(m)
        if not p.exists():
            raise AttachmentError(f"attachment {m.get('name')} is missing; re-upload it on the step")
        data = p.read_bytes()
        total += len(data)
        out.append({"name": m["name"], "type": m.get("type") or "application/octet-stream", "data": data})
    if total > MAX_PER_EMAIL:
        raise AttachmentError("attachments on this step add up to more than 10 MB")
    return out
