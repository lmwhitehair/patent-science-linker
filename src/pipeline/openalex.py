import os
from typing import Iterable, List, Optional, Dict
import requests
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

OPENALEX_BASE = "https://api.openalex.org/works"

def _mailto() -> str:
    email = os.getenv("OPENALEX_EMAIL", "").strip()
    return f"&mailto={email}" if email else ""

@retry(
    reraise=True,
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
)
def fetch_work(oaid: str) -> Dict:
    # OAIDs in your dataset are numeric; OpenAlex wants "W{oaid}"
    oid = f"W{oaid}".upper()
    url = f"{OPENALEX_BASE}/{oid}?per-page=1{_mailto()}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()

def enrich_oaids(oaids: Iterable[str]) -> pd.DataFrame:
    rows = []
    for oid in oaids:
        try:
            data = fetch_work(oid)
            rows.append({
                "oaid": oid,
                "title": data.get("display_name"),
                "publication_year": data.get("publication_year"),
                "doi": (data.get("ids") or {}).get("doi"),
                "host_venue": ((data.get("host_venue") or {}).get("display_name")),
            })
        except Exception as e:
            rows.append({"oaid": oid, "title": None, "publication_year": None, "doi": None, "host_venue": None})
    return pd.DataFrame(rows)
