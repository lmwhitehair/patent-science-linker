# src/pipeline/company_query.py
from __future__ import annotations

import re
from typing import Dict, Iterable, List

# Common corporate tokens to ignore when forming AND-tokens queries
CORP_STOP = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "LTD", "LIMITED",
    "LLC", "PLC", "LP", "HOLDINGS", "HOLDING", "TECHNOLOGIES", "TECHNOLOGY", "SYSTEMS", "GROUP",
    "SA", "NV", "AG", "GMBH", "PTY", "BV", "KK", "CO.,", "CO.", "CORP.", "INC.", "LTD.", "LLP", "S.A.",
}

# Suffix expansions (so a single seed covers common variants)
SUFFIX_MAP = {
    " CORP": [" CORP", " CORPORATION", " CORP."],
    " CO":   [" CO", " CO.", " COMPANY", " COMPANIES"],
    " INC":  [" INC", " INC."],
    " LTD":  [" LTD", " LIMITED", " LTD."],
    " LLC":  [" LLC", " L.L.C."],
    " PLC":  [" PLC"],
}


def norm_upper(s: str) -> str:
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def expand_suffixes(name: str) -> List[str]:
    out = {name}
    for key, variants in SUFFIX_MAP.items():
        if name.endswith(key):
            stem = name[: -len(key)]
            for v in variants:
                out.add(stem + v)
    return sorted(out)


def tokens_for_and(name: str) -> List[str]:
    toks = [t for t in re.split(r"[\s\.,\-]+", norm_upper(name)) if t]
    return [t for t in toks if t not in CORP_STOP]


def seed_aliases(company: str, extra: Iterable[str] | None = None, include_predecessors: bool = False) -> List[str]:
    seeds = {norm_upper(company)}
    if extra:
        seeds.update(norm_upper(x) for x in extra if x)

    # Optionally bake in predecessor examples (extend as needed)
    if include_predecessors:
        if any(s.startswith("LOCKHEED") or s.startswith("LOCKHEED MARTIN") for s in seeds):
            seeds.update(["LOCKHEED AIRCRAFT CORPORATION", "MARTIN MARIETTA CORPORATION", "SKUNK WORKS", "LOCKHEED CORPORATION"])
        if any(s.startswith("RAYTHEON") or s.startswith("RTX") for s in seeds):
            seeds.update(["RAYTHEON TECHNOLOGIES CORPORATION", "UNITED TECHNOLOGIES CORPORATION", "RTX CORPORATION", "RAYTHEON COMPANY", "RAYTHEON INTELLIGENCE & SPACE", "RAYTHEON MISSILES & DEFENSE"])
        if any(s.startswith("NORTHROP") for s in seeds):
            seeds.update(["NORTHROP CORPORATION", "NORTHROP GRUMMAN CORPORATION", "GRUMMAN CORPORATION", "NORTHROP GRUMMAN SYSTEMS CORPORATION", "NORTHROP GRUMMAN INNOVATION SYSTEMS LLC"])
        if any(s.startswith("BOEING") or s.startswith("BOEING CO") for s in seeds):
            seeds.update(["BOEING DEFENSE, SPACE & SECURITY", "THE BOEING COMPANY"])

    # Expand suffixes so one alias covers CORP/CORPORATION/CO/COMPANY/etc.
    expanded = set()
    for s in seeds:
        for v in expand_suffixes(s):
            expanded.add(v)
    return sorted(expanded)


# -------- Query builders (Lens JSON) --------

def q_terms_exact(aliases: List[str], *, exact: bool = True) -> Dict:
    # precise: matches any alias exactly (OR) across owner/applicant keyword fields
    # If exact=True, use .exact fields only.
    if exact:
        return {
            "query": {
                "bool": {
                    "should": [
                        {"terms": {"owner_all.name.exact": aliases}},
                        {"terms": {"applicant.name.exact": aliases}},
                    ]
                }
            }
        }
    # If not exact, drop to analyzed text fields (less precise)
    return {
        "query": {
            "bool": {
                "should": [
                    {"terms": {"owner_all.name": aliases}},
                    {"terms": {"applicant.name": aliases}},
                ]
            }
        }
    }


def q_all_tokens(company: str) -> Dict:
    # recall: require ALL meaningful tokens (AND) to appear in owner OR applicant
    toks = tokens_for_and(company)
    if not toks:
        toks = [norm_upper(company)]
    must_owner = [{"match": {"owner_all.name": t}} for t in toks]
    must_app = [{"match": {"applicant.name": t}} for t in toks]
    return {
        "query": {
            "bool": {
                "should": [
                    {"bool": {"must": must_owner}},
                    {"bool": {"must": must_app}},
                ]
            }
        }
    }


def _with_resolved_npl(base: Dict, only: bool) -> Dict:
    if not only:
        return base
    base = {**base}
    qb = base.setdefault("query", {}).setdefault("bool", {})
    must = qb.setdefault("must", [])
    must.append({"match": {"cites_resolved_npl": True}})
    return base


def _with_filters(
    base: Dict,
    *,
    jurisdiction: str | None,
    year_from: int | None,
    year_to: int | None,
) -> Dict:
    base = {**base}
    qb = base.setdefault("query", {}).setdefault("bool", {})
    flt = qb.setdefault("filter", [])
    if jurisdiction:
        flt.append({"term": {"jurisdiction": jurisdiction}})
    if year_from or year_to:
        rng = {"range": {"year_published": {}}}
        if year_from is not None:
            rng["range"]["year_published"]["gte"] = year_from
        if year_to is not None:
            rng["range"]["year_published"]["lte"] = year_to
        flt.append(rng)
    return base


def _with_group_by(base: Dict, group_by: str | None) -> Dict:
    if not group_by:
        return base
    p = {**base}
    p["group_by"] = group_by  # Lens supports SIMPLE_FAMILY / EXTENDED_FAMILY
    return p


def build_passes(
    company: str,
    aliases: List[str],
    resolved_only: bool,
    *,
    fast_predecessors: bool = False,
    jurisdiction: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    group_by: str | None = None,
    exact: bool = True,
) -> List[Dict]:
    """
    Returns a list of Lens JSON payloads ("passes") to run inclusively and union.
    Pass 1: exact alias OR
    Pass 2: token-AND on the seed name (skipped when fast_predecessors=True)
    Filters and group_by are applied to each pass.
    """
    passes: List[Dict] = []

    # Pass 1 — precise aliases
    p1 = q_terms_exact(aliases, exact=exact)
    p1 = _with_resolved_npl(p1, resolved_only)
    p1 = _with_filters(p1, jurisdiction=jurisdiction, year_from=year_from, year_to=year_to)
    p1 = _with_group_by(p1, group_by)
    passes.append(p1)

    # Pass 2 — recall (skip if fast_predecessors)
    if not fast_predecessors:
        p2 = q_all_tokens(company)
        p2 = _with_resolved_npl(p2, resolved_only)
        p2 = _with_filters(p2, jurisdiction=jurisdiction, year_from=year_from, year_to=year_to)
        p2 = _with_group_by(p2, group_by)
        passes.append(p2)

    return passes
