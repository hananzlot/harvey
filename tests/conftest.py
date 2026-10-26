

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _no_link_tracking(monkeypatch):
    """Tests never call the live Supabase link RPC."""
    monkeypatch.setenv("HARVEY_TRACK_LINKS", "0")


@_pytest.fixture(autouse=True)
def _fresh_customer_list(tmp_path, monkeypatch):
    """Sending fails closed without a fresh iBuildPro customer list; give
    every test an empty, fresh one (tests that need entries write their own)."""
    from harvey import suppression
    path = tmp_path / "customer_suppression_default.json"
    suppression.write_list([], [], path=path)
    monkeypatch.setattr(suppression, "SUPPRESSION_PATH", path)
