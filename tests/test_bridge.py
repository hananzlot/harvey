"""Tests for the Supabase bridge (fake HTTP transport + temp SQLite, no network)."""

import asyncio
import json
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
import yaml

from harvey import bridge as bridge_mod
from harvey.bridge import Bridge, deep_merge
from harvey.state import StateManager

CONFIG = {
    "persona": {"name": "Chanan", "company": "iBuildPro", "role": "Founder",
                "email": "sales@example.com", "linkedin": "", "tone": "plain"},
    "product": {"name": "iBP", "description": "CRM", "pricing": "$99",
                "key_benefits": ["fast"], "objection_responses": {},
                "offer": {"primary": "demo", "entry": "free", "goal": "book_call"}},
    "icp": {"industries": ["roofing"], "company_size": "1-10", "titles": ["Owner"],
            "geography": ["Denver, CO"], "geo_coordinates": {"Denver, CO": "39.7,-104.9,50"}},
    "channels": {"email": {"max_daily_sends": 20, "require_approval": True}},
    "compliance": {"postal_address": "1 Main St, Denver CO"},
}


class Fake:
    """In-memory Supabase PostgREST + Harvey dashboard."""

    def __init__(self):
        self.records: dict[tuple[str, str], dict] = {}
        self.commands: list[dict] = []
        self.dash_calls: list[tuple[str, str, dict | None]] = []
        self.dash_running = False
        self.dash_replies: dict[str, tuple[int, dict]] = {}
        self.upsert_batches: list[int] = []
        self.offline = False
        self.rpc_calls: list[dict] = []
        self.rpc_reply = "updated"

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = urlparse(str(req.url))
        if u.hostname == "127.0.0.1":
            return self.dashboard(req, u)
        if self.offline:
            raise httpx.ConnectError("down")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.replace("/rest/v1", "")
        if path == "/harvey_records":
            kind = q.get("kind", "").removeprefix("eq.")
            if req.method == "GET":
                rows = [{"external_id": e} for (k, e) in self.records if k == kind]
                return httpx.Response(200, json=rows)
            if req.method == "POST":
                rows = json.loads(req.content)
                self.upsert_batches.append(len(rows))
                for r in rows:
                    self.records[(r["kind"], r["external_id"])] = r
                return httpx.Response(201)
            if req.method == "DELETE":
                ids = re.findall(r'"([^"]*)"', unquote(q["external_id"]))
                for i in ids:
                    self.records.pop((kind, i), None)
                return httpx.Response(204)
        if path == "/rpc/harvey_record_contact_email":
            body = json.loads(req.content)
            self.rpc_calls.append(body)
            return httpx.Response(200, json=self.rpc_reply)
        if path == "/harvey_commands":
            if req.method == "GET":
                return httpx.Response(200, json=[c for c in self.commands
                                                 if c["status"] == "pending"])
            if req.method == "PATCH":
                cid = q["id"].removeprefix("eq.")
                for c in self.commands:
                    if c["id"] == cid:
                        c.update(json.loads(req.content))
                return httpx.Response(204)
        return httpx.Response(404)

    def dashboard(self, req, u):
        body = json.loads(req.content) if req.content else None
        self.dash_calls.append((req.method, u.path, body))
        if u.path in self.dash_replies:
            code, data = self.dash_replies[u.path]
            return httpx.Response(code, json=data)
        if u.path == "/api/harvey/status":
            return httpx.Response(200, json={"running": self.dash_running,
                                             "started_at": "2026-10-25T10:00:00"})
        if u.path == "/api/harvey/start":
            self.dash_running = True
        if u.path == "/api/harvey/stop":
            self.dash_running = False
        if u.path == "/api/outbox/approve-all":
            return httpx.Response(200, json={"success": True, "approved": 3})
        return httpx.Response(200, json={"success": True})

    def cmd(self, kind, payload=None, cid=None):
        c = {"id": cid or f"c{len(self.commands)+1}", "kind": kind,
             "payload": payload or {}, "status": "pending"}
        self.commands.append(c)
        return c


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    cfg = tmp_path / "harvey.local.yaml"
    cfg.write_text(yaml.safe_dump(CONFIG))
    fake = Fake()
    b = Bridge(supabase_url="https://x.supabase.co", service_key="k", db_path=db,
               config_path=cfg, state_path=tmp_path / "bridge_state.json",
               backup_dir=tmp_path / "backups", transport=httpx.MockTransport(fake.handler),
               host="studio")
    return SimpleEnv(b, fake, db, cfg, tmp_path)


