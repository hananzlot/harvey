"""Website finder — give a company that has no domain one, from a SERP lookup.

A licensed contractor from the CSLB list comes with a name and a city and no
website. This asks Google (through the DataForSEO SERP client Harvey already
has) "<business name> <city> CA", and takes the first organic result that is

1. not a directory / aggregator / social profile, and
2. plausibly the SAME business: the business name's distinctive words show
   up in the domain or the page title.

Both checks matter: skipping directories alone returns whatever random site
ranks first; matching alone returns Yelp. Costs one SERP page per company
(depth 10), counted against the run's spend cap.
"""

import asyncio
import difflib
import logging
import re

logger = logging.getLogger("harvey.collectors.website_finder")

LOOKUP_DEPTH = 10
CONCURRENCY = 4

# Directories, review sites, social profiles, marketplaces, lead-gen
# middlemen, licensing/permit databases, map and search properties. Matched
# as a registered-domain label or suffix, so "m.yelp.com" and "yelp.ca" hit.
DIRECTORY_DOMAINS = frozenset({
    "yelp", "bbb", "angi", "angieslist", "homeadvisor", "thumbtack", "houzz",
    "facebook", "instagram", "linkedin", "nextdoor", "yellowpages", "mapquest",
    "buildzoom", "porch", "manta", "chamberofcommerce", "google", "apple",
    "bing", "yahoo", "duckduckgo", "twitter", "x", "tiktok", "youtube",
    "pinterest", "reddit", "quora", "wikipedia", "trustpilot", "birdeye",
    "foursquare", "tripadvisor", "superpages", "networx", "modernize",
    "fixr", "homeguide", "homelight", "expertise", "bark", "yellowbot",
    "citysearch", "merchantcircle", "dandb", "dnb", "zoominfo", "opencorporates",
    "bizapedia", "buzzfile", "cylex", "hotfrog", "showmelocal", "alignable",
    "indeed", "glassdoor", "ziprecruiter", "zillow", "realtor", "redfin",
    "amazon", "cslb", "contractorlicensing", "licensecheck", "mycontractor",
    "sunrun", "energysage", "solarreviews", "consumeraffairs", "angi",
    "constructionbidsource", "localdatabase", "roofingcalc",
})

# Words in a business name that say what it does, not who it is.
GENERIC_NAME_WORDS = frozenset({
    "inc", "llc", "corp", "corporation", "co", "company", "ltd", "lp", "llp",
    "the", "and", "of", "dba", "roofing", "roofs", "roof",
    "roofers", "solar", "construction", "contractor", "contractors",
    "contracting", "services", "service", "systems", "system", "energy",
    "power", "home", "homes", "general", "building", "builders", "builder",
    "enterprises", "enterprise", "group", "industries", "restoration",
    "remodeling", "repair", "repairs", "installation", "installers",
    "installer", "electric", "electrical", "pro", "pros", "professional",
    "professionals", "usa", "california", "socal", "southern", "county",
    "commercial", "residential", "ca", "oc", "gutters", "gutter", "raingutters",
})


def _registered_labels(domain: str) -> list[str]:
    return [p for p in (domain or "").lower().split(".") if p]


def is_directory(domain: str) -> bool:
    """True for directories, aggregators, socials and other non-own-sites."""
    domain = (domain or "").lower().strip(".")
    labels = [p for p in domain.split(".") if p]
    if len(labels) < 2:
        return True
    # Every label except the last (the TLD): "m.yelp.com", "yelp.co.uk",
    # "www.bbb.org". A business's own subdomain site ("joes.wixsite.com")
    # is deliberately NOT listed.
    if any(l in DIRECTORY_DOMAINS for l in labels[:-1]):
        return True
    if domain.endswith((".gov", ".edu", ".mil")):
        return True
    # The shared junk patterns used by discovery (lead-gen, news, infra).
    from harvey.collectors.discover import is_junk
    return is_junk(domain)


