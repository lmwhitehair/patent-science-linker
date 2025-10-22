# src/pipeline/lens_client.py
from __future__ import annotations

import os
import time
import logging
from typing import Dict, Iterator, List, Optional

import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

logger = logging.getLogger("lens")
logger.setLevel(logging.INFO)

LENS_BASE = "https://api.lens.org"
PATENT_SEARCH_POST = f"{LENS_BASE}/patent/search"
PATENT_GET = f"{LENS_BASE}/patent"

DEFAULT_HEADERS = {"Content-Type": "application/json"}
MAX_PAGE = int(os.getenv("LENS_MAX_PAGE", "100"))  # Lens per-request cap (usually 100)


class LensAPIError(Exception):
    pass


class TransientLensError(Exception):
    """Retryable (429, 5xx, network)."""
    pass


class PermanentLensError(Exception):
    """Do NOT retry (400/401/403/404/415/etc.)."""
    pass


def _log_payload(tag: str, payload: dict) -> None:
    size = payload.get("size")
    off = payload.get("from")
    has_scroll_id = "scroll_id" in payload
    logger.info(f"{tag} Lens POST size={size} from={off} scroll_id={has_scroll_id}")


def _auth_headers() -> Dict[str, str]:
    token = os.getenv("LENS_API_TOKEN", "").strip()
    if not token:
        raise LensAPIError("Missing LENS_API_TOKEN in environment (.env).")
    return {"Authorization": f"Bearer {token}", **DEFAULT_HEADERS}


def _extract_rate_headers(resp: requests.Response) -> Dict[str, str]:
    keys = [
        "x-rate-limit-remaining-request-per-minute",
        "x-rate-limit-retry-after-seconds",
        "x-rate-limit-retry-after-millis",
        "x-rate-limit-reset-date",
        "x-rate-limit-remaining-request-per-month",
        "x-rate-limit-remaining-record-per-month",
        "Retry-After",
    ]
    return {k: resp.headers.get(k) for k in keys}