class SimpleEnv:
    def __init__(self, b, fake, db, cfg, tmp):
        self.b, self.fake, self.db, self.cfg, self.tmp = b, fake, db, cfg, tmp

    def sql(self, q, *a):
        c = sqlite3.connect(self.db)
        c.execute(q, a)
        c.commit()
        c.close()

    def one(self, q, *a):
        c = sqlite3.connect(self.db)
        r = c.execute(q, a).fetchone()
        c.close()
        return r

    def seed(self):
        self.sql("INSERT INTO companies (id,name,domain,location,signals_json) VALUES "
                 "('co1','Acme Roofing','acme.com','Denver','[{\"type\":\"hiring\"},{\"type\":\"hiring\"}]')")
        self.sql("INSERT INTO prospects (id,company_id,first_name,last_name,email,title,company,status,score)"
                 " VALUES ('p1','co1','Ann','Lee','ann@acme.com','Owner','Acme Roofing','new',7)")
        self.sql("INSERT INTO outbox (id,prospect_id,conversation_id,to_email,subject,body,status)"
                 " VALUES ('o1','p1','cv1','ann@acme.com','hi','Hello Ann','pending_review')")
        self.sql("INSERT INTO conversations (id,prospect_id,thread_json,status) VALUES "
                 "('cv1','p1','[{\"sender\":\"harvey\",\"content\":\"hi\",\"timestamp\":\"2026-10-24T10:00:00\"},"
                 "{\"sender\":\"prospect\",\"content\":\"yes\",\"timestamp\":\"2026-10-24T11:00:00\"}]','open')")
        self.sql("INSERT INTO actions (id,action_type,agent,details_json,created_at) VALUES "
                 "('a1','prospect','scout','{\"prospects_found\": 4}','2026-10-24 09:00:00')")

    def push(self):
        return asyncio.run(self.b.push_snapshot())

    def run(self, coro):
        return asyncio.run(coro)

    def data(self, kind, eid):
        return self.fake.records[(kind, eid)]["data"]


# ── snapshot ──


def test_snapshot_shapes(env):
    env.seed()
    env.fake.dash_running = True
    env.push()
    st = env.data("status", "current")
    assert st["running"] is True and st["host"] == "studio"
    assert st["version"] == "v0.2.1+ibuildpro"
    assert st["daily_cap"] == 20 and st["require_approval"] is True
    assert st["prospects_by_status"] == {"new": 1}
    assert st["outbox_by_status"] == {"pending_review": 1}
    assert st["replies_total"] == 1 and st["companies_total"] == 1
    assert st["quiet_hours"]["timezone"] == "America/New_York"
    ob = env.data("outbox", "o1")
    assert ob["body"] == "Hello Ann"
    assert "1 Main St, Denver CO" in ob["footer"] and "iBuildPro" in ob["footer"]
    assert ob["prospect_name"] == "Ann Lee" and ob["company_name"] == "Acme Roofing"
    cv = env.data("conversation", "cv1")
    assert [m["sender"] for m in cv["thread"]] == ["harvey", "prospect"]
    assert env.data("company", "co1")["signals"] == ["hiring"]
    assert env.data("prospect", "p1")["company_name"] == "Acme Roofing"
    assert "scout" in env.data("activity", "a1")["summary"]
    cfg = env.data("config", "current")
    assert cfg["sending"]["max_daily_sends"] == 20
    assert cfg["postal_address"] == "1 Main St, Denver CO"
    assert cfg["guidance"] == ""


def test_change_only_upsert_and_heartbeat(env):
    env.seed()
    first = env.push()
    assert first["upserted"] > 3
    hb1 = env.data("status", "current")["bridge_heartbeat_at"]
    env.fake.upsert_batches.clear()
    second = env.push()
    # only the status row (heartbeat) changed
    assert second["upserted"] == 1 and env.fake.upsert_batches == [1]
    assert env.data("status", "current")["bridge_heartbeat_at"] >= hb1
    env.sql("UPDATE outbox SET subject='new' WHERE id='o1'")
    assert env.push()["upserted"] == 2
    assert env.data("outbox", "o1")["subject"] == "new"


