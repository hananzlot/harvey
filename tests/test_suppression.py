"""Existing iBuildPro customers are never emailed."""
import asyncio
import os
import time
from types import SimpleNamespace

from harvey import suppression
from harvey.agents import sender as sender_mod
from harvey.agents.sender import Sender
from harvey.models.prospect import Prospect
from harvey.state import StateManager


def _list(tmp_path, monkeypatch, emails=(), domains=()):
    path = tmp_path / "customer_suppression.json"
    suppression.write_list(list(emails), list(domains), path=path)
    monkeypatch.setattr(suppression, "SUPPRESSION_PATH", path)
    return path


def test_is_customer_by_exact_email_and_business_domain(tmp_path, monkeypatch):
    path = _list(tmp_path, monkeypatch, emails=["owner@gmail.com"], domains=["acmeroofing.com"])
    assert suppression.is_customer("Owner@Gmail.com", path)
    assert not suppression.is_customer("someone@gmail.com", path)
    assert suppression.is_customer("office@acmeroofing.com", path)
    assert suppression.is_customer("bob@mail.acmeroofing.com", path)
    assert not suppression.is_customer("office@notacme.com", path)
    assert not suppression.is_customer("", path)


def test_add_prospect_marks_customer(tmp_path, monkeypatch):
    _list(tmp_path, monkeypatch, domains=["acmeroofing.com"])
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    asyncio.run(state.add_prospect(Prospect(first_name="", last_name="", title="Owner",
                                            company="Acme", email="office@acmeroofing.com",
                                            contact_kind="business")))
    assert [p.status for p in asyncio.run(state.get_prospects_by_status("customer"))] == ["customer"]
    assert asyncio.run(state.get_prospects_by_status("new")) == []


def test_suppression_problem_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(suppression, "SUPPRESSION_PATH", tmp_path / "missing.json")
    assert "not been downloaded" in Sender._suppression_problem()
    path = _list(tmp_path, monkeypatch, domains=["x.com"])
    assert Sender._suppression_problem() == ""
    old = time.time() - (sender_mod.SUPPRESSION_MAX_AGE_HOURS + 1) * 3600
    os.utime(path, (old, old))
    assert "hours old" in Sender._suppression_problem()


def test_customer_sweep_retires_contact_and_cancels_drafts(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    pid = asyncio.run(state.add_prospect(Prospect(first_name="Ann", last_name="Lee", title="Owner",
                                                  company="Acme", email="ann@acmeroofing.com")))
    _list(tmp_path, monkeypatch, domains=["acmeroofing.com"])
    s = Sender.__new__(Sender)
    s.state = state
    assert asyncio.run(s._suppress_customers()) == 1
    assert asyncio.run(state.get_prospect(pid)).status == "customer"
    assert "customer" in sender_mod.STOP_STATUSES
