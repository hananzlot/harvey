"""Existing iBuildPro customers — never cold-email them (owner 2026-10-25).

The Studio bridge pulls the list from the app (RPC harvey_customer_suppression:
every customer company's user emails, its own email, and its business email
domains / website domain; free-mail domains appear only as exact addresses)
and writes it to data/customer_suppression.json. Harvey checks it when it adds
a contact, before it drafts, and right before every send.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.suppression")

SUPPRESSION_PATH = PROJECT_ROOT / "data" / "customer_suppression.json"
CUSTOMER_STATUS = "customer"

_cache: dict = {"mtime": None, "emails": frozenset(), "domains": frozenset()}


def _load(path: Path | None = None) -> tuple[frozenset, frozenset]:
    path = path or SUPPRESSION_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return frozenset(), frozenset()
    if _cache["mtime"] != (str(path), mtime):
        try:
            data = json.loads(path.read_text())
            _cache.update(
                mtime=(str(path), mtime),
                emails=frozenset(e.strip().lower() for e in data.get("emails", []) if e),
                domains=frozenset(d.strip().lower() for d in data.get("domains", []) if d),
            )
        except (OSError, ValueError) as e:
            logger.warning("Could not read customer suppression list: %s", e)
    return _cache["emails"], _cache["domains"]


def is_customer(email: str, path: Path | None = None) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    emails, domains = _load(path)
    domain = email.rsplit("@", 1)[1]
    return email in emails or domain in domains or any(
        domain.endswith("." + d) for d in domains)


def write_list(emails: list[str], domains: list[str], path: Path | None = None) -> None:
    """Atomically replace the list (called by the bridge)."""
    path = path or SUPPRESSION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"emails": sorted(set(emails)), "domains": sorted(set(domains))}))
    os.replace(tmp, path)


def has_list(path: Path | None = None) -> bool:
    return (path or SUPPRESSION_PATH).is_file()
