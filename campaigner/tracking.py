"""Open / click / unsubscribe tracking through the Cloudflare Worker in tracker/.

URLs are HMAC-signed with OC_TRACK_SECRET (shared with the worker), so the worker only redirects to links
we put in our own emails: no open redirect, no forged events. Events are pulled with OC_TRACK_KEY.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

import requests

# Link scanners and prefetchers (Safe Links, Mimecast, Proofpoint, Apple Mail Privacy Protection...) hit
# pixels and links automatically. These hits are counted separately and excluded from rates.
BOT_UA = re.compile(r"bot|crawl|spider|preview|scan|barracuda|mimecast|proofpoint|safelinks|python|curl|wget|"
                    r"headless|go-http|java/|okhttp|axios|node-fetch", re.I)
MACHINE_WINDOW_S = 60  # hits this soon after sending are almost always scanners, not people


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Tracker:
    def __init__(self, base: str, key: str, secret: str):
        self.base, self.key, self.secret = base.rstrip("/"), key, secret

    @classmethod
    def from_env(cls) -> Tracker | None:
        base, key, secret = (os.environ.get(k, "").strip() for k in ("OC_TRACK_URL", "OC_TRACK_KEY", "OC_TRACK_SECRET"))
        return cls(base, key, secret) if base and key and secret else None

    def sign(self, msg: str) -> str:
        return b64(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest())[:22]

    def open_url(self, sid: str) -> str:
        return f"{self.base}/o/{sid}/{self.sign('o:' + sid)}.gif"

    def click_url(self, sid: str, url: str) -> str:
        return f"{self.base}/c/{sid}/{b64(url.encode())}/{self.sign(f'c:{sid}:{url}')}"

    def unsub_url(self, sid: str) -> str:
        return f"{self.base}/u/{sid}/{self.sign('u:' + sid)}"

    def owns(self, url: str) -> bool:
        return url.startswith(self.base + "/")

    def pull(self, since: int) -> list[dict]:
        r = requests.get(f"{self.base}/events", params={"since": since},
                         headers={"Authorization": f"Bearer {self.key}"}, timeout=20)
        r.raise_for_status()
        return r.json()["events"]

    def health(self) -> str | None:
        """None if the worker answers and the API key works, else what's wrong."""
        try:
            r = requests.get(f"{self.base}/events", params={"since": 2 ** 52},
                             headers={"Authorization": f"Bearer {self.key}"}, timeout=10)
        except requests.RequestException as e:
            return f"unreachable: {e.__class__.__name__}"
        if r.status_code == 401:
            return "API key rejected (OC_TRACK_KEY doesn't match the worker's API_KEY secret)"
        if r.status_code != 200:
            return f"worker answered {r.status_code}"
        return None
