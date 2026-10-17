"""Sending-domain checks: SPF, DKIM (Microsoft 365 selectors), DMARC, MX. Results cached in settings."""
from __future__ import annotations

from datetime import timedelta

import dns.exception
import dns.resolver

from . import db
from .db import iso, parse, utcnow

CACHE_FOR = timedelta(hours=12)


def _txt(name: str) -> list[str]:
    try:
        return [b"".join(r.strings).decode(errors="replace") for r in dns.resolver.resolve(name, "TXT", lifetime=5)]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []


def _exists(name: str, rtype: str) -> list[str]:
    try:
        return [r.to_text().rstrip(".") for r in dns.resolver.resolve(name, rtype, lifetime=5)]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []


def check(domain: str) -> list[dict]:
    """[{check, status: good|warning|critical, detail}]"""
    out = []
    try:
        spf = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
        if not spf:
            out.append(("SPF", "critical", "No SPF record. Receivers can't verify you're allowed to send."))
        elif len(spf) > 1:
            out.append(("SPF", "critical", "Multiple SPF records. That's invalid; merge them into one."))
        elif spf[0].rstrip().endswith("+all"):
            out.append(("SPF", "critical", "SPF ends in +all, which lets anyone send as you."))
        elif "spf.protection.outlook.com" in spf[0].lower():
            out.append(("SPF", "good", spf[0]))
        else:
            out.append(("SPF", "warning", "No direct include:spf.protection.outlook.com (fine if a nested include "
                                          f"covers Microsoft 365): {spf[0]}"))

        dkim = [s for s in ("selector1", "selector2") if _exists(f"{s}._domainkey.{domain}", "CNAME")]
        out.append(("DKIM", "good", f"Microsoft 365 selectors found: {', '.join(dkim)}") if dkim else
                   ("DKIM", "warning", "No Microsoft 365 DKIM selectors (selector1/2._domainkey). "
                                       "Enable DKIM in Microsoft Defender → Email authentication."))

        dmarc = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
        if not dmarc:
            out.append(("DMARC", "critical", "No DMARC record. Gmail and Yahoo require one for bulk senders."))
        else:
            tags = dict(p.strip().split("=", 1) for p in dmarc[0].split(";") if "=" in p)
            policy = tags.get("p", "").strip().lower()
            out.append(("DMARC", "good" if policy in ("quarantine", "reject") else "warning",
                        f"p={policy or '?'}" + ("" if policy in ("quarantine", "reject")
                                                else ". Monitoring only; fine to start, tighten later.")))

        mx = [m.split()[-1] for m in _exists(domain, "MX") if len(m.split()) == 2]  # "0 ." is a null MX
        out.append(("MX", "good", ", ".join(mx[:2])) if mx else
                   ("MX", "warning", "No MX: replies can't reach this domain."))
    except dns.exception.DNSException as e:
        out.append(("DNS", "warning", f"Lookup failed: {e.__class__.__name__}"))
    return [{"check": c, "status": s, "detail": d} for c, s, d in out]


def cached(conn, domain: str, refresh: bool = False) -> dict:
    key = f"dns:{domain}"
    hit = db.get_setting(conn, key)
    if hit and not refresh and utcnow() - parse(hit["checked_at"]) < CACHE_FOR:
        return hit
    hit = {"checked_at": iso(utcnow()), "results": check(domain)}
    db.set_setting(conn, key, hit)
    return hit
