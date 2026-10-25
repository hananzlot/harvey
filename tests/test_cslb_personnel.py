"""Owner names from the licence board's personnel file."""
import asyncio

from harvey.collectors.cslb_personnel import best_title, parse_name, read_owners
from harvey.models.prospect import Prospect
from harvey.state import StateManager

HEADER = ("LIC-NO,LastUpdated,REC-TP,SEQ-NO,Name-TP,Name,EMP-Titl-CDE,CL-CDE,CL-CDE-STAT,ASSN-DT,"
          "DIS-ASSN-DT,SURETY-TP,SuretyCompany,BOND-NO,BOND-AMT,EffectiveDate,CancellationDate,"
          "JointVentureLicenseType,JointVentureLicenseNumber\n")


def _row(lic, name_tp, name, titles, ended):
    return f'{lic},x,Class/Title,1,{name_tp},"{name}","{titles}",,,,"{ended}",,,,,,,,\n'


def test_parse_name():
    assert parse_name(" KIM                                PETER          JUNWOO") == ("Peter", "Kim")
    assert parse_name(" MCDONALD    SEAN") == ("Sean", "McDonald")
    assert parse_name(" O'BRIEN     MARY  K") == ("Mary", "O'Brien")
    assert parse_name(" ACME ROOFING") == ("", "")


def test_best_title_prefers_current_owner_and_skips_ended_and_employees():
    assert best_title(" Sole Owner| Sole Owner", " | 01/01/2004") == (0, "Owner")
    assert best_title(" Sole Owner", " 01/01/2004") is None
    assert best_title(" Responsible Managing Employee", " ") is None
    assert best_title(" Officer| Chief Executive Officer/President", " | ") == (2, "President")


def test_read_owners_picks_best_person_per_licence(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(HEADER
                 + _row("1", " Principal", " KIM   KWANG  HEE", " Responsible Managing Officer", " ")
                 + _row("1", " Principal", " KIM   PETER  JUNWOO", " Sole Owner", " ")
                 + _row("2", " Business", " ACME ROOFING", " Officer", " ")
                 + _row("3", " Principal", " LEE   ANN", " Officer", " "))
    owners = read_owners(p, {"1", "2"})
    assert set(owners) == {"1"}
    assert (owners["1"].first_name, owners["1"].last_name, owners["1"].title) == ("Peter", "Kim", "Owner")


def test_unnamed_licence_contacts_get_owner_name(tmp_path):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    import sqlite3
    c = sqlite3.connect(tmp_path / "h.db")
    c.execute("INSERT INTO companies (id,name,domain,external_id) VALUES ('c1','Acme','acme.com','cslb:1')")
    c.commit(); c.close()
    pid = asyncio.run(state.add_prospect(Prospect(first_name="", last_name="", title="Owner", company="Acme",
                                                  company_id="c1", email="office@acme.com",
                                                  contact_kind="business")))
    from harvey.collectors.cslb_personnel import LicenceOwner
    asyncio.run(state.save_licence_owners([LicenceOwner("1", "Peter", "Kim", "Owner", 0)]))
    assert asyncio.run(state.licence_numbers()) == {"1"}
    assert asyncio.run(state.name_unnamed_licence_contacts()) == 1
    p = asyncio.run(state.get_prospect(pid))
    assert (p.first_name, p.last_name, p.contact_kind, p.greeting_name()) == ("Peter", "Kim", "person", "Peter")
    assert asyncio.run(state.licence_owner_for_company("c1"))["first_name"] == "Peter"


def test_reply_reading_ignores_quoted_footer_and_honours_bare_stop():
    from harvey.agents.handler import OPT_OUT_PATTERNS, is_bare_stop, strip_quoted_reply
    reply = ("Sounds interesting, call me Tuesday.\n\n"
             "On Mon, Oct 26, 2026 at 9:00 AM Chanan <crm_sales@outreach.ibuildpro.ai> wrote:\n"
             "> Hi Peter,\n> ...\n> iBuildPro, 11022 Santa Monica Blvd. Reply stop to opt out.")
    new = strip_quoted_reply(reply)
    assert new == "Sounds interesting, call me Tuesday."
    assert not any(p in new.lower() for p in OPT_OUT_PATTERNS)
    for t in ("stop", "STOP.", "Stop please", "stop\n\n> quoted"):
        assert is_bare_stop(strip_quoted_reply(t) or t), t
    for t in ("don't stop now", "Stop by the yard tomorrow?", ""):
        assert not is_bare_stop(t), t