def test_batches_capped_at_500(env):
    c = sqlite3.connect(env.db)
    c.executemany("INSERT INTO companies (id,name) VALUES (?,?)",
                  [(f"c{i}", f"n{i}") for i in range(1203)])
    c.commit()
    c.close()
    env.push()
    assert max(env.fake.upsert_batches) <= 500
    assert sum(1 for k in env.fake.records if k[0] == "company") == 1203


def test_deletion_of_vanished_rows(env):
    env.seed()
    env.push()
    env.sql("DELETE FROM outbox WHERE id='o1'")
    env.sql("DELETE FROM companies WHERE id='co1'")
    out = env.push()
    assert out["deleted"] == 2
    assert ("outbox", "o1") not in env.fake.records
    assert ("company", "co1") not in env.fake.records
    assert ("prospect", "p1") in env.fake.records


def test_remote_orphans_removed_after_restart(env):
    env.fake.records[("prospect", "ghost")] = {"data": {}}
    env.push()
    assert ("prospect", "ghost") not in env.fake.records


def test_activity_capped_at_newest_500(env):
    c = sqlite3.connect(env.db)
    c.executemany("INSERT INTO actions (id,action_type,agent,details_json,created_at) VALUES (?,?,?,?,?)",
                  [(f"x{i:04d}", "t", "a", "{}", f"2026-10-01 00:{i // 60:02d}:{i % 60:02d}")
                   for i in range(520)])
    c.commit()
    c.close()
    env.push()
    ids = {e for (k, e) in env.fake.records if k == "activity"}
    assert len(ids) == 500
    assert "x0519" in ids and "x0000" not in ids
    # an older row falling out of the window is deleted remotely too
    env.sql("INSERT INTO actions (id,action_type,agent,details_json,created_at) VALUES "
            "('new','t','a','{}','2026-11-01 00:00:00')")
    env.push()
    ids = {e for (k, e) in env.fake.records if k == "activity"}
    assert len(ids) == 500 and "new" in ids and "x0020" not in ids


def test_network_error_does_not_crash_loop(env):
    env.fake.offline = True

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(bridge_mod.bridge_loop(
            env.b, stop=stop, command_interval=0.01, snapshot_interval=0.01))
        await asyncio.sleep(0.1)
        assert not t.done()
        stop.set()
        await t
    asyncio.run(go())


# ── commands ──


def drain(env):
    return env.run(env.b.drain_commands())


def test_approve_reject_success_and_failure(env):
    c1 = env.fake.cmd("approve", {"outbox_id": "o1"})
    c2 = env.fake.cmd("reject", {"outbox_id": "o2"})
    c3 = env.fake.cmd("approve", {})
    env.fake.dash_replies["/api/outbox/o9/approve"] = (200, {"success": False})
    c4 = env.fake.cmd("approve", {"outbox_id": "o9"})
    assert drain(env) == 4
    assert c1["status"] == "done" and ("POST", "/api/outbox/o1/approve", None) in env.fake.dash_calls
    assert c2["status"] == "done" and any(p == "/api/outbox/o2/reject" for _, p, _ in env.fake.dash_calls)
    assert c3["status"] == "failed" and "outbox_id" in c3["result"]
    assert c4["status"] == "failed" and c4["processed_at"]


def test_simple_dashboard_commands(env):
    cmds = {k: env.fake.cmd(k) for k in
            ("approve_all", "start", "stop", "pause_sending", "resume_sending")}
    drain(env)
    assert all(c["status"] == "done" for c in cmds.values())
    assert "3" in cmds["approve_all"]["result"]
    paths = [p for _, p, _ in env.fake.dash_calls]
    for p in ("/api/outbox/approve-all", "/api/harvey/start", "/api/harvey/stop",
              "/api/sending/pause", "/api/sending/resume"):
        assert p in paths


def test_dashboard_down_fails_politely(env):
    def boom(req):
        if req.url.host == "127.0.0.1":
            raise httpx.ConnectError("refused")
        return env.fake.handler(req)
    env.b._dash = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    c = env.fake.cmd("start")
    drain(env)
    assert c["status"] == "failed" and "dashboard" in c["result"]


def test_unknown_command_fails(env):
    c = env.fake.cmd("rm_rf")
    drain(env)
    assert c["status"] == "failed" and "Unknown" in c["result"]


