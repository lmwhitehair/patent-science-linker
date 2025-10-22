import re
from typing import Optional, Tuple

# Patterns: US "11,426,570" or "2012/0172239" etc.
_COMMA_RE = re.compile(r"[,\s]+")
_WS_RE = re.compile(r"\s+")
_SLASH_RE = re.compile(r"/+")

def normalize_jurisdiction(j: str) -> str:
    return (j or "").strip().lower()

def normalize_kind(k: Optional[str]) -> str:
    if not k:
        return ""
    return k.strip().lower()

def normalize_doc_number(num: str) -> str:
    """
    Normalize doc_number like '11,426,570' -> '11426570'
    Application-style '2012/0172239' -> '2012/0172239' (keep slash for apps).
    """
    s = num.strip()
    # preserve slashes for application-style numbers, but drop spaces/commas
    s = _COMMA_RE.sub("", s)
    s = _WS_RE.sub("", s)
    # collapse multiple slashes
    s = _SLASH_RE.sub("/", s)
    return s

def build_pubnorm(jurisdiction: str, doc_number: str, kind: Optional[str]) -> str:
    """
    Build canonical key matching the citations dataset:
      'us-11426570-b2' or 'wo-2022020260-a1' or 'us-2009/0302153-a1'
    """
    j = normalize_jurisdiction(jurisdiction)
    n = normalize_doc_number(doc_number)
    k = normalize_kind(kind)
    parts = [j, n]
    if k:
        parts.append(k)
    return "-".join(parts)

def from_lens_publication_reference(pubref: dict) -> Optional[str]:
    """
    pubref is a dict like:
      {"jurisdiction":"US","doc_number":"11426570","kind":"B2","date":"2022-08-23"}
    """
    if not pubref:
        return None
    j = pubref.get("jurisdiction")
    n = pubref.get("doc_number")
    k = pubref.get("kind")
    if not j or not n:
        return None
    return build_pubnorm(j, n, k)
