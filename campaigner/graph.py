"""Microsoft Graph client. Several M365 users can be signed in at once; each Graph instance acts as one of them."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache

import msal
import requests
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send", "Mail.ReadWrite", "User.Read"]
# Encrypted at rest: macOS Keychain under the hood. Holds every signed-in mailbox.
CACHE_PATH = os.path.expanduser("~/.outlook-campaigns/token_cache.bin")
MSG_FIELDS = "id,conversationId,from,subject,receivedDateTime,isDraft,internetMessageHeaders"
RETRY_STATUS = {429, 502, 503, 504}


class AuthError(RuntimeError):
    pass


class GraphError(RuntimeError):
    pass


# One instance per process: construction does network authority discovery.
@lru_cache(maxsize=1)
def _app() -> msal.PublicClientApplication:
    client_id = os.environ.get("OC_CLIENT_ID")
    if not client_id:
        raise AuthError("OC_CLIENT_ID not set. Put it in outlook-campaigns/.env (see README).")
    tenant = os.environ.get("OC_TENANT_ID", "organizations")
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=PersistedTokenCache(build_encrypted_persistence(CACHE_PATH)),
    )


def login(device_code: bool = False) -> str:
    """Sign in one more mailbox (the Microsoft page lets you pick or add an account). Returns its sign-in name."""
    app = _app()
    if device_code:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise AuthError(flow.get("error_description", "device code flow failed"))
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
    else:
        result = app.acquire_token_interactive(SCOPES, prompt="select_account")
    if "access_token" not in result:
        raise AuthError(result.get("error_description", "login failed"))
    return result.get("id_token_claims", {}).get("preferred_username", "?").lower()


def accounts() -> list[str]:
    """Signed-in sign-in names, from the local cache (no network after first use)."""
    try:
        return [a["username"].lower() for a in _app().get_accounts()]
    except AuthError:
        return []


def logout(account: str) -> None:
    app = _app()
    for a in app.get_accounts():
        if a["username"].lower() == account.lower():
            app.remove_account(a)


def _token(account: str | None = None) -> str:
    app = _app()
    accs = app.get_accounts()
    if account:
        accs = [a for a in accs if a["username"].lower() == account.lower()]
    result = app.acquire_token_silent(SCOPES, account=accs[0]) if accs else None
    if not result or "access_token" not in result:
        raise AuthError(f"{account or 'Outlook'} is not signed in (or needs to sign in again).")
    return result["access_token"]


def _rcpt(email: str, name: str) -> dict:
    addr = {"address": email}
    if name:
        addr["name"] = name
    return {"emailAddress": addr}


def _q(s: str) -> str:
    return s.replace("'", "''")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_top(doc: str, html: str) -> str:
    m = re.search(r"<body[^>]*>", doc, re.I)
    return doc[: m.end()] + html + "<br>" + doc[m.end():] if m else html + "<br>" + doc


class Graph:
    def __init__(self, account: str | None = None):
        self.account = account
        _token(account)  # fail fast if not signed in
        self.s = requests.Session()
        self._me = None

    def _req(self, method: str, url: str, **kw):
        url = url if url.startswith("http") else GRAPH + url
        # Token fetched per call: silent refresh keeps long throttled runs alive past the 1h expiry.
        headers = {"Authorization": f"Bearer {_token(self.account)}", **kw.pop("headers", {})}
        for attempt in range(6):
            r = self.s.request(method, url, headers=headers, timeout=30, **kw)
            if r.status_code in RETRY_STATUS:
                time.sleep(int(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                raise GraphError(f"{method} {url.removeprefix(GRAPH)} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise GraphError(f"{method} {url.removeprefix(GRAPH)}: still throttled after retries")

    def _list(self, path: str, **params) -> list[dict]:
        out, url, q = [], path, {f"${k}": v for k, v in params.items()}
        while url:
            page = self._req("GET", url, params=q)
            out += page.get("value", [])
            url, q = page.get("@odata.nextLink"), None
        return out

    def profile(self) -> dict:
        """{'email', 'name', 'account'}: the address mail goes out from."""
        if self._me is None:
            me = self._req("GET", "/me", params={"$select": "mail,userPrincipalName,displayName"})
            self._me = {"email": (me.get("mail") or me["userPrincipalName"]).lower(),
                        "name": me.get("displayName") or "", "account": me["userPrincipalName"].lower()}
        return self._me

    def my_address(self) -> str:
        return self.profile()["email"]

    def _send_draft(self, draft_id: str) -> None:
        try:
            self._req("POST", f"/me/messages/{draft_id}/send")
        except GraphError:
            try:
                self._req("DELETE", f"/me/messages/{draft_id}")  # don't leave orphan drafts
            except GraphError:
                pass
            raise

    def send_new(self, to_email: str, to_name: str, subject: str, html: str) -> str:
        """Send a fresh message. Returns its conversationId."""
        draft = self._req("POST", "/me/messages", json={
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [_rcpt(to_email, to_name)],
        })
        self._send_draft(draft["id"])
        return draft["conversationId"]

    def send_in_thread(self, conversation_id: str, to_email: str, to_name: str, html: str, fallback_subject: str,
                       strip_pixels: str | None = None) -> str:
        """Reply on top of our last sent message in the thread, so follow-ups stack like a human's would.
        strip_pixels: tracker base URL; earlier open pixels are removed from the quoted text so they
        don't fire again (and misattribute opens) when the follow-up is read."""
        sent = self._list(
            "/me/mailFolders/sentitems/messages",
            filter=f"conversationId eq '{_q(conversation_id)}'",
            select="id,sentDateTime",
        )
        if not sent:
            return self.send_new(to_email, to_name, fallback_subject, html)
        last = max(sent, key=lambda m: m.get("sentDateTime") or "")
        draft = self._req("POST", f"/me/messages/{last['id']}/createReply",
                          headers={"Prefer": 'outlook.body-content-type="html"'})
        quoted = draft["body"]["content"]
        if strip_pixels:
            quoted = re.sub(r"<img[^>]*" + re.escape(strip_pixels) + r"/o/[^>]*>", "", quoted, flags=re.I)
        self._req("PATCH", f"/me/messages/{draft['id']}", json={
            "body": {"contentType": "HTML", "content": _insert_top(quoted, html)},
            "toRecipients": [_rcpt(to_email, to_name)],
            "ccRecipients": [],
        })
        self._send_draft(draft["id"])
        return conversation_id

    def conversation(self, conversation_id: str) -> list[dict]:
        return self._list("/me/messages", filter=f"conversationId eq '{_q(conversation_id)}'", select=MSG_FIELDS)

    def inbound_from(self, email: str, since: datetime) -> list[dict]:
        return self._list(
            "/me/messages",
            filter=f"from/emailAddress/address eq '{_q(email)}' and receivedDateTime ge {_iso(since)}",
            select=MSG_FIELDS,
        )

    def messages_since(self, since: datetime) -> list[dict]:
        """Everything received after `since`, all folders (LinkedIn mails often get rule-filed)."""
        return self._list("/me/messages", filter=f"receivedDateTime gt {_iso(since)}",
                          select="id,from,subject,bodyPreview,receivedDateTime", top="100")
