# src/pipeline/uspto_assignment_client.py
from __future__ import annotations

import os
import time
import logging
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import xml.etree.ElementTree as ET

logger = logging.getLogger("uspto_assignment")
logger.setLevel(logging.INFO)

# ---------- Configuration ----------
USPTO_ASSIGN_BASE = os.getenv("USPTO_ASSIGN_BASE", "https://assignment-api.uspto.gov")
USPTO_ASSIGN_SEARCH = os.getenv("USPTO_ASSIGN_SEARCH_PATH", "/patent/basicSearch")  # FIXED PATH

DEFAULT_TIMEOUT = int(os.getenv("USPTO_HTTP_TIMEOUT", "30"))
MAX_PER_PAGE = int(os.getenv("USPTO_ASSIGN_PAGE_SIZE", "100"))
MAX_PAGES = int(os.getenv("USPTO_ASSIGN_MAX_PAGES", "500"))

# Assignment Center (new) configuration
USPTO_AC_BASE = os.getenv(
    "USPTO_AC_BASE",
    "https://assignmentcenter.uspto.gov/ipas/search/api/v2/public",
).rstrip("/")
USPTO_AC_SEARCH_PATH = os.getenv("USPTO_AC_SEARCH_PATH", "/search/patent")


# ---------- Helpers ----------
class USPTOAssignmentAPIError(Exception):
    """Generic non-HTTP client error."""


def _build_url() -> str:
    base = USPTO_ASSIGN_BASE.rstrip("/")
    path = USPTO_ASSIGN_SEARCH
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _common_headers() -> Dict[str, str]:
    # Some USPTO endpoints behave differently without a UA
    ua = os.getenv("USPTO_HTTP_USER_AGENT", "patent-science-linker/0.1").strip()
    return {"User-Agent": ua}


def _headers_xml() -> Dict[str, str]:
    tok = os.getenv("USPTO_ASSIGN_TOKEN", "").strip()
    h = {"Accept": "application/xml", **_common_headers()}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _headers_json() -> Dict[str, str]:
    tok = os.getenv("USPTO_ASSIGN_TOKEN", "").strip()
    h = {"Accept": "application/json", **_common_headers()}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h

# ---- Assignment Center helpers (JSON) ----
def _ac_headers() -> Dict[str, str]:
    """Headers for Assignment Center public API.

    Uses Origin/Referer to satisfy WAF, optional Cookie and X-Api-Key if required by environment.
    """
    h: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": os.getenv("USPTO_AC_ORIGIN", "https://assignmentcenter.uspto.gov"),
        "Referer": os.getenv(
            "USPTO_AC_REFERER", "https://assignmentcenter.uspto.gov/search/patent"
        ),
        **_common_headers(),
    }
    cookie = os.getenv("USPTO_AC_COOKIE", "").strip()
    if cookie:
        h["Cookie"] = cookie
    api_key = os.getenv("USPTO_ASSIGN_TOKEN", "").strip()
    if api_key:
        # Only add if your deployment requires it; harmless otherwise
        h["X-Api-Key"] = api_key
    return h


@retry(reraise=True, stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=15))
def _ac_post(path: str, payload: dict) -> dict:
    url = f"{USPTO_AC_BASE}/{path.lstrip('/')}"
    r = requests.post(url, headers=_ac_headers(), json=payload, timeout=30)
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        try:
            sleep_s = float(retry_after) if retry_after else 2.0
        except Exception:
            sleep_s = 2.0
        _sleep_backoff(sleep_s)
        raise requests.RequestException("429 rate limited (Assignment Center)")
    r.raise_for_status()
    # AC always returns JSON; still guard against HTML just in case
    if _looks_like_html(r):
        raise requests.HTTPError("Assignment Center returned HTML content")
    return r.json()


def _ac_response_to_list(data: dict) -> tuple[list[dict], int]:
    """Normalize Assignment Center response to (items, total_rows).

    Response example can be a dict or a list with one dict. We handle both.
    """
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return [], 0
    sr = data.get("successResponse") or {}
    items = sr.get("data") or []
    try:
        total = int(sr.get("totalRows") or 0)
    except Exception:
        total = 0
    parsed = [x for x in items if isinstance(x, dict)]
    return parsed, total


