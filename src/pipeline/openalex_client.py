# src/pipeline/openalex_client.py
from __future__ import annotations

import os
import time
from itertools import islice
from typing import Dict, Iterable, List, Optional, Union

import pyalex
import requests
from pyalex import Works, config

# ---------------------------
# OpenAlex client configuration
# ---------------------------

# Identify yourself to OpenAlex (recommended)
email = os.getenv("OPENALEX_EMAIL")
if email:
    pyalex.config.email = email

# API key (optional, but helps with higher quotas & stability)
api_key = os.getenv("OPENALEX_API_KEY")
if api_key:
    pyalex.config.api_key = api_key

# Retries & backoff
config.max_retries = int(os.getenv("OPENALEX_MAX_RETRIES", "3"))
config.retry_backoff_factor = float(os.getenv("OPENALEX_BACKOFF", "0.5"))
config.retry_http_codes = [429, 500, 503]

# OpenAlex hard limit is 100 IDs per request
MAX_IDS_PER_CALL = int(os.getenv("OPENALEX_MAX_IDS_PER_CALL", "100"))
BATCH_SLEEP = float(os.getenv("OPENALEX_BATCH_SLEEP", "0"))  # seconds between batches (optional throttle)
OPENALEX_BASE_URL = os.getenv("OPENALEX_BASE_URL", "https://api.openalex.org").rstrip("/")
OPENALEX_UNIVERSITY_MAX_WORKS = int(os.getenv("OPENALEX_UNIVERSITY_MAX_WORKS", "5000"))
OPENALEX_UNIVERSITY_PAGE_SIZE = int(os.getenv("OPENALEX_UNIVERSITY_PAGE_SIZE", "200"))


# ---------------------------
# Helpers
# ---------------------------

def _chunked(iterable: Iterable[str], n: int) -> Iterable[List[str]]:
    it = iter(iterable)
    while True:
        chunk = list(islice(it, n))
        if not chunk:
            return
        yield chunk


def normalize_oaid(x: Optional[Union[str, int]]) -> Optional[str]:
    """
    Normalize an OpenAlex work id into 'W…' form.
    Accepts integers, 'W123', or full URLs like 'https://openalex.org/W123'.
    """
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    if s.startswith("http"):
        s = s.rsplit("/", 1)[-1]
    if not s.upper().startswith("W"):
        s = f"W{s}"
    else:
        s = "W" + s[1:]  # ensure capital W
    return s


def _select_fields() -> List[str]:
    """
    Keep payloads light but useful.
    NOTE: only include fields that are valid for 'select' on Work objects.
    """
    return [
        "id",                       # URL form of openalex id
        "ids",                      # contains ids.openalex (URL), doi, pmid, etc.
        "doi",
        "title",
        "display_name",
        "publication_year",
        "abstract_inverted_index",
        "authorships",
        "concepts",
        "topics",
        "awards",                   # OpenAlex replaced/valid field vs legacy grants
        "funders",
        "primary_location",
        "referenced_works",
        "updated_date",
    ]


def _decode_abstract(inv: Optional[dict]) -> Optional[str]:
    """
    Convert abstract_inverted_index into plaintext.
    Returns None if not available.
    """
    if not inv:
        return None
    # Build a position -> token map
    max_pos = -1
    for positions in inv.values():
        if positions:
            m = max(positions)
            if m > max_pos:
                max_pos = m
    if max_pos < 0:
        return None

    words = [""] * (max_pos + 1)
    for token, positions in inv.items():
        for p in positions or []:
            if 0 <= p < len(words):
                words[p] = token

    # Collapse multiple spaces; simple join is fine for most cases
    text = " ".join(w for w in words if w != "")
    return text.strip() or None


# ---------------------------
# Public API
# ---------------------------