def test_run_discover_limits(env):
    ok = env.fake.cmd("run_discover", {"provider": "osm", "max_spend": 5, "cities": ["Denver, CO"]})
    too_much = env.fake.cmd("run_discover", {"provider": "osm", "max_spend": 5.01})
    bad_provider = env.fake.cmd("run_discover", {"provider": "hunter", "max_spend": 1})
    no_cap = env.fake.cmd("run_discover", {"provider": "osm"})
    drain(env)
    assert ok["status"] == "done"
    call = [c for c in env.fake.dash_calls if c[1] == "/api/discover/run"]
    assert len(call) == 1 and call[0][2]["cities"] == ["Denver, CO"]
    for c in (too_much, bad_provider, no_cap):
        assert c["status"] == "failed"


def test_run_discover_busy(env):
    env.fake.dash_replies["/api/discover/run"] = (409, {"success": False, "message": "a run is already going"})
    c = env.fake.cmd("run_discover", {"provider": "osm", "max_spend": 1})
    drain(env)
    assert c["status"] == "failed" and "already going" in c["result"]


def test_edit_draft(env):
    env.seed()
    ok = env.fake.cmd("edit_draft", {"outbox_id": "o1", "subject": "S2", "body": "B2"})
    drain(env)
    assert ok["status"] == "done"
    assert env.one("SELECT subject, body FROM outbox WHERE id='o1'") == ("S2", "B2")

    env.sql("UPDATE outbox SET status='approved' WHERE id='o1'")
    bad = env.fake.cmd("edit_draft", {"outbox_id": "o1", "subject": "S3", "body": "B3"})
    missing = env.fake.cmd("edit_draft", {"outbox_id": "nope", "subject": "a", "body": "b"})
    empty = env.fake.cmd("edit_draft", {"outbox_id": "o1", "subject": "", "body": "b"})
    drain(env)
    assert bad["status"] == "failed" and "approved" in bad["result"]
    assert env.one("SELECT subject FROM outbox WHERE id='o1'") == ("S2",)
    assert missing["status"] == "failed" and empty["status"] == "failed"


def test_update_config_merges_validates_backs_up(env):
    c = env.fake.cmd("update_config", {"patch": {
        "guidance": "Lead with the free trial",
        "icp": {"industries": ["roofing", "solar"]},
        "sending": {"max_daily_sends": 10, "quiet_hours": {"start": "21:00", "end": "08:00",
                                                           "timezone": "America/Denver"}},
        "persona": {"tone": "warm"},
    }})
    drain(env)
    assert c["status"] == "done", c["result"]
    new = yaml.safe_load(env.cfg.read_text())
    assert new["guidance"] == "Lead with the free trial"
    assert new["icp"]["industries"] == ["roofing", "solar"]
    assert new["icp"]["titles"] == ["Owner"]  # untouched keys survive
    assert new["channels"]["email"]["max_daily_sends"] == 10
    assert new["channels"]["email"]["require_approval"] is True
    assert new["usage"]["quiet_hours"]["timezone"] == "America/Denver"
    assert new["persona"]["tone"] == "warm" and new["persona"]["email"] == "sales@example.com"
    assert len(list((env.tmp / "backups").iterdir())) == 1
    assert "isn't running" in c["result"]


def test_update_config_restarts_when_running(env):
    env.fake.dash_running = True
    c = env.fake.cmd("update_config", {"patch": {"guidance": "x"}})
    drain(env)
    assert c["status"] == "done" and "restarted" in c["result"]
    paths = [p for _, p, _ in env.fake.dash_calls]
    assert paths.index("/api/harvey/stop") < paths.index("/api/harvey/start")


def test_update_config_refuses_protected_fields(env):
    before = env.cfg.read_text()
    a = env.fake.cmd("update_config", {"patch": {"postal_address": "9 Elsewhere Rd"}})
    b = env.fake.cmd("update_config", {"patch": {"persona": {"email": "evil@example.com"}}})
    c = env.fake.cmd("update_config", {"patch": {"compliance": {"opt_out_line": ""}}})
    drain(env)
    for cmd in (a, b, c):
        assert cmd["status"] == "failed", cmd
    assert env.cfg.read_text() == before
    assert not (env.tmp / "backups").exists()


