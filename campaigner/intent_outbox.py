"""Collects campaigns queued by the RefractOne Intent Engine page.

Browsers block a public https page from posting to this app on 127.0.0.1, so the
page queues leads on the it-deals API instead and this loop pulls them every
POLL_SECONDS, creates the draft campaign, and reports the result back.
Env: OC_INTENT_API_URL (default the Render API), OC_INTENT_API_KEY (required).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

POLL_SECONDS = 30
log = logging.getLogger("intent_outbox")


def _call(method: str, path: str, body: dict | None = None) -> dict:
    base = os.environ.get("OC_INTENT_API_URL", "https://it-deals-api.onrender.com").rstrip("/")
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"x-api-key": os.environ["OC_INTENT_API_KEY"], "Content-Type": "application/json",
                                          "User-Agent": "outlook-campaigns"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def collect_once(intake) -> int:
    """intake(payload) -> result dict (raises on bad input). Returns items handled."""
    items = _call("GET", "/api/intent-engine/outbox").get("items", [])
    for it in items:
        try:
            res, ok = intake(it["payload"]), True
        except Exception as e:  # report it on the page rather than retry forever
            res, ok = {"error": str(getattr(e, "detail", "") or e)}, False
        _call("POST", f"/api/intent-engine/outbox/{it['id']}/ack", {"ok": ok, "result": res})
        log.info("intent outbox %s: %s", it["id"], "ok" if ok else res)
    return len(items)


def start(intake) -> bool:
    if not os.environ.get("OC_INTENT_API_KEY"):
        return False

    def loop():
        while True:
            try:
                collect_once(intake)
            except Exception as e:  # API asleep or offline: try again next round
                log.warning("intent outbox poll failed: %r", e)
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, name="intent-outbox", daemon=True).start()
    return True
