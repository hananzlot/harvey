"""Round pace: configurable companies per round, parallel site reads,
daily re-check of unconfirmed emails, and contact-buffer scouting."""
import asyncio
from types import SimpleNamespace

import pytest

from harvey.agents import scout as scout_mod
from harvey.agents.scout import Scout
from harvey.config import UsageConfig
from harvey.models.prospect import Prospect
from harvey.state import StateManager


def test_usage_defaults_keep_upstream_behaviour():
    u = UsageConfig()
    assert (u.companies_per_cycle, u.contact_buffer, u.daily_rechecks) == (10, 0, 0)


def test_companies_per_cycle_bounds():
    with pytest.raises(ValueError):
        UsageConfig(companies_per_cycle=0)
    with pytest.raises(ValueError):
        UsageConfig(companies_per_cycle=101)


def _scout(state, usage):
    s = Scout.__new__(Scout)
    s.state = state
    s.env = SimpleNamespace()
    s.config = SimpleNamespace(usage=usage, icp=SimpleNamespace(industries=["Roofing"]))
    return s


def test_backfill_reads_sites_in_parallel_and_honours_round_size(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    calls = []

    async def companies_needing_prospects(limit):
        calls.append(limit)
        return [{"id": f"c{i}", "name": f"Co {i}", "domain": f"co{i}.com", "source": "discover:cslb"}
                for i in range(limit)]

    running = {"now": 0, "peak": 0}

    async def contacts(self, **kw):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        return [Prospect(first_name="", last_name="", title="Owner", email=f"office@{kw['domain']}",
                         company=kw["company_name"], company_id=kw["company_id"], contact_kind="business")]

    added = []
    state.companies_needing_prospects = companies_needing_prospects
    async def mark(_): return None
    state.mark_prospects_checked = mark
    async def add(p): added.append(p)
    state.add_prospect = add
    monkeypatch.setattr(Scout, "_contacts_from_company", contacts)

    s = _scout(state, UsageConfig(companies_per_cycle=12))
    n = asyncio.run(s._prospect_via_known_companies())
    assert calls == [12]
    assert n == 12 and len(added) == 12
    assert 1 < running["peak"] <= scout_mod.BACKFILL_CONCURRENCY


def test_recheck_runs_once_a_day_and_upgrades_decided(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    for i, st in enumerate(["guess", "guess", "verified"]):
        asyncio.run(state.add_prospect(Prospect(
            first_name="", last_name="", title="Owner", email=f"a{i}@x{i}.com",
            company="X", contact_kind="business", email_status=st)))

    seen = []

    async def fake_verify(email, env=None, state=None):
        seen.append(email)
        return SimpleNamespace(email=email, status="verified" if email.startswith("a0") else "guess")
    monkeypatch.setattr(scout_mod, "verify_address", fake_verify)

    s = _scout(state, UsageConfig(daily_rechecks=5))
    assert asyncio.run(s._recheck_unconfirmed()) == 0
    assert sorted(seen) == ["a0@x0.com", "a1@x1.com"]
    left = asyncio.run(state.prospects_with_email_status("guess"))
    assert [r["email"] for r in left] == ["a1@x1.com"]
    asyncio.run(s._recheck_unconfirmed())          # same day: no second pass
    assert len(seen) == 2


def test_recheck_off_by_default(tmp_path):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    assert asyncio.run(_scout(state, UsageConfig())._recheck_unconfirmed()) == 0
