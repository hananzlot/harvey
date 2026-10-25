"""Contacts a small business publishes about itself.

A roofing or solar contractor's website rarely lists a team page with named
decision makers. It does list a phone number and usually an address —
``office@``, ``info@``, or the owner's own Gmail. Those are real, deliverable,
and reach the owner, so Harvey treats them as a "business contact" instead of
finding nothing.

Everything here is pure text processing (no network, no database) so it is
cheap to test against awkward real-world markup.
"""

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net",
    "cox.net", "protonmail.com", "proton.me",
})

# Local parts that are never a real inbox for a sales conversation.
_BAD_LOCALS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "webmaster", "abuse", "privacy", "unsubscribe", "bounce",
    "bounces", "admin@wordpress", "wordpress", "root", "hostmaster",
    "example", "name", "yourname", "your", "youremail", "email", "user",
    "username", "test", "sample", "someone", "firstname", "lastname",
    "first.last", "you", "me", "jane", "janedoe", "johndoe", "john.doe",
    "careers", "jobs", "hr", "press", "legal", "dmca", "copyright",
})

# Registrable domains that are placeholders or platform plumbing.
_BAD_DOMAIN_PARTS = (
    "example.", "domain.", "mysite.", "yoursite.", "yourdomain.", "yourcompany.",
    "email.com", "sentry", "wixpress", "wix.com", "godaddy.", "squarespace.",
    "wordpress.", "weebly.", "shopify.", "cloudflare", "amazonaws.",
    "test.com", "localhost", "company.com", "website.com", "site.com",
    "sample.", "placeholder.",
)

_IMAGE_TLDS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico",
                         "css", "js", "woff", "woff2", "ttf", "pdf", "mp4"})

# Preference among addresses at the company's own domain (lower = better).
_OWN_PRIORITY = (
    ("owner", 0), ("ceo", 0), ("president", 0), ("office", 1), ("info", 1),
    ("contact", 1), ("hello", 1), ("admin", 2), ("sales", 2), ("estimates", 2),
    ("service", 3), ("services", 3), ("support", 3), ("customerservice", 3),
    ("billing", 4), ("accounting", 4),
)


def is_placeholder(email: str) -> bool:
    """True for addresses that are template filler, plumbing, or not an inbox."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return True
    local, _, domain = email.partition("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in _IMAGE_TLDS or re.search(r"@\d+x\.", email):
        return True                      # "logo@2x.png"
    if local in _BAD_LOCALS or len(local) < 2:
        return True
    if re.fullmatch(r"[0-9a-f]{16,}", local):
        return True                      # machine ids (Sentry DSNs etc.)
    if any(part in domain for part in _BAD_DOMAIN_PARTS):
        return True
    if domain.startswith(("sentry", "wixpress")):
        return True
    return False


def _deobfuscate(text: str) -> str:
    """"info [at] acmeroofing [dot] com" -> "info@acmeroofing.com"."""
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.I)
    return text.replace("&#64;", "@").replace("&commat;", "@")


def harvest_emails(html: str, mailto_hrefs: list[str] | None = None) -> list[str]:
    """Every distinct, non-placeholder address on a page, in order of appearance.

    ``mailto_hrefs`` (raw ``mailto:`` hrefs, when the caller parsed them) come
    first: a mailto is a deliberate publication, body text may be noise.
    """
    found: list[str] = []
    for href in mailto_hrefs or []:
        h = href.strip()
        if h.lower().startswith("mailto:"):
            addr = h[7:].split("?")[0].strip().lower()
            if addr:
                found.append(addr)
    text = _deobfuscate(html or "")
    # Strip tags so "<b>office</b>@x.com"-style splits don't matter, but keep
    # the original too: mailto hrefs only exist in the raw markup.
    for blob in (text, re.sub(r"<[^>]+>", " ", text)):
        found.extend(m.lower() for m in EMAIL_RE.findall(blob))
    out: list[str] = []
    for addr in found:
        addr = addr.strip(".,;:'\"<>()")
        if addr and addr not in out and not is_placeholder(addr):
            out.append(addr)
    return out


def _root(domain: str) -> str:
    parts = (domain or "").lower().strip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def rank_business_emails(emails: list[str], domain: str,
                         company_name: str = "") -> list[str]:
    """Best-first. Own-domain beats free-mail; roles beat random locals.

    A free-mail address is kept only when it plausibly belongs to the owner:
    on a small contractor's own site, an owner Gmail is the normal case.
    """
    own_root = _root(domain)
    toks = [t for t in re.findall(r"[a-z0-9]+", (company_name or "").lower())
            if len(t) >= 3]

    def key(addr: str):
        local, _, dom = addr.partition("@")
        is_own = _root(dom) == own_root and bool(own_root)
        is_free = dom in FREE_MAIL
        if not (is_own or is_free):
            return None                  # some third party's address
        rank = 99
        for word, pri in _OWN_PRIORITY:
            if local == word or local.startswith(word):
                rank = pri
                break
        else:
            rank = 2                     # a personal-looking local part
        name_hit = any(t in local for t in toks)
        return (0 if is_own else 1, 0 if name_hit else 1, rank)

    scored = [(key(a), i, a) for i, a in enumerate(emails)]
    return [a for k, i, a in sorted(
        (x for x in scored if x[0] is not None), key=lambda x: (x[0], x[1]))]


# ── "My name is Todd Gregory Young, owner of ..." ─────────────────────

_NAME = r"([A-Z][a-z]+(?:[ .'\-][A-Z][a-z.']+){1,2})"
_ROLE = r"(?i:owner|founder|president|ceo)"
_OWNER_PATTERNS = (
    re.compile(r"(?i:\bmy name is|\bi am|\bi'm)\s+" + _NAME +
               r"[,.]?\s+(?i:(?:and\s+)?(?:i am\s+)?(?:the\s+)?)" + _ROLE + r"\b"),
    re.compile(_NAME + r",?\s+(?i:(?:is\s+)?(?:the\s+|our\s+)?)" + _ROLE + r"\b"),
    re.compile(r"(?i:\b(?:owned|founded|started|run|operated)\s+(?:and\s+\w+\s+)?by)\s+" + _NAME),
    re.compile(r"\b" + _ROLE + r"\s*[:\-\u2013,]\s*" + _NAME),
)
_NOT_NAMES = frozenset({
    "roofing", "solar", "company", "contractor", "contractors", "licensed",
    "family", "construction", "services", "service", "california", "southern",
    "inc", "llc", "the", "our", "your", "owner", "project", "projects",
    "customer", "customers", "free", "estimate", "estimates", "home",
    "homes", "san", "los", "orange", "county", "angeles", "diego",
})


def _clean_person(raw: str) -> tuple[str, str] | None:
    words = [w.strip(".,'") for w in re.split(r"[ ]+", raw.strip()) if w.strip(".,'")]
    if not 2 <= len(words) <= 3:
        return None
    if any(w.lower() in _NOT_NAMES or len(w) < 2 for w in words):
        return None
    return words[0], " ".join(words[1:])


def find_owner_name(text: str) -> tuple[str, str] | None:
    """A (first, last) owner name stated in page text, or None.

    Deliberately simple: a handful of high-precision phrasings ("My name is
    Todd Gregory Young, owner of ...", "Owned by Maria Lopez", "Maria Lopez,
    Owner"). Case-sensitive on the name, so ordinary prose does not match.
    """
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or ""))
    for pat in _OWNER_PATTERNS:
        for m in pat.finditer(flat):
            person = _clean_person(m.group(1))
            if person:
                return person
    return None
