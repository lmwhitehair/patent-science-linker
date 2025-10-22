# src/pipeline/openalex_client.py
from __future__ import annotations

import os
import time
from itertools import islice
from typing import Dict, Iterable, List, Optional, Union

import pyalex
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
        "grants",
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

            # Funders from grants; fallback to funders list
            award_ids = []
            funders = set()
            for g in (w.get("grants") or []):
                award = g.get("award_id")
                if award:
                    award_ids.append(award)
                fname = g.get("funder_display_name") or g.get("funder")
                if fname:
                    funders.add(fname)

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