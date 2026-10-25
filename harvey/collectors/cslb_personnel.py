"""Owner names from the California licence board's personnel file.

The board's free Master List download (DataPortal/ContractorList → Personnel
→ CSV) lists everyone associated with each licence: "LAST  FIRST  MIDDLE",
their title(s), and when each association ended. Harvey's licensed-contractor
companies (external_id 'cslb:<licence>') get the best current person — owner
first, then president, member, officer — so an email to the business's
published address can open "Hi Peter," instead of "Hi <Company> team,".

The site blocks scripted downloads, so the owner downloads the file and runs
``harvey cslb-owners <path>``.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Lower rank wins. A responsible managing EMPLOYEE is a hired qualifier, not
# an owner, so it is never used.
_TITLE_RANK = [
    ("sole owner", 0, "Owner"),
    ("general partner", 1, "Owner"),
    ("partner", 1, "Owner"),
    ("chief executive officer/president", 2, "President"),
    ("president", 2, "President"),
    ("llc member", 3, "Owner"),
    ("responsible managing member", 3, "Owner"),
    ("member", 3, "Owner"),
    ("responsible managing officer", 4, "Officer"),
    ("officer", 5, "Officer"),
]
_SKIP = ("responsible managing employee",)


@dataclass(frozen=True)
class LicenceOwner:
    licence: str
    first_name: str
    last_name: str
    title: str
    rank: int


def _nice(word: str) -> str:
    w = word.capitalize()
    w = re.sub(r"^(Mc|O')([a-z])", lambda m: m.group(1) + m.group(2).upper(), w)
    return re.sub(r"-([a-z])", lambda m: "-" + m.group(1).upper(), w)


def parse_name(raw: str) -> tuple[str, str]:
    """'KIM                PETER      JUNWOO' -> ('Peter', 'Kim')."""
    parts = [p for p in re.split(r"\s{2,}", (raw or "").strip()) if p.strip()]
    if len(parts) < 2:
        return "", ""
    last, first = parts[0].strip(), parts[1].strip().split()[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", first):
        return "", ""
    return _nice(first), " ".join(_nice(w) for w in last.split())


def best_title(titles: str, ended: str) -> tuple[int, str] | None:
    """Best still-current title among '|'-separated entries, or None."""
    t_list = [t.strip().lower() for t in (titles or "").split("|")]
    d_list = [d.strip() for d in (ended or "").split("|")]
    best = None
    for i, t in enumerate(t_list):
        if not t or any(s in t for s in _SKIP):
            continue
        if i < len(d_list) and d_list[i]:
            continue  # this association has ended
        for key, rank, label in _TITLE_RANK:
            if key in t:
                if best is None or rank < best[0]:
                    best = (rank, label)
                break
    return best


def read_owners(path: Path, licences: set[str] | None = None) -> dict[str, LicenceOwner]:
    """Best current person per licence (optionally only the given licences)."""
    owners: dict[str, LicenceOwner] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            licence = (row.get("LIC-NO") or "").strip()
            if not licence or (licences is not None and licence not in licences):
                continue
            if "principal" not in (row.get("Name-TP") or "").lower():
                continue  # a business-name entry, not a person
            picked = best_title(row.get("EMP-Titl-CDE", ""), row.get("DIS-ASSN-DT", ""))
            if picked is None:
                continue
            first, last = parse_name(row.get("Name", ""))
            if not first:
                continue
            cur = owners.get(licence)
            if cur is None or picked[0] < cur.rank:
                owners[licence] = LicenceOwner(licence, first, last, picked[1], picked[0])
    return owners