@retry(
    reraise=True,
    retry=retry_if_exception_type((requests.RequestException, TransientLensError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
)
def _post(url: str, json_payload: dict) -> requests.Response:
    _log_payload("patent/search", json_payload)
    resp = requests.post(url, headers=_auth_headers(), json=json_payload, timeout=30)

    # Handle rate limits
    if resp.status_code == 429:
        hdrs = _extract_rate_headers(resp)
        monthly_remaining = hdrs.get("x-rate-limit-remaining-record-per-month")
        if monthly_remaining == "0":
            # Monthly cap reached — do not retry
            raise PermanentLensError(f"429 monthly record cap reached: {hdrs}")
        ra = hdrs.get("x-rate-limit-retry-after-seconds") or hdrs.get("Retry-After")
        # small, bounded sleep then retry
        try:
            ra_int = int(ra) if ra else 5
        except Exception:
            ra_int = 5
        time.sleep(min(5, ra_int))
        raise TransientLensError(f"429: {hdrs}")

    # 5xx → transient
    if 500 <= resp.status_code < 600:
        raise TransientLensError(f"Lens 5xx {resp.status_code}: {resp.text[:300]}")

    # 204 → end of scroll
    if resp.status_code == 204:
        return resp

    # other non-ok → permanent
    if not resp.ok:
        raise PermanentLensError(f"Lens POST {resp.status_code}: {resp.text[:300]}")

    return resp


def build_company_query(
    company: str,
    exact: bool = True,
    use_owner: bool = True,
    use_applicant: bool = True,
    resolved_npl_only: bool = False,
) -> dict:
    """Kept for compatibility; simple single-company query builder."""
    field_suffix = ".exact" if exact else ""
    shoulds = []
    if use_owner:
        shoulds.append({"match": {f"owner_all.name{field_suffix}": company}})
    if use_applicant:
        shoulds.append({"match": {f"applicant.name{field_suffix}": company}})
    if not shoulds:
        raise ValueError("At least one of use_owner/use_applicant must be True.")
    must = [{"bool": {"should": shoulds}}]
    if resolved_npl_only:
        must.append({"match": {"cites_resolved_npl": True}})
    return {"query": {"bool": {"must": must}}}


def count_only(payload: dict) -> int:
    """
    Cheap count probe (size=1). Returns total/results if present, else 0.
    Safe for both grouped and ungrouped queries.
    """
    tmp = dict(payload)
    tmp["size"] = 1
    # 'from' and 'scroll' not needed for counting
    tmp.pop("from", None)
    tmp.pop("scroll", None)
    resp = _post(PATENT_SEARCH_POST, tmp)
    if resp.status_code == 204:
        return 0
    data = resp.json()
    # Lens may return "total" OR "results"
    for key in ("total", "results"):
        v = data.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 0


def _compute_goal_and_page(desired_size: int | None, limit: int | None) -> tuple[int | None, int]:
    """
    Decide overall goal count and per-request page size.
    goal = min(size, limit) if both present; else whichever is set; else None (unbounded).
    page_size is capped at MAX_PAGE.
    """
    goal = None
    if desired_size is not None and limit is not None:
        goal = min(desired_size, limit)
    elif desired_size is not None:
        goal = desired_size
    elif limit is not None:
        goal = limit
    page_size = MAX_PAGE if goal is None else min(goal, MAX_PAGE)
    return goal, page_size


def iterate_patents_offset(query, size: int | None = None, include_fields=None, sort=None, limit: int | None = None):
    goal, page_size = _compute_goal_and_page(size, limit)
    payload = {"query": query["query"], "size": page_size, "from": 0}
    if include_fields:
        payload["include"] = include_fields
    if sort:
        payload["sort"] = sort

    seen, offset = 0, 0
    while True:
        payload["from"] = offset
        resp = _post(PATENT_SEARCH_POST, payload)
        if resp.status_code == 204:
            return
        data = resp.json()
        rows = data.get("data", [])
        if not rows:
            return
        for r in rows:
            yield r
            seen += 1
            if goal is not None and seen >= goal:
                return
        offset += page_size
        if offset >= 10000:  # offset ceiling
            return


def iterate_patents_scroll(query, size: int | None = None, include_fields=None, limit: int | None = None, scroll_minutes: int = 1):
    goal, page_size = _compute_goal_and_page(size, limit)
    payload = {"query": query["query"], "size": page_size, "scroll": f"{scroll_minutes}m"}
    if include_fields:
        payload["include"] = include_fields

    resp = _post(PATENT_SEARCH_POST, payload)
    if resp.status_code == 204:
        return
    data = resp.json()
    rows, seen = data.get("data", []), 0

    while rows:
        for r in rows:
            yield r
            seen += 1
            if goal is not None and seen >= goal:
                return
        sid = data.get("scroll_id")
        if not sid:
            return
        resp = _post(PATENT_SEARCH_POST, {"scroll_id": sid, "scroll": f"{scroll_minutes}m"})
        if resp.status_code == 204:
            return
        data = resp.json()
        rows = data.get("data", [])


# Convenience API (kept in case other modules still import it)
def fetch_company_patents(
    company: str,
    exact: bool = True,
    use_owner: bool = True,
    use_applicant: bool = True,
    resolved_npl_only: bool = False,
    use_scroll: bool = False,
    size: int | None = None,
    limit: Optional[int] = None,
    include_minimal: bool = True,
) -> List[dict]:
    """Return a list of patent records (minimal fields by default) for a simple single-company request."""
    query = build_company_query(
        company=company,
        exact=exact,
        use_owner=use_owner,
        use_applicant=use_applicant,
        resolved_npl_only=resolved_npl_only,
    )
    include_fields = None
    if include_minimal:
        include_fields = [
            "lens_id",
            "jurisdiction",
            "doc_number",
            "kind",
            "biblio.publication_reference",
            "families.simple_family.members",
            "families.extended_family.members",
        ]
    it = iterate_patents_scroll if use_scroll else iterate_patents_offset
    return list(it(query, size=size, limit=limit, include_fields=include_fields))