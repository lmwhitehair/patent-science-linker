# src/pipeline/patentsview_client.py
from __future__ import annotations

import os
import time
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Set

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

PS_BASE = "https://search.patentsview.org/api/v1/patent/"
PV_BASE = "https://search.patentsview.org/api/v1"
PV_PATENT_ENDPOINT = f"{PV_BASE}/patent/"
PV_ASSIGNEE_ENDPOINT = f"{PV_BASE}/assignee/"

DEFAULT_PATENT_FIELDS: Sequence[str] = (
    "patent_id",
    "patent_date",
    "patent_title",
    "patent_abstract",
    "assignees.assignee_id",
    "assignees.assignee_organization",
)

DEFAULT_ASSIGNEE_FIELDS: Sequence[str] = (
    "assignee_id",
    "assignee_organization",
    "assignee_lastknown_state",
    "assignee_lastknown_country",
)


class CompanyPatentResult(NamedTuple):
    patents: List[dict]
    assignee_ids: List[str]
    alias_hits: Dict[str, List[dict]]
    fallback_used: bool


def _get_api_key() -> str | None:
    # Try common env var names; optionally load from .env if python-dotenv exists
    key = (
        os.getenv("PV_API_KEY")
        or os.getenv("PATENTSVIEW_API_KEY")
        or os.getenv("PATENTSEARCH_API_KEY")
    )
    if key:
        return key
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
        return (
            os.getenv("PV_API_KEY")
            or os.getenv("PATENTSVIEW_API_KEY")
            or os.getenv("PATENTSEARCH_API_KEY")
        )
    except Exception:
        return None


@retry(reraise=True, stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=15))
def _post(url: str, json: dict) -> dict:
    """
    POST helper with retries. Handles 429 politely and raises helpful errors for 400/403.
    """
    headers = {"Content-Type": "application/json"}
    api_key = _get_api_key()
    if api_key:
        headers["X-Api-Key"] = api_key

    r = requests.post(url, json=json, headers=headers, timeout=30)

    # Rate limit: respect Retry-After if present
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        try:
            sleep_s = float(retry_after) if retry_after else 2.0
        except Exception:
            sleep_s = 2.0
        time.sleep(sleep_s)
        # trigger retry
        raise requests.RequestException("429 rate limited")

    if r.status_code == 403:
        raise requests.HTTPError(
            "403 Forbidden from PatentsView API. Verify your X-Api-Key is correct and present."
        )

    if r.status_code == 410:
        # Treat discontinued endpoints as recoverable so callers can fall back.
        raise requests.HTTPError(
            "410 Gone from PatentsView API (endpoint discontinued)", response=r
        )

    if r.status_code == 400:
        # Surface the server's diagnostic headers if present
        reason = r.headers.get("X-Status-Reason") or r.text[:300]
        raise requests.HTTPError(f"400 Bad Request from PatentsView API: {reason}")

    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error") is True:
        raise requests.HTTPError("PatentsView API returned error=true")
    return data


