"""CSLB — California Contractors State License Board public download.

The free, official list of every licensed California contractor: license
number, business name, address, phone, status, classifications, bond and
workers' comp. It carries NO email addresses and NO websites — it is the
authoritative "who exists" list, and Harvey finds the rest elsewhere.

Source: https://www.cslb.ca.gov/OnlineServices/DataPortal/ListByCounty

The page is an ASP.NET WebForms page. A download is a POST that echoes the
hidden state fields (__VIEWSTATE, __EVENTVALIDATION, ...) from a fresh GET
plus the chosen classification(s) and county code(s), and answers with an
.xlsx attachment (the page says "Excel"; in practice it is xlsx).

Observed behaviour (2026-10-25): selecting more than one classification does
NOT return the union, so Harvey downloads one classification per request.

This module is pure parsing and form-posting — no database, no discovery
types — so both ``discover.py`` and the tests can import it freely.
"""

import html
import io
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger("harvey.collectors.cslb")

FORM_URL = "https://www.cslb.ca.gov/OnlineServices/DataPortal/ListByCounty"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CLASSIFICATION_FIELD = "ctl00$MainContent$lbClassification"
COUNTY_FIELD = "ctl00$MainContent$lbCounty"
SEARCH_BUTTON = "ctl00$MainContent$btnSearch"
MAX_COUNTIES = 10                 # the form's own limit per download
POLITE_DELAY_SECONDS = 5.0        # between downloads

# ── Mapping tables (config-driven: icp.cslb_classifications overrides) ──

# Substring of an ICP industry (lowercase) -> CSLB classification code.
DEFAULT_CLASSIFICATIONS: dict[str, str] = {
    "roof": "C-39",      # Roofing contractor
    "solar": "C-46",     # Solar contractor
}

# County name -> the form's numeric county code (all 58, from the live page).
COUNTY_CODES: dict[str, str] = {
    "alameda": "1", "alpine": "2", "amador": "3", "butte": "4",
    "calaveras": "5", "colusa": "6", "contra costa": "7", "del norte": "8",
    "el dorado": "9", "fresno": "10", "glenn": "11", "humboldt": "12",
    "imperial": "13", "inyo": "14", "kern": "15", "kings": "16", "lake": "17",
    "lassen": "18", "los angeles": "19", "madera": "20", "marin": "21",
    "mariposa": "22", "mendocino": "23", "merced": "24", "modoc": "25",
    "mono": "26", "monterey": "27", "napa": "28", "nevada": "29",
    "orange": "30", "placer": "31", "plumas": "32", "riverside": "33",
    "sacramento": "34", "san benito": "35", "san bernardino": "36",
    "san diego": "37", "san francisco": "38", "san joaquin": "39",
    "san luis obispo": "40", "san mateo": "41", "santa barbara": "42",
    "santa clara": "43", "santa cruz": "44", "shasta": "45", "sierra": "46",
    "siskiyou": "47", "solano": "48", "sonoma": "49", "stanislaus": "50",
    "sutter": "51", "tehama": "52", "trinity": "53", "tulare": "54",
    "tuolumne": "55", "ventura": "56", "yolo": "57", "yuba": "58",
}

# Geography entries -> county, for deriving counties when none are configured.
GEOGRAPHY_COUNTIES: dict[str, tuple[str, ...]] = {
    "los angeles": ("Los Angeles",),
    "orange county": ("Orange",),
    "san diego": ("San Diego",),
    "inland empire": ("Riverside", "San Bernardino"),
}

DEFAULT_COUNTIES = ["Los Angeles", "Orange", "San Diego", "Riverside",
                    "San Bernardino"]


class CslbError(RuntimeError):
    """The download failed in a way the caller should report verbatim."""


class CslbBlocked(CslbError):
    """The site refused automation (captcha, 403, an HTML page, ...)."""


def classifications_for(industries: list[str],
                        overrides: dict[str, str] | None = None
                        ) -> list[tuple[str, str]]:
    """[(industry, code)] for every ICP industry that maps to a classification.

    ``overrides`` (icp.cslb_classifications) is checked before the defaults.
    An industry that maps to nothing is skipped; a code is never repeated.
    """
    table = {**DEFAULT_CLASSIFICATIONS,
             **{k.lower(): v for k, v in (overrides or {}).items()}}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for industry in industries:
        low = industry.lower()
        for keyword, code in table.items():
            if keyword in low and code not in seen:
                seen.add(code)
                out.append((industry, code))
                break
    return out


def resolve_counties(counties: list[str] | None,
                     geography: list[str] | None = None) -> list[str]:
    """Canonical county names: configured ones, else derived, else SoCal."""
    wanted = [c.strip() for c in (counties or []) if c and c.strip()]
    if not wanted:
        for place in geography or []:
            low = place.lower()
            for key, names in GEOGRAPHY_COUNTIES.items():
                if key in low:
                    wanted.extend(names)
    if not wanted:
        wanted = list(DEFAULT_COUNTIES)
    out: list[str] = []
    for name in wanted:
        key = re.sub(r"\s+county$", "", name.strip(), flags=re.I).lower()
        if key not in COUNTY_CODES:
            raise ValueError(f"unknown California county {name!r}")
        canon = key.title()
        if canon not in out:
            out.append(canon)
    return out[:MAX_COUNTIES]


def county_codes(names: list[str]) -> list[str]:
    return [COUNTY_CODES[re.sub(r"\s+county$", "", n.strip(), flags=re.I).lower()]
            for n in names]


