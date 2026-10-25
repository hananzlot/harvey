"""CSLB licence list, website finder, business-address contacts, greetings.

Nothing here touches the network, the live database or a paid API.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.agents.scout import Scout
from harvey.agents.sender import Sender
from harvey.bridge import DISCOVER_PROVIDERS
from harvey.collectors import cslb
from harvey.collectors import discover as D
from harvey.collectors import website_finder as W
from harvey.config import load_config
from harvey.gate import pre_send_check
from harvey.integrations import site_contacts as SC
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

FIXTURE = Path(__file__).parent / "fixtures" / "cslb_sample.xlsx"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)


# ── xls(x) parsing ──


def test_parse_workbook_reads_every_row_and_maps_fields():
    lics = cslb.parse_workbook(FIXTURE.read_bytes())
    assert len(lics) == 6
    first = lics[0]
    assert first.number == "1055445"
    assert first.name == "Rainbow Construction Group"
    assert first.address == "1200 Dewey Ave, Los Angeles, CA 90006"
    assert first.phone == "(310) 243 6336"
    assert first.county == "Los Angeles"
    assert first.classifications == ("A", "B", "C12", "C39")
    assert first.is_active()


def test_active_filter_and_classification_match():
    lics = cslb.parse_workbook(FIXTURE.read_bytes())
    active_roofing = [l for l in lics if l.is_active() and l.holds("C-39")]
    assert sorted(l.number for l in active_roofing) == ["1055445", "1084553", "790599"]
    expired = next(l for l in lics if l.number == "900002")
    assert not expired.is_active()
    solar = [l for l in lics if l.is_active() and l.holds("C-46")]
    assert [l.number for l in solar] == ["900001"]


def test_pretty_name():
    assert cslb.pretty_name("F D L ROOFING INC") == "F D L Roofing Inc"
    assert cslb.pretty_name("SUNNY SIDE SOLAR LLC") == "Sunny Side Solar LLC"


def test_parse_rejects_non_workbooks():
    with pytest.raises(cslb.CslbError):
        cslb.parse_workbook(b"<html>blocked</html>")
    with pytest.raises(cslb.CslbError):
        cslb.parse_workbook(b"\xd0\xcf\x11\xe0legacy")


# ── classification / county mapping ──


def test_classification_mapping_defaults_and_override():
    got = cslb.classifications_for(["Roofing contractor", "Solar installer", "Bakery"])
    assert got == [("Roofing contractor", "C-39"), ("Solar installer", "C-46")]
    over = cslb.classifications_for(["Plumber"], {"plumb": "C-36"})
    assert over == [("Plumber", "C-36")]


def test_counties_configured_derived_and_default():
    assert cslb.resolve_counties(["orange county", "San Diego"]) == ["Orange", "San Diego"]
    assert cslb.resolve_counties([], ["Los Angeles, CA", "Inland Empire, CA"]) == [
        "Los Angeles", "Riverside", "San Bernardino"]
    assert cslb.resolve_counties([], []) == cslb.DEFAULT_COUNTIES
    with pytest.raises(ValueError):
        cslb.resolve_counties(["Narnia"])
    assert cslb.county_codes(["Los Angeles", "Orange", "Riverside",
                              "San Bernardino", "San Diego"]) == ["19", "30", "33", "36", "37"]


def test_cslb_queries_from_config():
    config = load_config()
    config.icp.counties = ["Los Angeles", "Orange"]
    qs = D.build_queries(config, provider="cslb")
    assert [q.category for q in qs] == ["C-39", "C-46"]
    assert all(q.counties == ["Los Angeles", "Orange"] for q in qs)
    # --city names counties for this provider
    qs = D.build_queries(config, cities=["San Diego"], provider="cslb")
    assert qs[0].counties == ["San Diego"]


# ── the form post ──


def _form_page():
    return ('<form><input type="hidden" name="__VIEWSTATE" value="VS&#43;1" />'
            '<input type="hidden" name="__EVENTVALIDATION" value="EV" />'
            '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" /></form>')


@pytest.mark.asyncio
async def test_download_posts_state_classification_and_counties():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, text=_form_page())
        from urllib.parse import parse_qs
        seen.update(parse_qs(req.content.decode()))
        return httpx.Response(200, content=FIXTURE.read_bytes(), headers={
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        data = await cslb.download(c, "C-39", ["Orange", "San Diego"])
    assert data[:2] == b"PK"
    assert seen["__VIEWSTATE"] == ["VS+1"]
    assert seen["__EVENTTARGET"] == [cslb.SEARCH_BUTTON]
    assert seen[cslb.CLASSIFICATION_FIELD] == ["C-39"]
    assert seen[cslb.COUNTY_FIELD] == ["30", "37"]


@pytest.mark.asyncio
async def test_download_reports_a_block_verbatim():
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, text=_form_page())
        return httpx.Response(200, text="<html><body>Access denied. Complete the captcha.</body></html>",
                              headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(cslb.CslbBlocked) as e:
            await cslb.download(c, "C-39", ["Orange"])
    assert "captcha" in str(e.value)


# ── website finder ──


class R:
    def __init__(self, domain, title="", url=""):
        self.domain, self.name, self.website = domain, title, url or f"https://{domain}"


@pytest.mark.parametrize("domain", [
    "yelp.com", "m.yelp.com", "www.bbb.org", "angi.com", "homeadvisor.com",
    "facebook.com", "buildzoom.com", "cslb.ca.gov", "linkedin.com", "nextdoor.com",
    "mapquest.com", "yellowpages.com", "houzz.com", "porch.com", "manta.com",
    "chamberofcommerce.com", "google.com", "maps.apple.com", "bing.com", "thumbtack.com",
])
def test_directories_are_filtered(domain):
    assert W.is_directory(domain)


def test_own_sites_are_not_directories():
    for d in ("rainbowroofing.com", "joes.wixsite.com", "triangleroofing.net"):
        assert not W.is_directory(d)


def test_name_match_and_first_good_result_wins():
    results = [
        R("yelp.com", "Triangle Roofing - San Jacinto - Yelp"),
        R("randomblog.com", "Top 10 roofers"),
        R("triangleroofingca.com", "Triangle Roofing | San Jacinto"),
        R("triangleroofing.net", "Another"),
    ]
    assert W.pick_website("Triangle Roofing", results) == (
        "triangleroofingca.com", "https://triangleroofingca.com")


def test_no_plausible_match_returns_none():
    results = [R("yelp.com"), R("angi.com"), R("completelyunrelated.com", "Plumbing tips")]
    assert W.pick_website("Rainbow Construction Group", results) is None


def test_name_match_uses_the_registered_domain_only():
    assert W.plausible_match("F D L Roofing Inc", "fdlroofing.com")
    assert W.plausible_match("A-1 Roofing", "a1roofing.com")
    assert W.plausible_match("Tecta America Southern California Inc", "tectaamerica.com")
    assert W.plausible_match("Top Choice Roof Tearoff Inc", "topchoiceimprovements.com")
    assert W.plausible_match("Escamilla'S Roofing & Raingutters Inc", "escamillasroofing.com")
    # A page title naming the business no longer counts (a news article or a
    # directory page titles itself with the business name).
    assert not W.plausible_match("Sunny Side Solar LLC", "pvexperts.com", "Sunny Side Solar | San Diego")
    # The real misses of 2026-10-25.
    for name, domain in [("Lemus Roofing", "gaf.com"), ("Marino Bill", "theglobeandmail.com"),
                         ("Myers William Christopher", "usphonebook.com"),
                         ("Lomeli'S Roofing", "zerofi.ai"), ("Starr Roofing", "experience.com"),
                         ("Cardiff By The Sea Roofing Inc", "cardiff-by-the-sea-ca.proaccessroofing.com"),
                         ("Over All Roofing Inc", "overandaboveroofingoc.com"),
                         ("San Marino Roof Co Inc", "birchbarkroofing.com"),
                         ("Mega Roofing Inc", "gtamegaroofing.ca")]:
        assert not W.plausible_match(name, domain), (name, domain)


def test_city_of():
    assert W.city_of("1200 Dewey Ave, Los Angeles, CA 90006") == "Los Angeles"


class FakeSerp:
    """Stands in for the DataForSEO SERP provider; never spends."""
    PAGE_COST = 0.0006

    def __init__(self, answers):
        self.answers, self.calls, self.last_cost = answers, [], 0.0

    def configured(self, env):
        return True

    def estimate(self, queries):
        return self.PAGE_COST

    async def fetch(self, client, env, query):
        self.calls.append(query.keyword())
        self.last_cost = self.PAGE_COST
        return self.answers.get(query.term, [])


async def _seed_cslb_companies(state, n):
    ids = []
    for i in range(n):
        ids.append(await state.add_company(Company(
            name=f"Roofer {i}", source="discover:cslb", external_id=f"cslb:{i}",
            location="1 Main St, San Diego, CA 92101")))
    return ids


@pytest.mark.asyncio
async def test_lookups_respect_the_spend_cap_and_stamp_every_company(state, monkeypatch):
    serp = FakeSerp({"Roofer 0": [R("roofer0.com", "Roofer 0")]})
    monkeypatch.setitem(D.PROVIDERS, "dataforseo_serp", serp)
    await _seed_cslb_companies(state, 10)
    # Room for exactly 3 lookups ($0.0018 / $0.0006)
    res = await W.run_lookups(state, None, {}, "discover:cslb", budget=0.0018)
    assert res["looked_up"] == 3 and res["stopped"] == "budget reached"
    assert res["cost"] == pytest.approx(0.0018)
    assert res["remaining"] == 7
    assert len(serp.calls) == 3 and serp.calls[0].endswith("San Diego CA")
    # Re-run: the three already looked up are not paid for again.
    res2 = await W.run_lookups(state, None, {}, "discover:cslb", budget=0.0006)
    assert res2["looked_up"] == 1 and res2["remaining"] == 6
    found = await state.get_company_by_domain("roofer0.com")
    assert found is not None and found.name == "Roofer 0"


@pytest.mark.asyncio
async def test_domain_already_owned_is_not_stolen(state):
    a, b = await _seed_cslb_companies(state, 2)
    assert await state.set_company_website(a, "same.com", "https://same.com")
    assert not await state.set_company_website(b, "same.com", "https://same.com")
    assert await state.count_companies_needing_website("discover:cslb") == 0


# ── discovery end-to-end (fake download + fake SERP) ──


@pytest.mark.asyncio
async def test_cslb_discovery_saves_active_licences_and_looks_up_sites(state, monkeypatch):
    import harvey.config as hc
    lics = {"C-39": cslb.parse_workbook(FIXTURE.read_bytes())}

    async def fake_download(client, code, counties):
        return FIXTURE.read_bytes()

    monkeypatch.setattr(cslb, "download", fake_download)
    serp = FakeSerp({"Triangle Roofing": [R("triangleroofing.net", "Triangle Roofing")]})
    monkeypatch.setitem(D.PROVIDERS, "dataforseo_serp", serp)
    monkeypatch.setattr(hc, "load_env", lambda: hc.EnvConfig(
        dataforseo_login="x", dataforseo_password="y", dataforseo_sandbox="1"))

    config = load_config()
    config.icp.counties = ["Orange"]
    est = await D.run_discovery(state, config, "cslb", max_spend=1.0, dry_run=True)
    assert est.queries == 2 and 0 < est.estimated_cost <= 1.0

    rep = await D.run_discovery(state, config, "cslb", max_spend=0.003)
    assert rep.errors == []
    # 3 active C-39 + 1 active C-46; expired + plumbing are dropped
    assert rep.new_companies == 4
    assert rep.website_lookups == 4 and rep.actual_cost == pytest.approx(0.0024)
    c = await state.get_company_by_external_id("cslb:790599")
    assert c.source == "discover:cslb" and c.industry == "Roofing contractor"
    assert c.phone == "(800) 360 6771" and c.location.startswith("1392 Enchanted Trails")
    assert c.domain == "triangleroofing.net"
    assert await state.get_company_by_external_id("cslb:900002") is None
    assert await state.get_company_by_external_id("cslb:900003") is None

    again = await D.run_discovery(state, config, "cslb", max_spend=0.003)
    assert again.new_companies == 0 and again.known_companies == 4


# ── email harvesting ──


def test_placeholders_are_rejected():
    for bad in ("example@example.com", "john@example.com", "you@mysite.com",
                "name@domain.com", "abc123@sentry.io", "x@sentry-next.wixpress.com",
                "logo@2x.png", "noreply@acme.com", "no-reply@acme.com",
                "user@yourcompany.com", "webmaster@acme.com"):
        assert SC.is_placeholder(bad), bad
    for good in ("office@acmeroofing.com", "todd.young@gmail.com", "info@acme.com"):
        assert not SC.is_placeholder(good), good


def test_harvest_handles_mailto_obfuscation_and_noise():
    html = ('<a href="mailto:Office@Acme.com?subject=Hi">Email</a> '
            'bob [at] gmail [dot] com <img src="logo@2x.png"> john@example.com '
            '<span>info@acme.com</span>')
    got = SC.harvest_emails(html, ["mailto:Office@Acme.com?subject=Hi"])
    assert got == ["office@acme.com", "bob@gmail.com", "info@acme.com"]


def test_own_domain_preferred_over_freemail_and_third_parties():
    emails = ["owner.todd@gmail.com", "designer@webagency.io", "info@acme.com", "office@acme.com"]
    ranked = SC.rank_business_emails(emails, "acme.com", "Acme Roofing")
    assert set(ranked[:2]) == {"office@acme.com", "info@acme.com"}
    assert ranked[2:] == ["owner.todd@gmail.com"]   # own domain first, free-mail after
    assert SC.rank_business_emails(["only@gmail.com", "x@agency.io"], "acme.com") == ["only@gmail.com"]


def test_owner_name_from_text():
    assert SC.find_owner_name(
        "<p>My name is Todd Gregory Young, owner of Young Roofing.</p>") == ("Todd", "Gregory Young")
    assert SC.find_owner_name("Maria Lopez, Owner") == ("Maria", "Lopez")
    assert SC.find_owner_name("Locally owned and operated by Sam Carter since 1999") == ("Sam", "Carter")
    assert SC.find_owner_name("Free estimates from our family owner operated roofing team") is None


# ── business contacts in the scout ──


def make_scout(state):
    env = SimpleNamespace(reoon_api_key="", zerobounce_api_key="")
    return Scout(brain=None, state=state, config=load_config(), env=env)


def fake_site(pages):
    async def resolve(self, domain, retries=1):
        return f"https://{domain}", None

    async def fetch(self, url, timeout=0, retries=0, **kw):
        for path, html in pages.items():
            if url.rstrip("/") == f"https://acme.com{path}".rstrip("/"):
                return SimpleNamespace(text=html, status_code=200)
        return None
    return resolve, fetch


@pytest.mark.asyncio
async def test_site_with_only_an_office_address_yields_a_business_contact(state, monkeypatch):
    resolve, fetch = fake_site({
        "": "<html>Call us 555-1212</html>",
        "/contact": '<a href="mailto:office@acme.com">office@acme.com</a> sales@example.com',
    })
    monkeypatch.setattr(Scout, "_resolve_base_url", resolve)
    monkeypatch.setattr(Scout, "_fetch", fetch)

    async def verify(email, env=None, state=None):
        return SimpleNamespace(email=email, status="verified")
    import harvey.agents.scout as S
    monkeypatch.setattr(S, "verify_address", verify)

    scout = make_scout(state)
    p = await scout._business_contact("cid", "acme.com", "Acme Roofing", "Roofing")
    assert p.contact_kind == "business" and p.email == "office@acme.com"
    assert p.title == "Owner" and p.first_name == "" and p.last_name == ""
    assert p.email_status == "verified" and p.is_valid()


@pytest.mark.asyncio
async def test_named_owner_on_the_site_becomes_a_person(state, monkeypatch):
    resolve, fetch = fake_site({
        "/about": "<p>My name is Todd Gregory Young, owner of Young Roofing.</p>",
        "": '<a href="mailto:todd@gmail.com">x</a>',
    })
    monkeypatch.setattr(Scout, "_resolve_base_url", resolve)
    monkeypatch.setattr(Scout, "_fetch", fetch)
    import harvey.agents.scout as S

    async def verify(email, env=None, state=None):
        return SimpleNamespace(email=email, status="risky")
    monkeypatch.setattr(S, "verify_address", verify)

    p = await make_scout(state)._business_contact("cid", "acme.com", "Young Roofing", "Roofing")
    assert (p.first_name, p.last_name, p.title) == ("Todd", "Gregory Young", "Owner")
    assert p.contact_kind == "person" and p.email == "todd@gmail.com"


@pytest.mark.asyncio
async def test_nothing_published_means_no_contact(state, monkeypatch):
    resolve, fetch = fake_site({"": "<html>Welcome</html>"})
    monkeypatch.setattr(Scout, "_resolve_base_url", resolve)
    monkeypatch.setattr(Scout, "_fetch", fetch)
    assert await make_scout(state)._business_contact("cid", "acme.com", "Acme", "Roofing") is None


@pytest.mark.asyncio
async def test_backfill_only_falls_back_for_licensed_contractor_sources(state, monkeypatch):
    calls = []

    async def scrape(self, domain):
        return []

    async def biz(self, **kw):
        calls.append(kw["company_id"])
        return None
    monkeypatch.setattr(Scout, "_scrape_team_page", scrape)
    monkeypatch.setattr(Scout, "_business_contact", biz)
    scout = make_scout(state)
    await scout._contacts_from_company("a", "acme.com", "Acme", "Roofing", business_fallback=True)
    await scout._contacts_from_company("b", "acme.com", "Acme", "Roofing", business_fallback=False)
    assert calls == ["a"]
    from harvey.agents.scout import BUSINESS_CONTACT_SOURCES
    assert BUSINESS_CONTACT_SOURCES == ("discover:cslb",)


# ── business-contact validity, storage, greeting, gate ──


def business(**kw):
    base = dict(email="office@acme.com", email_status="verified", title="Owner",
                company="Acme Roofing", company_id="c1", contact_kind="business")
    base.update(kw)
    return Prospect(**base)


def test_business_contact_is_valid_but_a_nameless_person_is_not():
    assert business().is_valid()
    assert not business(email="").is_valid()
    assert not business(title="").is_valid()
    assert not Prospect(first_name="", last_name="", title="Owner", company="Acme",
                        email="a@b.com").is_valid()
    assert business().display_name() == "Owner of Acme Roofing"


@pytest.mark.asyncio
async def test_business_contact_round_trips_through_the_database(state):
    pid = await state.add_prospect(business())
    got = await state.get_prospect(pid)
    assert got.contact_kind == "business" and got.first_name == ""
    plain = Prospect(first_name="Jo", last_name="Doe", title="Owner", email="jo@x.com")
    got2 = await state.get_prospect(await state.add_prospect(plain))
    assert got2.contact_kind == "person"


def test_greeting_renders_naturally_for_a_business_contact():
    sender = Sender.__new__(Sender)
    p = business()
    assert sender._render("Hi {{first_name}},\n\nQuick one for {{company}}.", p) == \
        "Hi Acme Roofing team,\n\nQuick one for Acme Roofing."
    assert sender._render("{{first_name}}, quick follow up", p) == "Hi Acme Roofing team, quick follow up"
    assert sender._render("Hi {{ first_name }},", p) == "Hi Acme Roofing team,"
    # a named person is unchanged
    named = Prospect(first_name="Todd", last_name="Young", title="Owner", company="Acme")
    assert sender._render("Hi {{first_name}},", named) == "Hi Todd,"


def test_gate_still_blocks_real_unrendered_tags():
    p = business()
    rendered = Sender.__new__(Sender)._render("Hi {{first_name}}, a question about {{company}}", p)
    ok = pre_send_check("office@acme.com", "quick question", rendered, p)
    assert ok.ok, ok.reasons
    leftover = pre_send_check("office@acme.com", "quick question",
                              "Hi there, about {{roof_type}}", p)
    assert not leftover.ok and any("unrendered merge tags" in r for r in leftover.reasons)
    leftover2 = pre_send_check("office@acme.com", "hi {{first_name}}", "Hi there", p)
    assert not leftover2.ok  # a tag the renderer never touched (subject not rendered)


def test_gate_only_sends_deliverable_business_addresses():
    risky = business(email_status="guess")
    res = pre_send_check("office@acme.com", "quick question", "Hi there, one question?", risky)
    assert not res.ok and any("not deliverable" in r for r in res.reasons)


# ── bridge allowlist ──


def test_bridge_allows_cslb():
    assert "cslb" in DISCOVER_PROVIDERS
    assert "cslb" in D.PROVIDERS
    assert any(m["key"] == "cslb" for m in D.provider_menu())