def _chunk(seq: Sequence[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def _dedupe_aliases(names: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for name in names:
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _extract_total(data: dict) -> Optional[int]:
    for key in (
        "total_patent_count",
        "total_assignee_count",
        "total_count",
        "total_hits",
        "total_rows",
    ):
        val = data.get(key)
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return None


def _paginate_post(
    url: str,
    payload: dict,
    list_key: str,
    *,
    per_page: int,
    max_pages: int,
) -> Iterable[dict]:
    page = 1
    while page <= max_pages:
        body = dict(payload)
        options = dict(body.get("o") or {})
        options["per_page"] = per_page
        options["page"] = page
        body["o"] = options

        data = _post(url, body)
        if not isinstance(data, dict):
            break

        rows = data.get(list_key) or []
        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            yield row

        total = _extract_total(data)
        if total is not None and page * per_page >= total:
            break
        if len(rows) < per_page:
            break
        page += 1


def _assignee_query(alias: str, strategy: str) -> dict:
    if strategy == "phrase":
        return {"_text_phrase": {"assignee_organization": alias}}
    if strategy == "any":
        return {"_text_any": {"assignee_organization": alias}}
    if strategy == "begins":
        return {"_begins": {"assignee_organization": alias}}
    raise ValueError(f"Unknown strategy: {strategy}")


def _search_assignees_for_alias(
    alias: str,
    *,
    per_page: int,
    max_pages: int,
    strategy: str,
) -> List[dict]:
    payload = {
        "q": _assignee_query(alias, strategy),
        "f": list(DEFAULT_ASSIGNEE_FIELDS),
        "o": {"matched_subentities_only": True},
    }
    try:
        return list(
            _paginate_post(
                PV_ASSIGNEE_ENDPOINT,
                payload,
                "assignees",
                per_page=per_page,
                max_pages=max_pages,
            )
        )
    except requests.HTTPError as exc:
        # Some installations still reference the legacy assignee endpoint which now returns 410/500.
        # Treat these as soft failures so we can fall back to direct patent searches.
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code in {404, 410, 429, 500, 502, 503, 504}:
            return []
        raise


def _patent_query(alias: str, strategy: str) -> dict:
    if strategy == "phrase":
        return {"_text_phrase": {"assignee_organization": alias}}
    if strategy == "any":
        return {"_text_any": {"assignee_organization": alias}}
    raise ValueError(f"Unknown strategy: {strategy}")


def _search_patents_for_alias(
    alias: str,
    *,
    per_page: int,
    max_pages: int,
    fields: Sequence[str],
    strategy: str,
) -> Iterable[dict]:
    payload = {
        "q": _patent_query(alias, strategy),
        "f": list(fields),
        "o": {"matched_subentities_only": True},
    }
    yield from _paginate_post(
        PV_PATENT_ENDPOINT,
        payload,
        "patents",
        per_page=per_page,
        max_pages=max_pages,
    )


def fetch_patents_for_company(
    aliases: Iterable[str],
    *,
    limit: Optional[int] = None,
    fields: Optional[Sequence[str]] = None,
    assignee_per_page: int = 250,
    assignee_max_pages: int = 5,
    assignee_chunk_size: int = 25,
    patent_per_page: int = 1000,
    patent_max_pages: int = 40,
) -> CompanyPatentResult:
    """
    Resolve PatentsView assignee IDs for the provided aliases and return matching patents.

    Returns a CompanyPatentResult containing raw patent records, the distinct assignee IDs
    matched, alias hit details, and whether a direct patent-name fallback was required.
    """
    aliases_norm = _dedupe_aliases(aliases)
    if not aliases_norm:
        return CompanyPatentResult([], [], {}, False)

    selected_fields = list(fields or DEFAULT_PATENT_FIELDS)
    alias_hits: Dict[str, List[dict]] = {}

    assignee_ids: List[str] = []
    seen_assignees: Set[str] = set()
    for alias in aliases_norm:
        hits = _search_assignees_for_alias(
            alias,
            per_page=assignee_per_page,
            max_pages=assignee_max_pages,
            strategy="phrase",
        )
        if not hits:
            hits = _search_assignees_for_alias(
                alias,
                per_page=assignee_per_page,
                max_pages=assignee_max_pages,
                strategy="any",
            )
        alias_hits[alias] = hits
        for hit in hits:
            aid = str(hit.get("assignee_id") or "").strip()
            if aid and aid not in seen_assignees:
                seen_assignees.add(aid)
                assignee_ids.append(aid)

    patents: List[dict] = []
    seen_patents: Set[str] = set()

    def _maybe_add_patent(record: dict) -> bool:
        pid = str(record.get("patent_id") or "").strip()
        if not pid or pid in seen_patents:
            return False
        patents.append(record)
        seen_patents.add(pid)
        return True

    if assignee_ids:
        for chunk in _chunk(assignee_ids, assignee_chunk_size):
            payload = {
                "q": {"assignee_id": {"_in": chunk}},
                "f": selected_fields,
                "o": {"matched_subentities_only": True},
            }
            for rec in _paginate_post(
                PV_PATENT_ENDPOINT,
                payload,
                "patents",
                per_page=patent_per_page,
                max_pages=patent_max_pages,
            ):
                added = _maybe_add_patent(rec)
                if limit and added and len(patents) >= limit:
                    break
            if limit and len(patents) >= limit:
                break

    fallback_aliases = [alias for alias, hits in alias_hits.items() if not hits]
    fallback_used = False

    if not patents or fallback_aliases:
        targets = fallback_aliases or aliases_norm
        for alias in targets:
            for strat in ("phrase", "any"):
                for rec in _search_patents_for_alias(
                    alias,
                    per_page=patent_per_page,
                    max_pages=patent_max_pages,
                    fields=selected_fields,
                    strategy=strat,
                ):
                    added = _maybe_add_patent(rec)
                    fallback_used = True
                    if limit and added and len(patents) >= limit:
                        break
                if limit and len(patents) >= limit:
                    break
            if limit and len(patents) >= limit:
                break

    return CompanyPatentResult(patents, assignee_ids, alias_hits, fallback_used)


def fetch_kinds_for_patent_numbers(us_patent_numbers: Iterable[str], batch_size: int = 1000) -> Dict[str, str]:
    """
    Return a mapping {patent_id -> wipo_kind} for the given US patent numbers.

    PatentSearch differences vs legacy PatentsView:
      - Use q={"patent_id": [...]} (NO `_in`)
      - Field name is `wipo_kind` (NOT `patent_kind`)
      - Endpoint is POST https://search.patentsview.org/api/v1/patent/
    """
    nums = [str(n).strip() for n in us_patent_numbers if str(n).strip()]
    out: Dict[str, str] = {}

    if not nums:
        return out

    for chunk in _chunk(nums, batch_size):
        body = {
            "q": {"patent_id": chunk},  # <-- array value, no `_in`
            "f": ["patent_id", "wipo_kind"],  # <-- new field name
            "o": {"size": len(chunk)},  # up to 1000 per request
        }
        data = _post(PS_BASE, body)
        for p in data.get("patents", []):
            pid = str(p.get("patent_id") or "").strip()
            kind = str(p.get("wipo_kind") or "").strip().upper()
            if pid:
                out[pid] = kind

    return out