def name_tokens(name: str) -> list[str]:
    """The distinctive words of a business name, lowercase, no generics."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    return [w for w in words if w not in GENERIC_NAME_WORDS]


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


US_FRIENDLY_CC = frozenset({"us", "co", "io", "ai", "me", "tv"})


def plausible_match(name: str, domain: str, title: str = "") -> bool:
    """Does this domain belong to the business called ``name``?

    Strict on purpose (2026-10-25: the looser version — page-title words,
    fuzzy similarity, words anywhere in a subdomain — matched gaf.com,
    theglobeandmail.com, usphonebook.com and a lead-gen subdomain to licensed
    roofers, and their emails went out as contacts). Only the registered name
    (the label before the TLD; never a subdomain) counts, and it must hold:
    every distinctive word of 3+ letters; or, for names with 3+ such words,
    the first one plus at least one more; or, when the name has no distinctive
    word that long ("J J Roofing", "K R Construction"), the squashed letters as
    its start. ``title`` is accepted for compatibility and ignored.
    """
    labels = [p for p in (domain or "").lower().split(".") if p]
    if len(labels) < 2:
        return False
    # California licensees: a two-letter country ending other than .us
    # (gtamegaroofing.ca, a Toronto roofer, matched "Mega Roofing Inc").
    if len(labels[-1]) == 2 and labels[-1] not in US_FRIENDLY_CC:
        return False
    label = _squash(labels[-2])
    if len(labels) >= 3 and len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        label = _squash(labels[-3])      # "acme.co.uk" style
    if not label:
        return False
    toks = name_tokens(name)
    long_toks = [t for t in toks if len(t) >= 3]
    if long_toks:
        hits = [t for t in long_toks if t in label]
        if len(hits) == len(long_toks):
            return True
        return len(long_toks) >= 3 and long_toks[0] in hits and len(hits) >= 2
    squashed = "".join(toks)
    return len(squashed) >= 2 and label.startswith(squashed)


def pick_website(name: str, results) -> tuple[str, str] | None:
    """First organic result that is not a directory and matches the name.

    ``results`` are Business-like objects (``.domain``, ``.website``,
    ``.name`` = page title) in SERP order. Returns (domain, url) or None.
    """
    for r in results:
        domain = (getattr(r, "domain", "") or "").lower()
        if not domain or is_directory(domain):
            continue
        if plausible_match(name, domain, getattr(r, "name", "") or ""):
            return domain, getattr(r, "website", "") or f"https://{domain}"
    return None


def city_of(location: str) -> str:
    """"1200 Dewey Ave, Los Angeles, CA 90006" -> "Los Angeles"."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    for i, p in enumerate(parts):
        if re.match(r"^CA\b", p, re.I) and i >= 1:
            return parts[i - 1]
    return parts[0] if len(parts) == 1 else (parts[-2] if len(parts) >= 2 else "")


async def find_website(client, env: dict, serp, name: str, location: str):
    """One SERP lookup. Returns (domain, url, cost) — domain "" when none fits."""
    from harvey.collectors.discover import DiscoveryQuery

    city = city_of(location)
    query = DiscoveryQuery(term=name, location=f"{city} CA".strip(),
                           depth=LOOKUP_DEPTH, limit=LOOKUP_DEPTH)
    results = await serp.fetch(client, env, query)
    # Depth 10 is one billed page. A constant, not serp.last_cost: that is
    # shared instance state and lookups run concurrently.
    cost = serp.PAGE_COST
    hit = pick_website(name, results)
    return (hit[0], hit[1], cost) if hit else ("", "", cost)


async def run_lookups(state, client, env: dict, source: str, budget: float,
                      limit: int = 5000, pause_check=None) -> dict:
    """Look up websites for ``source`` companies that have none, within budget.

    Stops when the next lookup could take total spend past ``budget``, when
    the kill switch is set (``pause_check`` returns truthy), or when nobody
    is left. Every attempted company is stamped so it is never paid for twice.
    """
    from harvey.collectors.discover import PROVIDERS

    serp = PROVIDERS["dataforseo_serp"]
    report = {"looked_up": 0, "found": 0, "cost": 0.0, "stopped": "",
              "remaining": 0, "errors": []}
    if not serp.configured(env):
        report["stopped"] = "website lookups need DATAFORSEO_LOGIN/PASSWORD in .env"
        report["remaining"] = await state.count_companies_needing_website(source)
        return report

    per = serp.estimate([_probe_query()])
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def one(company):
        async with sem:
            try:
                return company, await find_website(
                    client, env, serp, company["name"], company.get("location", ""))
            except Exception as e:
                return company, e

    while done < limit:
        if pause_check and await pause_check():
            report["stopped"] = "paused"
            break
        room = int((budget - report["cost"] + 1e-9) // per)
        if room <= 0:
            report["stopped"] = "budget reached"
            break
        batch = await state.companies_needing_website(
            source, limit=min(CONCURRENCY * 5, room, limit - done))
        if not batch:
            break
        for company, res in await asyncio.gather(*(one(c) for c in batch)):
            done += 1
            if isinstance(res, Exception):
                report["errors"].append(f"{company['name']}: {res}")
                # Not stamped: a transient failure should be retried next run.
                if len(report["errors"]) >= 5 and report["looked_up"] == 0:
                    report["stopped"] = "lookups failing"
                    report["remaining"] = await state.count_companies_needing_website(source)
                    return report
                continue
            domain, url, cost = res
            report["cost"] += cost
            report["looked_up"] += 1
            if await state.set_company_website(company["id"], domain, url):
                report["found"] += 1
        if report["stopped"]:
            break
    report["remaining"] = await state.count_companies_needing_website(source)
    return report


def _probe_query():
    from harvey.collectors.discover import DiscoveryQuery
    return DiscoveryQuery(term="x", depth=LOOKUP_DEPTH, limit=LOOKUP_DEPTH)
