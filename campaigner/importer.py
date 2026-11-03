"""CSV/Excel lead import: parse, guess column mapping, upsert leads, enroll into a campaign."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from urllib.parse import unquote

from .db import event, iso, one, utcnow

FIELDS = {
    "email": "Email",
    "first_name": "First name",
    "last_name": "Last name",
    "name": "Full name",
    "company": "Company",
    "title": "Title",
    "linkedin_url": "LinkedIn URL",
}
GUESS = {
    "email": ["email", "e_mail", "email_address", "work_email", "business_email", "mail"],
    "first_name": ["first_name", "firstname", "first", "given_name", "fname"],
    "last_name": ["last_name", "lastname", "last", "surname", "family_name", "lname"],
    "name": ["name", "full_name", "fullname", "contact_name", "contact", "person"],
    "company": ["company", "company_name", "organization", "organisation", "account", "account_name", "employer"],
    "title": ["title", "job_title", "position", "role", "designation", "headline"],
    "linkedin_url": ["linkedin", "linkedin_url", "linkedin_profile", "profile_url", "person_linkedin_url",
                     "li_url", "linkedin_profile_url"],
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LI_RE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.I)


def snake(k) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")


def read_table(filename: str, content: bytes) -> tuple[list[str], list[dict]]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it, None) or []]
        data = [dict(zip(header, r)) for r in it if any(v is not None for v in r)]
    else:
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        header = [h.strip() for h in reader.fieldnames or []]
        data = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
    header = [h for h in header if h]
    clean = [{h: ("" if r.get(h) is None else str(r.get(h)).strip()) for h in header} for r in data]
    return header, [r for r in clean if any(r.values())]


def guess_mapping(headers: list[str]) -> dict[str, str]:
    by_snake = {snake(h): h for h in headers}
    return {f: next((by_snake[g] for g in GUESS[f] if g in by_snake), "") for f in FIELDS}


def normalize_linkedin(url: str) -> str | None:
    m = LI_RE.search(url or "")
    return f"https://www.linkedin.com/in/{unquote(m.group(1)).strip().lower()}" if m else None


def _find(conn, email: str | None, li: str | None) -> dict | None:
    if email and (lead := one(conn, "SELECT * FROM leads WHERE email = ?", email)):
        return lead
    if li:
        return one(conn, "SELECT * FROM leads WHERE linkedin_url = ?", li)
    return None


def import_rows(conn, rows: list[dict], mapping: dict[str, str], campaign_id: int | None) -> dict:
    now = utcnow()
    summary = {"created": 0, "updated": 0, "enrolled": 0, "already_enrolled": 0, "skipped_rows": 0, "skipped": []}
    mapped = {v for v in mapping.values() if v}
    conn.execute("BEGIN")
    for n, r in enumerate(rows, start=2):
        val = {f: r.get(col, "").strip() if col else "" for f, col in mapping.items()}
        email = val["email"].lower() or None
        if email and not EMAIL_RE.match(email):
            summary["skipped"].append(f"row {n}: invalid email {email}")
            email = None
        li = normalize_linkedin(val["linkedin_url"])
        if val["linkedin_url"] and not li:
            summary["skipped"].append(f"row {n}: not a linkedin.com/in/ URL")
        if not email and not li:
            summary["skipped"].append(f"row {n}: no email or LinkedIn URL")
            summary["skipped_rows"] += 1
            continue
        first, last = val["first_name"], val["last_name"]
        if not first and val["name"]:
            first, _, rest = val["name"].partition(" ")
            last = last or rest.strip()
        extras = {snake(k): v for k, v in r.items() if k not in mapped and v}
        fields = {"first_name": first, "last_name": last, "company": val["company"], "title": val["title"]}

        lead = _find(conn, email, li)
        if lead:
            updates = {k: v for k, v in fields.items() if v}
            if email and not lead["email"]:
                updates["email"] = email
            if li and not lead["linkedin_url"]:
                updates["linkedin_url"] = li
            updates["data"] = json.dumps({**json.loads(lead["data"]), **extras})
            try:
                conn.execute(f"UPDATE leads SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                             (*updates.values(), lead["id"]))
            except sqlite3.IntegrityError:  # email/URL belongs to another lead; keep the rest
                updates.pop("email", None), updates.pop("linkedin_url", None)
                conn.execute(f"UPDATE leads SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                             (*updates.values(), lead["id"]))
            lead_id = lead["id"]
            summary["updated"] += 1
        else:
            try:
                cur = conn.execute(
                    "INSERT INTO leads (email, linkedin_url, first_name, last_name, company, title, data, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (email, li, first, last, val["company"], val["title"], json.dumps(extras), iso(now)))
            except sqlite3.IntegrityError:
                summary["skipped"].append(f"row {n}: email and LinkedIn URL belong to two different leads")
                summary["skipped_rows"] += 1
                continue
            lead_id = cur.lastrowid
            summary["created"] += 1

        if campaign_id:
            cur = conn.execute("INSERT OR IGNORE INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                               (campaign_id, lead_id, iso(now)))
            if cur.rowcount:
                e = one(conn, "SELECT * FROM enrollments WHERE id = ?", cur.lastrowid)
                event(conn, lead_id, "enrolled", enrollment=e, at=now)
                summary["enrolled"] += 1
            else:
                summary["already_enrolled"] += 1
    conn.execute("COMMIT")
    return summary
