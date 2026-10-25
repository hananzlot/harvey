"""iBuildPro bridge: mirror Harvey's local state UP to Supabase, run commands DOWN.

Contract: docs/harvey-bridge.md in the iBuildPro repo. Two tables are used:

* ``harvey_records``  (kind, external_id, data jsonb, synced_at) - upserted here
* ``harvey_commands`` (id, kind, payload, status, result, processed_at) - drained here

Commands are executed through Harvey's own dashboard HTTP API on 127.0.0.1:5555
(so Harvey's own logic runs), except ``edit_draft`` (SQLite) and
``update_config`` (harvey.local.yaml). Nothing here ever crashes the loop on a
single bad command or a network error.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import yaml
from dotenv import dotenv_values

from harvey.config import HarveyConfig, _find_config_file, load_config
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.bridge")

VERSION = "v0.2.1+ibuildpro"
DASHBOARD_URL = "http://127.0.0.1:5555"
COMMAND_INTERVAL = 15
SNAPSHOT_INTERVAL = 60
BATCH_SIZE = 500
ACTIVITY_KEEP = 500
DELETE_CHUNK = 100
MAX_DISCOVER_SPEND = 5.0
DISCOVER_PROVIDERS = {"dataforseo_listings", "osm", "cslb"}
SUPPRESSION_INTERVAL = 30 * 60
SYNCED_KINDS = ("outbox", "conversation", "prospect", "company", "activity")
PROTECTED_TOP_LEVEL = {"postal_address", "compliance"}


class CommandError(Exception):
    """A command failed; the message is the plain-English result."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str | None:
    """SQLite stores naive UTC ('YYYY-MM-DD HH:MM:SS' or isoformat). Emit ISO with Z."""
    if value in (None, ""):
        return None
    s = str(value).strip().replace(" ", "T", 1)
    if s.endswith("Z") or "+" in s[10:] or s[10:].count("-"):
        return s
    return s + "Z"


