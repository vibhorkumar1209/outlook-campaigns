"""CSV/Excel lead import: parse, guess column mapping, upsert leads, enroll into a campaign."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from urllib.parse import unquote

from . import engine
from .db import event, iso, one, utcnow

FIELDS = {
    "email": "Email",
    "first_name": "First name",
    "last_name": "Last name",
    "name": "Full name",
    "company": "Company",
    "title": "Title",
    "linkedin_url": "LinkedIn URL",
    # Optional history, so leads you've already worked outside the app start at the right step.
    "lead_status": "History: lead status",
    "li_status": "History: LinkedIn status",
    "li_msg_1": "History: LinkedIn message 1 date",
    "li_msg_2": "History: LinkedIn message 2 date",
    "email_1": "History: email 1 date",
    "email_2": "History: email 2 date",
}
HISTORY = ("lead_status", "li_status", "li_msg_1", "li_msg_2", "email_1", "email_2")
GUESS = {
    "email": ["email", "e_mail", "email_address", "work_email", "business_email", "mail"],
    "first_name": ["first_name", "firstname", "first", "given_name", "fname"],
    "last_name": ["last_name", "lastname", "last", "surname", "family_name", "lname"],
    "name": ["name", "full_name", "fullname", "contact_name", "contact", "person"],
    "company": ["company", "company_name", "organization", "organisation", "account", "account_name", "employer"],
    "title": ["title", "job_title", "position", "role", "designation", "headline"],
    "lead_status": ["lead_status"],
    "li_status": ["li_status", "linkedin_status", "connection_status"],
    "li_msg_1": ["li_1st_msg", "li_msg_1", "li_message_1", "linkedin_message_1", "li_msg1"],
    "li_msg_2": ["li_2nd_msg", "li_msg_2", "li_message_2", "linkedin_message_2", "li_msg2"],
    "email_1": ["em1", "em_1", "email_1", "email_1_date", "email1"],
    "email_2": ["em2", "em_2", "email_2", "email_2_date", "email2"],
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


CREDENTIALS = {"pe", "cpsm", "mba", "phd", "cpa", "pmp", "cfa", "jr", "sr", "ii", "iii", "esq"}


def _fix_text(s: str) -> str:
    """Undo Excel-on-Mac mojibake ('AtkinsR√©alis' -> 'AtkinsRéalis')."""
    if "√" in s:
        try:
            return s.encode("mac_roman").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def clean_last_name(last: str, full: str) -> str:
    """Spreadsheet splits leave 'KK)', 'Nats]', 'PE', 'MBA' as the last name: take it from the full name instead."""
    l = last.strip()
    bad = not l or not l[0].isalpha() or l[-1] in ")]" or l.lower().strip(".") in CREDENTIALS
    if not bad or not full:
        return l
    words = re.sub(r"[(\[][^)\]]*[)\]]", " ", full.split(",")[0]).split()
    words = [w for w in words if w.lower().strip(".") not in CREDENTIALS]
    return words[-1] if len(words) > 1 else l


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


def _keep_out(conn, lead_id: int, campaign_id: int) -> str | None:
    """Why an existing lead shouldn't join this campaign, or None. Someone who replied is in Leads, and someone in
    another live sequence would get two at once."""
    if one(conn, "SELECT 1 FROM enrollments WHERE lead_id = ? AND campaign_id = ?", lead_id, campaign_id):
        return None  # counted as already in the campaign
    lead = one(conn, "SELECT stage, dnc FROM leads WHERE id = ?", lead_id)
    if lead["dnc"]:
        return "unsubscribed / is do-not-contact"
    if lead["stage"] == "lead":
        return "already replied (in Leads)"
    other = one(conn, """SELECT c.name FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
                         WHERE e.lead_id = ? AND e.campaign_id != ? AND e.status IN ('pending', 'active')
                           AND c.status != 'draft' ORDER BY e.id LIMIT 1""", lead_id, campaign_id)
    return f"is still in another sequence ({other['name']})" if other else None


def import_rows(conn, rows: list[dict], mapping: dict[str, str], campaign_id: int | None) -> dict:
    now = utcnow()
    summary = {"created": 0, "updated": 0, "enrolled": 0, "already_enrolled": 0, "not_enrolled": 0,
               "skipped_rows": 0, "skipped": []}
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
        first, last = val["first_name"].strip(), val["last_name"]
        if not first and val["name"]:
            first, _, rest = val["name"].partition(" ")
            last = last or rest.strip()
        last = clean_last_name(last, val["name"])
        val["company"], val["title"] = _fix_text(val["company"]), _fix_text(val["title"])
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
            why = lead and _keep_out(conn, lead_id, campaign_id)
            if why:
                who = " ".join(x for x in (first or lead["first_name"], last or lead["last_name"]) if x) or email or li
                summary["skipped"].append(f"row {n}: {who} {why}; not enrolled")
                summary["not_enrolled"] += 1
                continue
            cur = conn.execute("INSERT OR IGNORE INTO enrollments (campaign_id, lead_id, enrolled_at) VALUES (?, ?, ?)",
                               (campaign_id, lead_id, iso(now)))
            if cur.rowcount:
                e = one(conn, "SELECT * FROM enrollments WHERE id = ?", cur.lastrowid)
                event(conn, lead_id, "enrolled", enrollment=e, at=now)
                summary["enrolled"] += 1
                hist = {f: val[f] for f in HISTORY if (val.get(f) or "").strip() not in ("", "-")}
                if hist:
                    note = engine.seed_history(conn, e, hist, now)
                    summary["with_history"] = summary.get("with_history", 0) + 1
                    if note:
                        summary["skipped"].append(f"row {n}: {note}")
            else:
                summary["already_enrolled"] += 1
    conn.execute("COMMIT")
    return summary