def fetch_works_by_ids(oaids: List[Union[str, int]], per_call: Optional[int] = None) -> Dict[str, dict]:
    """
    Fetch Works for the given OpenAlex IDs (in 'W…' form, ints, or URLs).
    Returns mapping { 'Wxxxx': full_work_dict_with_plaintext_abstract_and_helpers }.

    - Enforces OpenAlex limit of <=100 IDs per request
    - Decodes abstract_inverted_index into 'abstract'
    - Pre-derives helper lists:
        * _authors_list
        * _institutions_list
        * _topics_list  (topics if present; else concepts)
        * _funders_list (from grants; fallback to funders)
    """
    # Respect OpenAlex limit
    per_call = min(per_call or MAX_IDS_PER_CALL, MAX_IDS_PER_CALL)

    ids = [normalize_oaid(x) for x in oaids if x is not None]
    ids = [x for x in ids if x]
    if not ids:
        return {}

    by_id: Dict[str, dict] = {}
    fields = _select_fields()

    for chunk in _chunked(ids, per_call):
        # Filter must use 'openalex_id' (not 'id')
        resp = Works().filter(openalex_id="|".join(chunk)).select(fields).get()

        for w in resp:
            # Prefer 'id' (URL), else ids.openalex; normalize to 'W…'
            raw = w.get("id") or ((w.get("ids") or {}).get("openalex"))
            wid = ""
            if isinstance(raw, str):
                wid = raw.rsplit("/", 1)[-1]

            # Decode abstract
            w["abstract"] = _decode_abstract(w.get("abstract_inverted_index"))

            # Collect authors and institutions
            authors: List[str] = []
            insts_set = set()
            for a in w.get("authorships", []) or []:
                auth = a.get("author") or {}
                nm = auth.get("display_name")
                if nm:
                    authors.append(nm)
                for inst in (a.get("institutions") or []):
                    inm = inst.get("display_name")
                    if inm:
                        insts_set.add(inm)

            # Topics (fallback to concepts)
            if w.get("topics"):
                tset = {t.get("display_name") for t in w["topics"] if t.get("display_name")}
            else:
                tset = {c.get("display_name") for c in (w.get("concepts") or []) if c.get("display_name")}

            # Funders/award IDs from awards (preferred) with backward-compatible grants fallback
            award_ids = []
            funders = set()

            # Newer OpenAlex field
            for g in (w.get("awards") or []):
                award = g.get("award_id") or g.get("id")
                if award:
                    award_ids.append(str(award))
                fname = g.get("funder_display_name") or g.get("funder") or g.get("funder_name")
                if fname:
                    funders.add(str(fname))

            # Backward compatibility if API/library still returns grants
            for g in (w.get("grants") or []):
                award = g.get("award_id")
                if award:
                    award_ids.append(str(award))
                fname = g.get("funder_display_name") or g.get("funder")
                if fname:
                    funders.add(str(fname))

            # Supplement from top-level funders list
            for f in (w.get("funders") or []):
                if isinstance(f, dict):
                    fname = f.get("display_name") or f.get("name")
                    if fname:
                        funders.add(str(fname))

            w["_authors_list"] = authors
            w["_institutions_list"] = sorted(insts_set)
            w["_topics_list"] = sorted(tset)
            w["_funders_list"] = sorted(funders)
            w["_grants_award_ids"] = sorted(set(award_ids))

            if wid:
                by_id[wid] = w

        if BATCH_SLEEP > 0:
            time.sleep(BATCH_SLEEP)

    return by_id


def _normalize_institution_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    lower = value.lower()
    if lower.startswith("https://openalex.org/"):
        value = value.rsplit("/", 1)[-1]
    value = value.upper()
    if value.startswith("I"):
        return value
    return None


def _lookup_institution_id_by_name(name: str) -> Optional[str]:
    """
    Resolve a display name to an OpenAlex institution id (Ixxxx).
    """
    params = {"search": name, "per-page": 1}
    if email:
        params["mailto"] = email
    if api_key:
        params["api_key"] = api_key

    url = f"{OPENALEX_BASE_URL}/institutions"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    raw = results[0].get("id") or ((results[0].get("ids") or {}).get("openalex"))
    return _normalize_institution_id(raw)


def _build_institution_filter(raw: str) -> str:
    """
    Build an OpenAlex filter string for institution lookups.
    Accepts:
      - OpenAlex institution IDs (I12345...)
      - Full OpenAlex URLs
      - ROR URLs
      - Fallback: display_name search (best-effort via /institutions search)
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Institution name/id is required.")
    lower = value.lower()
    inst_id = _normalize_institution_id(value)
    if inst_id:
        return f"institutions.id:{inst_id}"
    if lower.startswith("https://ror.org/"):
        return f"institutions.ror:{value}"
    inst_id = _lookup_institution_id_by_name(value)
    if inst_id:
        return f"institutions.id:{inst_id}"
    # Last resort: fall back to display_name search
    return f'institutions.display_name.search:"{value}"'


def fetch_institution_oaids(
    institution: str,
    *,
    max_works: Optional[int] = None,
    per_page: Optional[int] = None,
) -> List[str]:
    """
    Fetch OpenAlex work IDs (OAIDs) for a given institution/university.

    Returns a list of normalized OAIDs (W-prefixed strings), limited by max_works.
    """
    cap = max_works if max_works is not None else OPENALEX_UNIVERSITY_MAX_WORKS
    per_page = per_page or OPENALEX_UNIVERSITY_PAGE_SIZE
    per_page = max(1, min(per_page, 200))

    filter_str = _build_institution_filter(institution)
    params = {
        "filter": filter_str,
        "per-page": per_page,
        "cursor": "*",
        "select": "id",
    }
    if email:
        params["mailto"] = email
    if api_key:
        params["api_key"] = api_key

    url = f"{OPENALEX_BASE_URL}/works"
    collected: List[str] = []
    cursor = "*"
    session = requests.Session()

    while cursor and (cap is None or len(collected) < cap):
        params["cursor"] = cursor
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results") or []
        for item in results:
            wid = normalize_oaid(item.get("id"))
            if wid:
                collected.append(wid)
                if cap is not None and len(collected) >= cap:
                    break
        if cap is not None and len(collected) >= cap:
            break
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor or cursor in {"", "null"} or not results:
            break

    return collected
