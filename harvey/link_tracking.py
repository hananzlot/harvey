"""Per-email tracked links (iBuildPro).

Right before an email is sent, every iBuildPro link in its body is swapped for
that email's own short link on the app domain (<app_base_url>/r/o-<outbox id>),
made by the Supabase RPC harvey_outreach_link. The app's existing short-link
system logs each click, and iBuildPro shows it on the lead and on
Super Admin -> Outreach (migration 20261202003700_outreach_link_clicks.sql).

Tracking must never stop an email: any failure leaves the body unchanged and
logs a warning. HARVEY_TRACK_LINKS=0 turns it off (the test suite does).
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from harvey.gate import URL_RE
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

TRACKED_HOSTS = ("ibuildpro.ai", "ibuildpro.co")
TRAILING_PUNCT = ".,;:!?)]}'\""
TIMEOUT_SECONDS = 10.0


def enabled() -> bool:
    return os.environ.get("HARVEY_TRACK_LINKS", "1") != "0"


def _credentials() -> tuple[str, str]:
    file_env = dotenv_values(PROJECT_ROOT / ".env")
    get = lambda k: (os.environ.get(k) or file_env.get(k) or "").strip()  # noqa: E731
    return get("SUPABASE_URL").rstrip("/"), get("SUPABASE_SERVICE_ROLE_KEY")


def _is_trackable(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in TRACKED_HOSTS):
        return False
    return not parts.path.startswith("/r/")  # already a short link


async def track_links(outbox_id: str, body: str) -> str:
    """Return body with each iBuildPro link replaced by this email's short link."""
    if not enabled() or not outbox_id or not body:
        return body
    targets: list[str] = []
    for raw in URL_RE.findall(body):
        url = raw.rstrip(TRAILING_PUNCT)
        if url and _is_trackable(url) and url not in targets:
            targets.append(url)
    if not targets:
        return body

    base, key = _credentials()
    if not base or not key:
        logger.warning("Link tracking: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing; sending untracked.")
        return body

    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    out = body
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            # One short link per email (the gate allows one link anyway), so only
            # the first iBuildPro URL is tracked.
            url = targets[0]
            r = await client.post(
                f"{base}/rest/v1/rpc/harvey_outreach_link",
                headers=headers,
                json={"p_outbox_id": str(outbox_id), "p_long_url": url},
            )
            if r.status_code >= 300:
                logger.warning(f"Link tracking: RPC -> {r.status_code}: {r.text[:200]}; sending untracked.")
                return body
            short = r.json()
            if not isinstance(short, str) or not short.startswith("http"):
                logger.warning(f"Link tracking: no link returned for {outbox_id}; sending untracked.")
                return body
            out = out.replace(url, short)
    except Exception as e:  # network, JSON - never block the send
        logger.warning(f"Link tracking failed for {outbox_id}: {e}; sending untracked.")
        return body
    return out