def _local_iso(value: Any) -> str | None:
    """The dashboard reports started_at as naive LOCAL time (datetime.now());
    attach this machine's offset instead of mislabelling it UTC."""
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return _iso(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def _hash(data: dict) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def _name(first: Any, last: Any) -> str:
    # A business contact (office@, an owner's gmail) has no personal name.
    return f"{first or ''} {last or ''}".strip() or "Owner (business address)"


# ── Pure helpers ──


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge ``patch`` into a copy of ``base`` (lists/scalars replace)."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def translate_patch(patch: dict) -> dict:
    """Map the page's ``config`` shape onto the YAML shape.

    ``sending`` {max_daily_sends, require_approval, quiet_hours} lives in
    ``channels.email`` and ``usage.quiet_hours`` in harvey.local.yaml.
    """
    p = copy.deepcopy(patch)
    sending = p.pop("sending", None)
    if sending is not None:
        if not isinstance(sending, dict):
            raise CommandError("'sending' must be an object.")
        email: dict = {}
        for key in ("max_daily_sends", "require_approval"):
            if key in sending:
                email[key] = sending[key]
        if email:
            p = deep_merge(p, {"channels": {"email": email}})
        if "quiet_hours" in sending:
            p = deep_merge(p, {"usage": {"quiet_hours": sending["quiet_hours"]}})
    return p


def _in_quiet_hours(start: str, end: str, tz_name: str) -> bool:
    import pytz

    try:
        now = datetime.now(pytz.timezone(tz_name)).time()
        s = datetime.strptime(start, "%H:%M").time()
        e = datetime.strptime(end, "%H:%M").time()
    except Exception:
        return False
    if s <= e:
        return s <= now < e
    return now >= s or now < e  # window crosses midnight


def footer_for(config: HarveyConfig) -> str:
    """The exact footer Sender appends at send time (reuses Sender's own code)."""
    from harvey.agents.sender import Sender

    return Sender._compliance_footer(SimpleNamespace(config=config))


# ── Bridge ──


class Bridge:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_key: str,
        db_path: Path | str | None = None,
        config_path: Path | str | None = None,
        state_path: Path | str | None = None,
        backup_dir: Path | str | None = None,
        dashboard_url: str = DASHBOARD_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        host: str | None = None,
    ):
        self.base = supabase_url.rstrip("/") + "/rest/v1"
        self.key = service_key
        self.db_path = str(db_path or PROJECT_ROOT / "data" / "harvey.db")
        self.config_path = str(config_path) if config_path else None
        self.state_path = Path(state_path or PROJECT_ROOT / "data" / "bridge_state.json")
        self.backup_dir = Path(backup_dir or PROJECT_ROOT / "data" / "config-backups")
        self.dashboard_url = dashboard_url.rstrip("/")
        self.host = host or socket.gethostname()
        self._sb = httpx.AsyncClient(transport=transport, timeout=30)
        self._dash = httpx.AsyncClient(transport=transport, timeout=120)
        self._cache: dict[str, dict[str, str]] = self._load_cache()
        self._seeded = False
        self._handled: dict[str, tuple[str, str]] = {}  # executed, result write pending

    async def aclose(self):
        await self._sb.aclose()
        await self._dash.aclose()

    # ── Supabase plumbing ──

    def _headers(self, **extra: str) -> dict[str, str]:
        h = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        h.update(extra)
        return h

    async def _sb_req(self, method: str, path: str, **kw) -> httpx.Response:
        r = await self._sb.request(method, self.base + path, headers=self._headers(
            **kw.pop("prefer", {})), **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {method} {path.split('?')[0]} -> {r.status_code}: {r.text[:200]}")
        return r

    # ── Hash cache ──

    def _load_cache(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self.state_path.read_text())
            return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _save_cache(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache))
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning("Could not save bridge cache: %s", e)

    async def _seed_from_remote(self):
        """First push of this process: learn which ids already exist remotely, so
        rows deleted locally while the bridge was down still get removed."""
        for kind in SYNCED_KINDS:
            offset = 0
            while True:
                r = await self._sb_req(
                    "GET", f"/harvey_records?select=external_id&kind=eq.{kind}"
                    f"&order=external_id.asc&limit=1000&offset={offset}")
                rows = r.json()
                bucket = self._cache.setdefault(kind, {})
                for row in rows:
                    bucket.setdefault(row["external_id"], "?")
                if len(rows) < 1000:
                    break
                offset += 1000
        self._seeded = True

    # ── Local reads ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _rows(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error as e:
            logger.warning("bridge query failed (%s): %s", sql[:40], e)
            return []

    def _load_config(self) -> HarveyConfig | None:
        try:
            return load_config(self.config_path)
        except Exception as e:
            logger.warning("bridge: could not load config: %s", e)
            return None

    def _config_path(self) -> str:
        return self.config_path or _find_config_file()

    # ── Snapshot building ──

    def build_snapshot(self, running: dict | None) -> dict[str, dict[str, dict]]:
        """{kind: {external_id: data}} for everything the page reads."""
        cfg = self._load_config()
        out: dict[str, dict[str, dict]] = {k: {} for k in ("status", "config", *SYNCED_KINDS)}
        conn = self._connect()
        try:
            out["outbox"] = self._outbox(conn, cfg)
            out["conversation"] = self._conversations(conn)
            out["prospect"] = self._prospects(conn)
            out["company"] = self._companies(conn)
            out["activity"] = self._activity(conn)
            out["status"] = {"current": self._status(conn, cfg, running)}
        finally:
            conn.close()
        if cfg is not None:
            out["config"] = {"current": self._config_snapshot(cfg)}
        return out

    def _outbox(self, conn, cfg) -> dict[str, dict]:
        footer = footer_for(cfg) if cfg is not None else ""
        rows = self._rows(conn, """
            SELECT o.*, p.first_name, p.last_name, p.company AS p_company,
                   p.email_status, p.personalization_notes, c.name AS c_name
            FROM outbox o
            LEFT JOIN prospects p ON p.id = o.prospect_id
            LEFT JOIN companies c ON c.id = p.company_id""")
        res = {}
        for r in rows:
            res[r["id"]] = {
                "kind": r["kind"], "step": r["step"], "status": r["status"],
                "to_email": r["to_email"], "subject": r["subject"], "body": r["body"],
                "footer": footer,
                "send_at": _iso(r["send_at"]), "sent_at": _iso(r["sent_at"]),
                "error": r["error"] or "", "prospect_id": r["prospect_id"],
                "prospect_name": _name(r["first_name"], r["last_name"]),
                "company_name": r["c_name"] or r["p_company"] or "",
                "email_status": r["email_status"] or "",
                "personalization_notes": r["personalization_notes"] or "",
                "conversation_id": r["conversation_id"],
                "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            }
        return res

    def _conversations(self, conn) -> dict[str, dict]:
        rows = self._rows(conn, """
            SELECT cv.*, p.first_name, p.last_name, p.email AS p_email,
                   p.company AS p_company, c.name AS c_name
            FROM conversations cv
            LEFT JOIN prospects p ON p.id = cv.prospect_id
            LEFT JOIN companies c ON c.id = p.company_id""")
        res = {}
        for r in rows:
            try:
                raw = json.loads(r["thread_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                raw = []
            thread = [
                {"sender": m.get("sender"), "content": m.get("content"),
                 "timestamp": _iso(m.get("timestamp"))}
                for m in raw if isinstance(m, dict)
            ]
            res[r["id"]] = {
                "prospect_id": r["prospect_id"],
                "prospect_name": _name(r["first_name"], r["last_name"]),
                "company_name": r["c_name"] or r["p_company"] or "",
                "email": r["p_email"] or "", "intent": r["intent"], "stage": r["stage"],
                "status": r["status"], "thread": thread,
                "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            }
        return res

    def _prospects(self, conn) -> dict[str, dict]:
        rows = self._rows(conn, """
            SELECT p.*, c.name AS c_name FROM prospects p
            LEFT JOIN companies c ON c.id = p.company_id""")
        return {
            r["id"]: {
                "first_name": r["first_name"], "last_name": r["last_name"],
                "title": r["title"], "email": r["email"],
                "email_status": r["email_status"] or "", "company_id": r["company_id"],
                "company_name": r["c_name"] or r["company"] or "",
                "industry": r["industry"], "status": r["status"], "score": r["score"],
                "contact_kind": (r["contact_kind"] if "contact_kind" in r.keys() else "") or "person",
                "personalization_notes": r["personalization_notes"],
                "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            } for r in rows
        }

    def _companies(self, conn) -> dict[str, dict]:
        res = {}
        for r in self._rows(conn, "SELECT * FROM companies"):
            try:
                sig = json.loads(r["signals_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                sig = []
            codes: list[str] = []
            for s in sig if isinstance(sig, list) else []:
                code = s if isinstance(s, str) else (
                    (s.get("type") or s.get("code")) if isinstance(s, dict) else None)
                if code and code not in codes:
                    codes.append(code)
            res[r["id"]] = {
                "name": r["name"], "domain": r["domain"], "website": r["website"],
                "location": r["location"], "industry": r["industry"],
                "phone": r["phone"] or "", "source": r["source"], "signals": codes,
                "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            }
        return res

    @staticmethod
    def _summary(action_type: str, agent: str, details: dict) -> str:
        bits = []
        for k, v in list(details.items())[:4]:
            if isinstance(v, (str, int, float, bool)):
                bits.append(f"{k.replace('_', ' ')} {v}")
        tail = f" ({', '.join(bits)})" if bits else ""
        return f"{agent}: {action_type.replace('_', ' ')}{tail}"

    def _activity(self, conn) -> dict[str, dict]:
        rows = self._rows(
            conn, "SELECT * FROM actions ORDER BY created_at DESC, id DESC LIMIT ?",
            (ACTIVITY_KEEP,))
        res = {}
        for r in rows:
            try:
                details = json.loads(r["details_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                details = {}
            res[r["id"]] = {
                "action_type": r["action_type"], "agent": r["agent"],
                "summary": self._summary(r["action_type"] or "", r["agent"] or "",
                                         details if isinstance(details, dict) else {}),
                "created_at": _iso(r["created_at"]),
            }
        return res

    def _count_map(self, conn, table: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._rows(
            conn, f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")}

    def _scalar(self, conn, sql: str, params: tuple = (), default=0):
        rows = self._rows(conn, sql, params)
        if not rows:
            return default
        v = next(iter(rows[0].values()))
        return default if v is None else v

    def _setting(self, conn, key: str) -> str:
        return str(self._scalar(conn, "SELECT value FROM settings WHERE key = ?", (key,), ""))

    def _status(self, conn, cfg, running: dict | None) -> dict:
        qh = cfg.usage.quiet_hours if cfg else None
        replies = 0
        for r in self._rows(conn, "SELECT thread_json FROM conversations"):
            try:
                if any(isinstance(m, dict) and m.get("sender") == "prospect"
                       for m in json.loads(r["thread_json"] or "[]")):
                    replies += 1
            except (json.JSONDecodeError, TypeError):
                pass
        try:
            bounces = int(self._setting(conn, "bounce_count") or 0)
        except ValueError:
            bounces = 0
        return {
            "running": bool(running and running.get("running")),
            "started_at": _local_iso(running.get("started_at")) if running else None,
            "sending_paused": self._setting(conn, "sending_paused"),
            "sent_today": self._scalar(
                conn, "SELECT COUNT(*) FROM outbox WHERE status='sent' "
                      "AND date(sent_at) = date('now')"),
            "daily_cap": cfg.channels.email.max_daily_sends if cfg else 0,
            "require_approval": cfg.channels.email.require_approval if cfg else True,
            "quiet_hours": ({"start": qh.start, "end": qh.end, "timezone": qh.timezone}
                            if qh else None),
            "in_quiet_hours": _in_quiet_hours(qh.start, qh.end, qh.timezone) if qh else False,
            "claude_calls_today": self._scalar(
                conn, "SELECT claude_calls FROM usage_log WHERE date = date('now')"),
            "bounce_count": bounces,
            "total_sent": self._scalar(conn, "SELECT COUNT(*) FROM outbox WHERE status='sent'"),
            "replies_total": replies,
            "prospects_by_status": self._count_map(conn, "prospects"),
            "outbox_by_status": self._count_map(conn, "outbox"),
            "companies_total": self._scalar(conn, "SELECT COUNT(*) FROM companies"),
            # No explicit cycle marker exists; the newest logged action is the best proxy.
            "last_cycle_at": _iso(self._scalar(
                conn, "SELECT MAX(created_at) FROM actions", default=None)),
            "hq_emails_copied": self.hq_emails_copied(),
            "bridge_heartbeat_at": _now_iso(),
            "version": VERSION,
            "host": self.host,
        }

    @staticmethod
    def _config_snapshot(cfg: HarveyConfig) -> dict:
        offer = cfg.product.offer
        qh = cfg.usage.quiet_hours
        return {
            "persona": {"name": cfg.persona.name, "company": cfg.persona.company,
                        "role": cfg.persona.role, "email": cfg.persona.email,
                        "tone": cfg.persona.tone},
            "product": {"name": cfg.product.name, "description": cfg.product.description,
                        "pricing": cfg.product.pricing,
                        "key_benefits": list(cfg.product.key_benefits),
                        "offer": {"primary": offer.primary, "entry": offer.entry,
                                  "goal": offer.goal}},
            "icp": {"industries": list(cfg.icp.industries),
                    "company_size": cfg.icp.company_size, "titles": list(cfg.icp.titles),
                    "geography": list(cfg.icp.geography),
                    "geo_coordinates": dict(cfg.icp.geo_coordinates)},
            "sending": {"max_daily_sends": cfg.channels.email.max_daily_sends,
                        "require_approval": cfg.channels.email.require_approval,
                        "quiet_hours": {"start": qh.start, "end": qh.end,
                                        "timezone": qh.timezone}},
            "guidance": cfg.guidance,
            "postal_address": cfg.compliance.postal_address,
        }

    # ── Push ──

    async def _dash_status(self) -> dict | None:
        try:
            r = await self._dash.get(self.dashboard_url + "/api/harvey/status")
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    async def _upsert(self, rows: list[dict]):
        for i in range(0, len(rows), BATCH_SIZE):
            await self._sb_req(
                "POST", "/harvey_records?on_conflict=kind,external_id",
                json=rows[i:i + BATCH_SIZE],
                prefer={"Prefer": "resolution=merge-duplicates,return=minimal"})

    async def _delete(self, kind: str, ids: list[str]):
        for i in range(0, len(ids), DELETE_CHUNK):
            chunk = ids[i:i + DELETE_CHUNK]
            quoted = ",".join('"' + x.replace('"', '') + '"' for x in chunk)
            await self._sb_req(
                "DELETE", f"/harvey_records?kind=eq.{kind}&external_id=in.({quoted})",
                prefer={"Prefer": "return=minimal"})

    async def push_snapshot(self) -> dict[str, int]:
        """Upsert changed rows, delete vanished ones. Returns counts for logging."""
        if not self._seeded:
            await self._seed_from_remote()
        running = await self._dash_status()
        snap = await asyncio.to_thread(self.build_snapshot, running)
        stamp = _now_iso()
        counts = {"upserted": 0, "deleted": 0}
        for kind, rows in snap.items():
            cache = self._cache.setdefault(kind, {})
            changed = [(i, d, _hash(d)) for i, d in rows.items() if cache.get(i) != _hash(d)]
            if changed:
                await self._upsert([
                    {"kind": kind, "external_id": i, "data": d, "synced_at": stamp}
                    for i, d, _ in changed])
                for i, _, h in changed:
                    cache[i] = h
                counts["upserted"] += len(changed)
                self._save_cache()
            if kind in SYNCED_KINDS:
                gone = [i for i in cache if i not in rows]
                if gone:
                    await self._delete(kind, gone)
                    for i in gone:
                        cache.pop(i, None)
                    counts["deleted"] += len(gone)
                    self._save_cache()
        return counts

    # ── Existing-customer list ──

    async def refresh_suppression(self, path=None) -> dict[str, int]:
        """Pull every existing iBuildPro customer's emails and business domains
        (RPC harvey_customer_suppression) for harvey/suppression.py. Replaced
        only on a successful, non-empty answer, so a failed call never wipes
        the list Harvey is checking against."""
        from harvey import suppression
        r = await self._sb_req("POST", "/rpc/harvey_customer_suppression", json={})
        rows = r.json() if isinstance(r.json(), list) else []
        emails = [x["value"] for x in rows if x.get("kind") == "email" and x.get("value")]
        domains = [x["value"] for x in rows if x.get("kind") == "domain" and x.get("value")]
        if not emails and not domains:
            raise RuntimeError("customer list came back empty; keeping the previous one")
        suppression.write_list(emails, domains, **({"path": path} if path else {}))
        return {"emails": len(set(emails)), "domains": len(set(domains))}

    # ── HQ contact write-back ──

    WRITEBACK_KEY = "_hq_writeback"
    WRITEBACK_STATUSES = ("verified", "risky")

    def _writeback_candidates(self) -> list[dict]:
        """Licensed-contractor prospects with a checked email, one per licence."""
        sql = (
            "SELECT c.external_id AS ext, p.email AS email, p.email_status AS status, "
            "       c.domain AS domain "
            "  FROM prospects p JOIN companies c ON c.id = p.company_id "
            " WHERE c.external_id LIKE 'cslb:%' AND p.email != '' "
            "   AND p.status NOT IN ('lost', 'customer') "
            f"   AND p.email_status IN ({','.join('?' * len(self.WRITEBACK_STATUSES))}) "
            " ORDER BY CASE p.email_status WHEN 'verified' THEN 0 ELSE 1 END, p.created_at"
        )
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, self.WRITEBACK_STATUSES).fetchall()
        except sqlite3.Error as e:
            logger.debug("write-back read failed: %s", e)
            return []
        out: dict[str, dict] = {}
        for r in rows:
            licence = str(r["ext"]).split(":", 1)[1].strip()
            if licence and licence not in out:
                out[licence] = {"licence": licence, "email": r["email"].strip().lower(),
                                "status": r["status"], "domain": r["domain"] or ""}
        return list(out.values())

    async def write_back_emails(self) -> dict[str, int]:
        """Copy each found email onto the iBuildPro HQ contact with that licence
        number. The database function only fills an EMPTY email, so a value a
        person typed is never overwritten. Each licence+email is sent once."""
        done = self._cache.setdefault(self.WRITEBACK_KEY, {})
        counts: dict[str, int] = {}
        for c in self._writeback_candidates():
            if done.get(c["licence"], "").split("|", 1)[0] == c["email"]:
                continue
            r = await self._sb_req("POST", "/rpc/harvey_record_contact_email", json={
                "p_license_number": c["licence"], "p_email": c["email"],
                "p_email_status": c["status"],
                "p_website": f"https://{c['domain']}" if c["domain"] else None,
            })
            result = str(r.json())
            done[c["licence"]] = f"{c['email']}|{result}"
            counts[result] = counts.get(result, 0) + 1
            self._save_cache()
        return counts

    def hq_emails_copied(self) -> int:
        return sum(1 for v in self._cache.get(self.WRITEBACK_KEY, {}).values()
                   if v.endswith("|updated"))

    # ── Commands ──

    async def drain_commands(self) -> int:
        r = await self._sb_req(
            "GET", "/harvey_commands?status=eq.pending&order=created_at.asc&limit=50")
        n = 0
        for cmd in r.json():
            cid = cmd["id"]
            if cid in self._handled:
                # Already executed; only the result write failed. Never re-run it.
                status, result = self._handled[cid]
            else:
                try:
                    result = await self.execute(cmd["kind"], cmd.get("payload") or {})
                    status = "done"
                except CommandError as e:
                    status, result = "failed", str(e)
                except Exception as e:  # never let one command kill the loop
                    logger.exception("command %s crashed", cid)
                    status, result = "failed", f"Unexpected error: {e}"
                self._handled[cid] = (status, result)
            await self._sb_req(
                "PATCH", f"/harvey_commands?id=eq.{cid}",
                json={"status": status, "result": result[:500], "processed_at": _now_iso()},
                prefer={"Prefer": "return=minimal"})
            self._handled.pop(cid, None)
            n += 1
        return n

    async def _dash_call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self._dash.request(method, self.dashboard_url + path, **kw)
        except httpx.HTTPError as e:
            raise CommandError(
                "Harvey's dashboard on the Studio isn't answering, so nothing was changed.") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 300 or (isinstance(body, dict) and body.get("success") is False):
            msg = (body.get("message") or body.get("error")) if isinstance(body, dict) else None
            raise CommandError(msg or f"Harvey refused it (HTTP {r.status_code}).")
        return body if isinstance(body, dict) else {}

    async def execute(self, kind: str, payload: dict) -> str:
        fn = getattr(self, f"_cmd_{kind}", None)
        if fn is None:
            raise CommandError(f"Unknown command '{kind}'.")
        if not isinstance(payload, dict):
            raise CommandError("The command's details were not in the expected form.")
        return await fn(payload)

    @staticmethod
    def _need(payload: dict, key: str) -> str:
        v = payload.get(key)
        if not isinstance(v, str) or not v.strip():
            raise CommandError(f"Missing '{key}'.")
        return v.strip()

    async def _cmd_approve(self, p: dict) -> str:
        oid = self._need(p, "outbox_id")
        body = await self._dash_call("POST", f"/api/outbox/{oid}/approve")
        if not body.get("success"):
            raise CommandError("That draft isn't waiting for review anymore.")
        return "Draft approved. It will go out at the next sending window."

    async def _cmd_reject(self, p: dict) -> str:
        await self._dash_call("POST", f"/api/outbox/{self._need(p, 'outbox_id')}/reject")
        return "Draft rejected."

    async def _cmd_approve_all(self, p: dict) -> str:
        body = await self._dash_call("POST", "/api/outbox/approve-all")
        return f"Approved {body.get('approved', 0)} draft(s)."

    async def _cmd_start(self, p: dict) -> str:
        await self._dash_call("POST", "/api/harvey/start")
        return "Harvey started."

    async def _cmd_stop(self, p: dict) -> str:
        await self._dash_call("POST", "/api/harvey/stop")
        return "Harvey stopped."

    async def _cmd_pause_sending(self, p: dict) -> str:
        await self._dash_call("POST", "/api/sending/pause")
        return "Sending paused."

    async def _cmd_resume_sending(self, p: dict) -> str:
        await self._dash_call("POST", "/api/sending/resume")
        return "Sending resumed."

    async def _cmd_run_discover(self, p: dict) -> str:
        provider = p.get("provider")
        if provider not in DISCOVER_PROVIDERS:
            raise CommandError("Only the DataForSEO listings, OpenStreetMap and California contractor-licence (CSLB) searches are allowed.")
        try:
            spend = float(p.get("max_spend"))
        except (TypeError, ValueError):
            raise CommandError("A spending cap is required for a search.")
        if not 0 < spend <= MAX_DISCOVER_SPEND:
            raise CommandError(f"The spending cap must be above $0 and at most ${MAX_DISCOVER_SPEND:g}.")
        body: dict[str, Any] = {"provider": provider, "max_spend": spend}
        cities = p.get("cities")
        if cities:
            if not isinstance(cities, list) or not all(isinstance(c, str) for c in cities):
                raise CommandError("Cities must be a list of names.")
            body["cities"] = cities
        res = await self._dash_call("POST", "/api/discover/run", json=body)
        return f"Search started ({res.get('queries', '?')} queries, spend capped at ${spend:g})."

    async def _cmd_edit_draft(self, p: dict) -> str:
        oid = self._need(p, "outbox_id")
        subject, body = p.get("subject"), p.get("body")
        if not isinstance(subject, str) or not subject.strip() \
                or not isinstance(body, str) or not body.strip():
            raise CommandError("Both a subject and a body are required.")

        def _do() -> str:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                cur = conn.execute(
                    "UPDATE outbox SET subject = ?, body = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'pending_review'",
                    (subject.strip(), body.strip(),
                     datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), oid))
                conn.commit()
                if cur.rowcount:
                    return "ok"
                row = conn.execute("SELECT status FROM outbox WHERE id = ?", (oid,)).fetchone()
                return f"status:{row[0]}" if row else "missing"
            finally:
                conn.close()

        out = await asyncio.to_thread(_do)
        if out == "ok":
            return "Draft updated."
        if out == "missing":
            raise CommandError("That draft no longer exists.")
        raise CommandError(
            f"Only drafts waiting for review can be edited; this one is '{out[7:]}'.")

    async def _cmd_update_config(self, p: dict) -> str:
        patch = p.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise CommandError("There was no change to apply.")
        path = Path(self._config_path())
        try:
            current = yaml.safe_load(path.read_text()) or {}
        except Exception as e:
            raise CommandError(f"Couldn't read Harvey's settings file: {e}")

        cur_postal = ((current.get("compliance") or {}).get("postal_address") or "")
        if "compliance" in patch:
            raise CommandError("The postal address and opt-out wording can't be changed from here.")
        if "postal_address" in patch and patch["postal_address"] != cur_postal:
            raise CommandError("The postal address can't be changed from here.")
        pe = (patch.get("persona") or {}).get("email") if isinstance(patch.get("persona"), dict) else None
        if pe is not None and pe != (current.get("persona") or {}).get("email"):
            raise CommandError("The sender email address can't be changed from here.")

        patch = {k: v for k, v in patch.items() if k != "postal_address"}
        if isinstance(patch.get("persona"), dict):
            patch["persona"] = {k: v for k, v in patch["persona"].items() if k != "email"}
        yaml_patch = translate_patch(patch)
        unknown = set(yaml_patch) - set(HarveyConfig.model_fields)
        if unknown:
            raise CommandError(f"Unknown setting(s): {', '.join(sorted(unknown))}.")

        merged = deep_merge(current, yaml_patch)
        try:
            HarveyConfig(**merged)
        except Exception as e:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                for err in getattr(e, "errors", lambda: [])()) or str(e)
            raise CommandError(f"Harvey's settings check rejected that: {msgs}"[:400])

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.backup_dir / f"{path.name}.{stamp}.bak"
        backup.write_text(path.read_text())
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cfg-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True,
                               default_flow_style=False, width=100)
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

        status = await self._dash_status()
        if status and status.get("running"):
            await self._dash_call("POST", "/api/harvey/stop")
            await self._dash_call("POST", "/api/harvey/start")
            return "Settings saved and Harvey restarted with them."
        return "Settings saved. Harvey isn't running, so they apply the next time it starts."


# ── Entry point ──


def _credentials() -> tuple[str, str]:
    file_env = dotenv_values(PROJECT_ROOT / ".env")
    get = lambda k: (os.environ.get(k) or file_env.get(k) or "").strip()  # noqa: E731
    return get("SUPABASE_URL"), get("SUPABASE_SERVICE_ROLE_KEY")


async def bridge_loop(bridge: Bridge, *, stop: asyncio.Event | None = None,
                      command_interval: float = COMMAND_INTERVAL,
                      snapshot_interval: float = SNAPSHOT_INTERVAL):
    stop = stop or asyncio.Event()
    next_push = 0.0
    next_suppression = 0.0
    failures = 0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            await bridge.drain_commands()
            if loop.time() >= next_suppression:
                try:
                    logger.info("customer list: %s", await bridge.refresh_suppression())
                    next_suppression = loop.time() + SUPPRESSION_INTERVAL
                except Exception as e:
                    logger.warning("customer list refresh failed: %s", e)
                    next_suppression = loop.time() + 300
            if loop.time() >= next_push:
                counts = await bridge.push_snapshot()
                logger.info("bridge push: %s", counts)
                copied = await bridge.write_back_emails()
                if copied:
                    logger.info("HQ contact write-back: %s", copied)
                next_push = loop.time() + snapshot_interval
            failures = 0
            delay = command_interval
        except Exception as e:
            failures += 1
            delay = min(command_interval * 2 ** failures, 300)
            logger.warning("bridge error (retry in %ds): %s", delay, e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def run_bridge():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    url, key = _credentials()
    if not url or not key:
        print("  SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env.")
        sys.exit(2)

    async def _main():
        bridge = Bridge(supabase_url=url, service_key=key)
        try:
            await bridge_loop(bridge)
        finally:
            await bridge.aclose()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
