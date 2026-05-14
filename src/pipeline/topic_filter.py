from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def _normalize_terms(values: Iterable[str] | None) -> List[str]:
    if not values:
        return []
    out = []
    for v in values:
        s = str(v).strip().lower()
        if s:
            out.append(s)
    return sorted(set(out))


def load_topic_profile(path: str) -> Dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Topic profile not found: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not data.get("name"):
        raise ValueError("Topic profile requires 'name'.")

    profile = {
        "name": str(data["name"]),
        "include_terms_title": _normalize_terms(data.get("include_terms_title")),
        "include_terms_abstract": _normalize_terms(data.get("include_terms_abstract")),
        "exclude_terms_title": _normalize_terms(data.get("exclude_terms_title")),
        "exclude_terms_abstract": _normalize_terms(data.get("exclude_terms_abstract")),
        "cpc_include_prefixes": [x.strip().upper() for x in data.get("cpc_include_prefixes", []) if str(x).strip()],
    }
    return profile


def _find_matches(text: str, terms: List[str]) -> List[str]:
    if not text or not terms:
        return []
    t = text.lower()
    return [term for term in terms if term in t]


def _split_cpc(cpc_groups: str) -> List[str]:
    if not cpc_groups:
        return []
    parts = []
    for raw in cpc_groups.split(";"):
        p = raw.strip().upper()
        if p:
            parts.append(p)
    return parts


def match_topic(metadata: Dict[str, str], profile: Dict) -> Dict[str, object]:
    title = str(metadata.get("patent_title") or "")
    abstract = str(metadata.get("patent_abstract") or "")
    cpc_groups = str(metadata.get("cpc_groups") or metadata.get("cpc_prefixes") or "")

    include_title = _find_matches(title, profile.get("include_terms_title", []))
    include_abstract = _find_matches(abstract, profile.get("include_terms_abstract", []))
    exclude_title = _find_matches(title, profile.get("exclude_terms_title", []))
    exclude_abstract = _find_matches(abstract, profile.get("exclude_terms_abstract", []))

    cpc_hits: List[str] = []
    prefixes = profile.get("cpc_include_prefixes", []) or []
    if prefixes:
        for code in _split_cpc(cpc_groups):
            for pref in prefixes:
                if code.startswith(pref):
                    cpc_hits.append(pref)

    include_hit = bool(include_title or include_abstract or cpc_hits)
    exclude_hit = bool(exclude_title or exclude_abstract)

    return {
        "topic_pass": include_hit and not exclude_hit,
        "matched_include_terms": "; ".join(sorted(set(include_title + include_abstract))),
        "matched_exclude_terms": "; ".join(sorted(set(exclude_title + exclude_abstract))),
        "matched_cpc_prefixes": "; ".join(sorted(set(cpc_hits))),
        "rule_profile": profile.get("name", ""),
    }


def apply_topic_filter(
    pubnorms: List[str],
    metadata_by_pubnorm: Dict[str, Dict[str, str]],
    profile: Dict,
) -> Tuple[List[str], List[Dict[str, object]]]:
    filtered: List[str] = []
    audit_rows: List[Dict[str, object]] = []

    for pat in pubnorms:
        meta = metadata_by_pubnorm.get(pat, {}) or {}
        res = match_topic(meta, profile)
        if res["topic_pass"]:
            filtered.append(pat)

        row = {
            "patent": pat,
            "topic_pass": bool(res["topic_pass"]),
            "matched_include_terms": str(res["matched_include_terms"]),
            "matched_exclude_terms": str(res["matched_exclude_terms"]),
            "matched_cpc_prefixes": str(res["matched_cpc_prefixes"]),
            "rule_profile": str(res["rule_profile"]),
            "patent_title": str(meta.get("patent_title") or ""),
            "patent_abstract": str(meta.get("patent_abstract") or ""),
            "cpc_groups": str(meta.get("cpc_groups") or ""),
            "wipo_kind": str(meta.get("wipo_kind") or ""),
            "wipo_sector_title": str(meta.get("wipo_sector_title") or ""),
            "wipo_field_title": str(meta.get("wipo_field_title") or ""),
        }
        audit_rows.append(row)

    return filtered, audit_rows