def iterate_assignments_by_party_ac(
    party_names: Iterable[str], *, rows_per_page: int = 100, max_pages: int = MAX_PAGES
) -> Iterator[dict]:
    """Iterate Assignment Center records for the given party (assignee) names.

    Uses POST /search/patent with payload matching the UI's structure.
    """
    path = USPTO_AC_SEARCH_PATH
    for alias in party_names:
        page = 1
        pages = 0
        total_rows: Optional[int] = None
        while pages < max_pages:
            payload = {
                "dataFilter": {"filterBy": [], "rowsPerPage": rows_per_page, "currentPage": page},
                "searchCriteria": [
                    {
                        "property": alias,
                        "searchBy": "assigneeName",
                        "matchType": "Contains",
                        "order": 1,
                        "relation": "AND",
                    }
                ],
            }
            data = _ac_post(path, payload)
            items, total = _ac_response_to_list(data)
            if total_rows is None:
                total_rows = total
            if not items:
                break
            for it in items:
                yield it
            pages += 1
            page += 1
            if total_rows and (page - 1) * rows_per_page >= total_rows:
                break


def collect_numbers_for_parties_ac(party_names: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Collect patent and application numbers via Assignment Center search by assignee name."""
    all_p: List[str] = []
    all_a: List[str] = []
    for rec in iterate_assignments_by_party_ac(party_names):
        for prop in (rec.get("properties") or []):
            pn = str(prop.get("patentNumber") or "").strip()
            an = str(prop.get("applicationNumber") or "").strip()
            if pn:
                all_p.append(pn)
            if an:
                all_a.append(an)

    # De-dup preserving order
    def _uniq(seq: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for x in seq:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return _uniq(all_p), _uniq(all_a)


def _sleep_backoff(seconds: float = 1.0) -> None:
    try:
        time.sleep(min(5.0, max(0.1, seconds)))
    except Exception:
        pass


def _looks_like_html(resp: requests.Response) -> bool:
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            return True
        txt = (resp.text or "").lstrip()[:64].lower()
        return txt.startswith("<!doctype html") or txt.startswith("<html")
    except Exception:
        return False


@retry(
    reraise=True,
    retry=retry_if_exception_type((requests.RequestException,)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
)
def _get_xml(url: str, params: Dict[str, str]) -> requests.Response:
    resp = requests.get(url, headers=_headers_xml(), params=params, timeout=DEFAULT_TIMEOUT)
    if resp.status_code == 429:
        _sleep_backoff(2.0)
        raise requests.RequestException("429 rate limited")
    resp.raise_for_status()
    # Guard: some deployments may return an HTML page (portal/landing) with 200 OK
    if _looks_like_html(resp):
        raise requests.HTTPError("Expected XML but received HTML content")
    return resp


@retry(
    reraise=True,
    retry=retry_if_exception_type((requests.RequestException,)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
)
def _get_json(url: str, params: Dict[str, str]) -> requests.Response:
    resp = requests.get(url, headers=_headers_json(), params=params, timeout=DEFAULT_TIMEOUT)
    if resp.status_code == 429:
        _sleep_backoff(2.0)
        raise requests.RequestException("429 rate limited")
    resp.raise_for_status()
    # Guard: avoid attempting to json() an HTML page
    if _looks_like_html(resp):
        raise requests.HTTPError("Expected JSON but received HTML content")
    return resp


def _parse_xml_page(xml_bytes: bytes) -> tuple[List[dict], int]:
    """
    Parse the XML response into (list_of_docs, total_num_found).

    Typical structure is a Solr-style response where:
      <result name="response" numFound="1234" start="0"> ... <doc> ... </doc> ... </result>
    Each <doc> contains child nodes like:
      <str name="patentNumber">10006973</str>
      <arr name="applicationNumber"><str>14/123,456</str>...</arr>
      <str name="reelNumber">...</str>, <str name="frameNumber">...</str>, etc.
    """
    root = ET.fromstring(xml_bytes)

    # total numFound
    total = 0
    for res in root.iterfind(".//result"):
        nf = res.attrib.get("numFound")
        if nf:
            try:
                total = int(nf)
            except ValueError:
                total = 0
            break

    # flatten <doc> blocks
    docs: List[dict] = []
    for d in root.iterfind(".//doc"):
        item: dict = {}
        for node in d:
            name = node.attrib.get("name")
            if not name:
                continue
            if node.tag in ("str", "int", "long", "date"):
                item[name] = (node.text or "").strip()
            elif node.tag == "arr":
                vals = []
                for s in node.findall("./str"):
                    t = (s.text or "").strip()
                    if t:
                        vals.append(t)
                if vals:
                    item[name] = vals
        docs.append(item)

    return docs, total


def _parse_json_page(data: dict) -> tuple[List[dict], int]:
    """
    Some deployments/paths may return JSON. Normalize to (docs, total).
    We expect one of: 'docs', 'results', or 'data' arrays, and 'numFound'/'total'/'recordCount'.
    """
    total = 0
    for key in ("numFound", "total", "recordCount"):
        v = data.get(key)
        if isinstance(v, int):
            total = v
            break
        if isinstance(v, str) and v.isdigit():
            total = int(v)
            break

    docs = data.get("docs") or data.get("results") or data.get("data") or []
    parsed: List[dict] = []
    for d in docs:
        if isinstance(d, dict):
            parsed.append(d)
    return parsed, total


def _try_fetch_page(url: str, params: Dict[str, str]) -> tuple[List[dict], int]:
    """
    Try XML first (expected), then fallback to JSON if XML parsing fails.
    """
    # Prefer XML (documented for /patent/basicSearch)
    try:
        resp_xml = _get_xml(url, params)
        return _parse_xml_page(resp_xml.content)
    except Exception as e_xml:
        logger.debug(f"XML parse failed, trying JSON fallback: {e_xml}")
        # JSON fallback
        try:
            resp_json = _get_json(url, params)
            return _parse_json_page(resp_json.json())
        except Exception as e_json:
            # Surface a helpful diagnostic instead of crashing on JSONDecodeError
            snippet = ""
            ctype = "unknown"
            try:
                snippet = (resp_json.text or "")[:300]
                ctype = resp_json.headers.get("Content-Type", "unknown")
            except Exception:
                pass
            raise USPTOAssignmentAPIError(
                f"USPTO assignment API response not parseable (XML:{type(e_xml).__name__}, JSON:{type(e_json).__name__}). "
                f"Content-Type={ctype}. Snippet: {snippet}"
            )


# ---------- Public API ----------
def iterate_assignments_by_party(
    party_names: Iterable[str],
    *,
    page_size: int = MAX_PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> Iterator[dict]:
    """
    Iterate assignment 'docs' for any of the given party names.

    Strategy per alias:
      1) fielded query:   query=partyName:"<ALIAS>"
      2) loose fallback:  query="<ALIAS>"   (only if step 1 returned zero)
    Pagination via rows/start. De-dup by simple record key if present.
    """
    url = _build_url()

    def _run_one_query(q: str) -> Iterator[dict]:
        start = 0
        pages = 0
        while pages < max_pages:
            params = {"query": q, "rows": str(page_size), "start": str(start)}
            docs, total = _try_fetch_page(url, params)
            if not docs:
                break
            for d in docs:
                yield d
            start += page_size
            pages += 1
            if total and start >= total:
                break

    seen_ids: set[str] = set()
    for alias in party_names:
        # 1) partyName:"ALIAS"
        had_any = False
        for d in _run_one_query(f'partyName:"{alias}"'):
            key = d.get("id") or d.get("reelFrame") or (d.get("reelNumber", "") + "/" + d.get("frameNumber", ""))
            if key and key in seen_ids:
                continue
            if key:
                seen_ids.add(key)
            had_any = True
            yield d

        # 2) loose fallback if nothing matched
        if not had_any:
            for d in _run_one_query(f'"{alias}"'):
                key = d.get("id") or d.get("reelFrame") or (d.get("reelNumber", "") + "/" + d.get("frameNumber", ""))
                if key and key in seen_ids:
                    continue
                if key:
                    seen_ids.add(key)
                yield d


def extract_ids_from_assignment(doc: dict) -> Tuple[List[str], List[str]]:
    """
    Extract patent/application numbers from a flattened assignment <doc>.

    Common field names in USPTO assignment responses (XML normalized to dict):
      - patentNumber, patNum, patNo, pat_no
      - applicationNumber, applNum, appNo, app_no
      - Some responses present arrays for these fields.

    Returns:
      (patent_numbers: List[str], application_numbers: List[str])
    """

    def _get_many(keys: List[str]) -> List[str]:
        out: List[str] = []
        for k in keys:
            v = doc.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
            elif isinstance(v, list):
                out.extend([x.strip() for x in v if isinstance(x, str) and x.strip()])
        # de-dup preserving order
        seen: set[str] = set()
        res: List[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                res.append(x)
        return res

    pnums = _get_many(["patentNumber", "patNum", "patNo", "pat_no"])
    appnums = _get_many(["applicationNumber", "applNum", "appNo", "app_no"])
    return pnums, appnums


def collect_numbers_for_parties(party_names: Iterable[str]) -> Tuple[List[str], List[str]]:
    """
    Convenience wrapper:
      Iterate all assignment docs for the given party names (aliases),
      extract patent and application numbers, and return unique lists.

    Returns:
      (unique_patent_numbers, unique_application_numbers)
    """
    all_p: List[str] = []
    all_a: List[str] = []

    for doc in iterate_assignments_by_party(party_names):
        pnums, appnums = extract_ids_from_assignment(doc)
        if pnums:
            all_p.extend(pnums)
        if appnums:
            all_a.extend(appnums)

    # De-dup preserving order
    def _uniq(seq: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return _uniq(all_p), _uniq(all_a)