# ── The record ────────────────────────────────────────────────────────


@dataclass
class License:
    number: str = ""
    business_type: str = ""
    name: str = ""
    address: str = ""        # "1200 Dewey Ave, Los Angeles, CA 90006"
    city: str = ""
    county: str = ""
    phone: str = ""
    status: str = ""
    classifications: tuple[str, ...] = ()
    expires: str = ""

    def is_active(self) -> bool:
        return self.status.strip().upper() == "CLEAR"

    def holds(self, code: str) -> bool:
        want = code.replace("-", "").upper()
        return want in {c.replace("-", "").upper() for c in self.classifications}


_NAME_KEEP_UPPER = {"llc", "lp", "llp", "usa", "la", "sd", "oc", "ac", "hvac",
                    "ii", "iii", "iv"}


def _cap(piece: str) -> str:
    return piece if piece.isdigit() or len(piece) <= 1 and not piece.isalpha() \
        else piece.capitalize()


def pretty_name(raw: str) -> str:
    """"F D L ROOFING INC" -> "F D L Roofing Inc"; "A-1 O'BRIEN LLC" -> "A-1 O'Brien LLC"."""
    out = []
    for w in re.sub(r"\s+", " ", (raw or "").strip()).split(" "):
        if not w:
            continue
        if w.lower() in _NAME_KEEP_UPPER:
            out.append(w.upper())
        elif len(w) == 1:
            out.append(w.upper())
        else:
            out.append("-".join("'".join(_cap(p) for p in seg.split("'"))
                                for seg in w.split("-")))
    return " ".join(out)


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def parse_workbook(content: bytes) -> list[License]:
    """Parse a CSLB download (.xlsx bytes) into License rows.

    Header-driven, so a reordered or extended column set still parses.
    Raises CslbError when the bytes are not a workbook or lack the expected
    columns, so a changed format is loud rather than silently empty.
    """
    if content[:4] == b"\xd0\xcf\x11\xe0":
        raise CslbError("CSLB returned legacy .xls; only .xlsx is supported")
    if content[:2] != b"PK":
        raise CslbError("CSLB response is not an Excel workbook")
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True,
                                    data_only=True)
    except Exception as e:  # corrupt zip, missing dependency, ...
        raise CslbError(f"could not open the CSLB workbook: {e}") from e
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        return []
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        # First occurrence wins: "ExpirationDate" appears twice (license, then
        # workers' comp as ExpirationDate1).
        idx.setdefault(_cell(h).lower(), i)
    need = ("licensenumber", "businessname")
    if any(k not in idx for k in need):
        raise CslbError(f"unexpected CSLB columns: {[_cell(h) for h in header][:8]}")

    def col(row, name):
        i = idx.get(name)
        return _cell(row[i]) if i is not None and i < len(row) else ""

    out: list[License] = []
    for row in rows:
        number = col(row, "licensenumber")
        if not number:
            continue
        street, city = col(row, "address"), col(row, "city")
        state, zip_ = col(row, "state") or "CA", col(row, "zip code")
        tail = " ".join(x for x in (state, zip_) if x)
        address = ", ".join(x for x in (street.title(), city.title(), tail) if x)
        classes = tuple(c.strip() for c in col(row, "classification(s)").split("|")
                        if c.strip())
        out.append(License(
            number=number.split(".")[0],
            business_type=col(row, "businesstype"),
            name=pretty_name(col(row, "businessname")),
            address=address,
            city=city.title(),
            county=col(row, "county"),
            phone=col(row, "phonenumber"),
            status=col(row, "status"),
            classifications=classes,
            expires=col(row, "expirationdate"),
        ))
    wb.close()
    return out


def hidden_fields(page: str) -> dict[str, str]:
    """The ASP.NET state fields to echo back on the POST."""
    fields = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', page):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return fields


async def download(client: httpx.AsyncClient, code: str,
                   counties: list[str]) -> bytes:
    """One polite download: one classification x up to ten counties.

    A fresh GET supplies the ASP.NET state; the POST names the Download
    button as the event target. Anything other than an Excel attachment is
    reported as CslbBlocked with what the site actually said.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        page = await client.get(FORM_URL, headers=headers, follow_redirects=True)
    except httpx.HTTPError as e:
        raise CslbError(f"CSLB form unreachable: {e}") from e
    if page.status_code != 200:
        raise CslbBlocked(f"CSLB form returned HTTP {page.status_code}")
    fields = hidden_fields(page.text)
    if "__VIEWSTATE" not in fields:
        raise CslbBlocked("CSLB form had no __VIEWSTATE (page changed or "
                          "a challenge page was served)")

    data: dict[str, object] = dict(fields)
    data["__EVENTTARGET"] = SEARCH_BUTTON
    data[CLASSIFICATION_FIELD] = [code]
    data[COUNTY_FIELD] = county_codes(counties)
    try:
        r = await client.post(FORM_URL, data=data,
                              headers={**headers, "Referer": FORM_URL},
                              follow_redirects=True)
    except httpx.HTTPError as e:
        raise CslbError(f"CSLB download failed: {e}") from e
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200 or "html" in ctype or r.content[:2] != b"PK":
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text[:3000]))[:200]
        raise CslbBlocked(
            f"CSLB did not return a spreadsheet (HTTP {r.status_code}, "
            f"{ctype or 'no content-type'}): {snippet.strip()}")
    return r.content
