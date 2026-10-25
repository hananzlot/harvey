"""Gmail reply polling must stay inside the persona's own mail."""
from types import SimpleNamespace

from harvey.integrations.gmail import GmailProvider


def _provider(email):
    p = GmailProvider.__new__(GmailProvider)
    p.config = SimpleNamespace(persona=SimpleNamespace(email=email))
    return p


def test_query_limited_to_persona_address_and_bounces():
    q = _provider("crm_sales@ibuildpro.ai")._reply_query()
    assert q.startswith("in:inbox -from:me newer_than:7d ")
    assert "{to:crm_sales@ibuildpro.ai from:mailer-daemon from:postmaster}" in q


def test_query_without_persona_address_keeps_default():
    assert _provider("")._reply_query() == "in:inbox -from:me newer_than:7d"