def test_update_config_unchanged_protected_values_are_ignored(env):
    c = env.fake.cmd("update_config", {"patch": {
        "postal_address": "1 Main St, Denver CO",
        "persona": {"email": "sales@example.com", "tone": "calm"}}})
    drain(env)
    assert c["status"] == "done", c["result"]


def test_update_config_validation_failure_leaves_file(env):
    before = env.cfg.read_text()
    bad = env.fake.cmd("update_config", {"patch": {"sending": {"max_daily_sends": -5}}})
    unknown = env.fake.cmd("update_config", {"patch": {"bogus": 1}})
    drain(env)
    assert bad["status"] == "failed" and unknown["status"] == "failed"
    assert env.cfg.read_text() == before


def test_result_write_failure_does_not_rerun_command(env):
    c = env.fake.cmd("start")
    real = env.fake.handler
    state = {"fail": True}

    def flaky(req):
        if req.method == "PATCH" and state["fail"]:
            state["fail"] = False
            return httpx.Response(500, text="boom")
        return real(req)
    env.b._sb = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    with pytest.raises(RuntimeError):
        drain(env)
    drain(env)
    assert c["status"] == "done"
    assert sum(1 for _, p, _ in env.fake.dash_calls if p == "/api/harvey/start") == 1


def test_deep_merge():
    assert deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 9}, "l": [2]}) == \
        {"a": {"b": 9, "c": 2}, "l": [2]}


def test_guidance_reaches_prompts():
    from types import SimpleNamespace
    from harvey.agents.writer import Writer
    w = Writer.__new__(Writer)
    w.config = SimpleNamespace(guidance="  Lead with price  ")
    assert w._guidance_block() == "\n\nDirection from the owner: Lead with price"
    w.config = SimpleNamespace(guidance="")
    assert w._guidance_block() == ""


def test_local_iso_attaches_machine_offset_to_naive_local_time():
    from datetime import datetime
    from harvey.bridge import _local_iso
    out = _local_iso("2026-10-25T12:10:02.893702")
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == datetime(2026, 10, 25, 12, 10, 2, 893702)


def test_dashboard_start_launches_the_run_subcommand():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("harvey/dashboard.py").read_text()
    assert '[sys.executable, "-m", "harvey", "run"]' in src


# ── HQ contact write-back ──


def _seed_cslb(env, status="verified", email="office@acme.com"):
    env.sql("INSERT INTO companies (id,name,domain,external_id) VALUES "
            "('cs1','Acme Roofing','acmeroofing.com','cslb:778739')")
    env.sql("INSERT INTO prospects (id,company_id,first_name,last_name,email,email_status,title,company)"
            " VALUES ('pc1','cs1','','',?,?,'Owner','Acme Roofing')", email, status)


def test_writeback_sends_licence_email_once(env):
    _seed_cslb(env)
    assert env.run(env.b.write_back_emails()) == {"updated": 1}
    assert env.fake.rpc_calls == [{"p_license_number": "778739", "p_email": "office@acme.com",
                                   "p_email_status": "verified",
                                   "p_website": "https://acmeroofing.com"}]
    assert env.run(env.b.write_back_emails()) == {}
    assert len(env.fake.rpc_calls) == 1
    assert env.b.hq_emails_copied() == 1


def test_writeback_skips_unchecked_and_non_licence_rows(env):
    env.seed()
    _seed_cslb(env, status="guess")
    assert env.run(env.b.write_back_emails()) == {}
    assert env.fake.rpc_calls == []


def test_writeback_resends_when_email_changes_and_counts_results(env):
    _seed_cslb(env)
    env.fake.rpc_reply = "kept_existing"
    assert env.run(env.b.write_back_emails()) == {"kept_existing": 1}
    assert env.b.hq_emails_copied() == 0
    env.sql("UPDATE prospects SET email='owner@acme.com' WHERE id='pc1'")
    env.fake.rpc_reply = "updated"
    assert env.run(env.b.write_back_emails()) == {"updated": 1}
    assert env.fake.rpc_calls[-1]["p_email"] == "owner@acme.com"


def test_status_reports_hq_emails_copied(env):
    _seed_cslb(env)
    env.run(env.b.write_back_emails())
    env.push()
    assert env.data("status", "current")["hq_emails_copied"] == 1
