# src/pipeline/patentsview_client.py
from __future__ import annotations

import os
import time
from typing import Dict, Iterable, List

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

PS_BASE = "https://search.patentsview.org/api/v1/patent/"

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
            "403 Forbidden from PatentSearch API. "
            "Verify your X-Api-Key is correct and present."
        )

    if r.status_code == 400:
        # Surface the server’s diagnostic headers if present
        reason = r.headers.get("X-Status-Reason") or r.text[:300]
        raise requests.HTTPError(f"400 Bad Request from PatentSearch API: {reason}")

    r.raise_for_status()
    data = r.json()
    # PatentSearch responses include {error, count, total_hits, patents: [...]}
    if isinstance(data, dict) and data.get("error") is True:
        raise requests.HTTPError("PatentSearch API returned error=true")
    return data

def _chunk(seq: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]

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
            "q": {"patent_id": chunk},          # <-- array value, no `_in`
            "f": ["patent_id", "wipo_kind"],    # <-- new field name
            "o": {"size": len(chunk)},          # up to 1000 per request
        }
        data = _post(PS_BASE, body)
        for p in data.get("patents", []):
            pid = str(p.get("patent_id") or "").strip()
            kind = str(p.get("wipo_kind") or "").strip().upper()
            if pid:
                out[pid] = kind

    return out