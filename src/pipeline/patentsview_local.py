from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import duckdb
import pandas as pd

from .normalizer import build_pubnorm


@dataclass(frozen=True)
class PatentsViewLocalResult:
    pubnorms: List[str]
    metadata_by_pubnorm: Dict[str, Dict[str, str]]
    matched_patent_count: int
    missing_kind_count: int


def _resolve_data_dir(data_dir: Optional[str | os.PathLike[str]]) -> Path:
    """
    Resolve the PatentsView data directory. Falls back to data/patentsview relative to repo root.
    """
    if data_dir:
        candidate = Path(data_dir)
    else:
        candidate = Path(__file__).resolve().parents[2] / "data" / "patentsview"
    if not candidate.exists():
        raise FileNotFoundError(f"PatentsView data directory not found: {candidate}")
    return candidate


def _require_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required PatentsView file not found: {path}")


def _chunked(seq: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _collect_patent_ids(
    con: duckdb.DuckDBPyConnection,
    assignee_path: Path,
    aliases: Sequence[str],
    *,
    alias_batch_size: int = 100,
    match: str = "exact",
) -> List[str]:
    """
    Return distinct patent_ids for the provided aliases (lowercased). Order is ascending numeric/string.
    """
    if not aliases:
        return []

    patent_ids: List[str] = []
    match_mode = (match or "exact").lower()
    for chunk in _chunked(aliases, alias_batch_size):
        df_alias = pd.DataFrame({"alias": chunk})
        con.register("alias_chunk", df_alias)
        if match_mode == "contains":
            predicate = (
                "lower(trim(a.disambig_assignee_organization)) LIKE "
                "('%' || alias_chunk.alias || '%')"
            )
        else:
            predicate = "lower(trim(a.disambig_assignee_organization)) = alias_chunk.alias"
        sql = f"""
            SELECT DISTINCT a.patent_id
            FROM read_csv_auto(?, delim='\t', header=TRUE, quote='"', sample_size=-1,
                               types={{'patent_id':'VARCHAR',
                                      'disambig_assignee_organization':'VARCHAR'}}) AS a
            JOIN alias_chunk
              ON {predicate}
            ORDER BY a.patent_id
        """
        rows = con.execute(sql, [assignee_path.as_posix()]).fetchall()
        patent_ids.extend(row[0] for row in rows if row and row[0])
        con.unregister("alias_chunk")

    # de-duplicate while preserving order from ORDER BY
    seen: set[str] = set()
    unique_ids: List[str] = []
    for pid in patent_ids:
        pid = pid.strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        unique_ids.append(pid)
    return unique_ids


def _fetch_wipo_kinds(
    con: duckdb.DuckDBPyConnection,
    patent_path: Path,
    patent_ids: Sequence[str],
) -> pd.DataFrame:
    if not patent_ids:
        return pd.DataFrame(columns=["patent_id", "wipo_kind"])

    df_patents = pd.DataFrame({"patent_id": patent_ids})
    con.register("target_patents", df_patents)
    sql = """
        SELECT t.patent_id, COALESCE(p.wipo_kind, '') AS wipo_kind
        FROM target_patents AS t
        LEFT JOIN read_csv_auto(?, delim='\t', header=TRUE, quote='"', sample_size=-1,
                                types={'patent_id':'VARCHAR', 'wipo_kind':'VARCHAR'}) AS p
          USING (patent_id)
    """
    df = con.execute(sql, [patent_path.as_posix()]).fetchdf()
    con.unregister("target_patents")
    return df


def _fetch_wipo_taxonomy(
    con: duckdb.DuckDBPyConnection,
    wipo_path: Path,
    patent_ids: Sequence[str],
) -> pd.DataFrame:
    if not patent_ids:
        return pd.DataFrame(columns=["patent_id", "wipo_sector_title", "wipo_field_title"])

    df_patents = pd.DataFrame({"patent_id": patent_ids})
    con.register("target_patents", df_patents)
    sql = """
        SELECT
            t.patent_id,
            string_agg(DISTINCT NULLIF(trim(w.wipo_sector_title), ''), '; ') AS wipo_sector_title,
            string_agg(DISTINCT NULLIF(trim(w.wipo_field_title), ''), '; ') AS wipo_field_title
        FROM read_csv_auto(?, delim='\t', header=TRUE, quote='"', sample_size=-1,
                           types={'patent_id':'VARCHAR',
                                  'wipo_sector_title':'VARCHAR',
                                  'wipo_field_title':'VARCHAR'}) AS w
        JOIN target_patents AS t USING (patent_id)
        GROUP BY t.patent_id
    """
    df = con.execute(sql, [wipo_path.as_posix()]).fetchdf()
    con.unregister("target_patents")
    return df


def collect_pubnorms_from_local_patentsview(
    aliases: Sequence[str],
    *,
    limit: Optional[int] = None,
    data_dir: Optional[str | os.PathLike[str]] = None,
    match: str = "exact",
) -> PatentsViewLocalResult:
    """
    Build PatentsView pubnorms and metadata for the provided aliases using local TSV files.
    """
    norm_aliases = [
        alias.strip().lower()
        for alias in aliases
        if alias and alias.strip()
    ]
    # Preserve original order but drop duplicates
    seen_alias: set[str] = set()
    dedup_aliases: List[str] = []
    for alias in norm_aliases:
        if alias not in seen_alias:
            seen_alias.add(alias)
            dedup_aliases.append(alias)

    if not dedup_aliases:
        return PatentsViewLocalResult([], {}, 0, 0)

    match_mode = (match or "exact").lower()
    if match_mode not in {"exact", "contains"}:
        raise ValueError("match must be 'exact' or 'contains'")

    pv_dir = _resolve_data_dir(data_dir)
    assignee_path = pv_dir / "g_assignee_disambiguated.tsv"
    patent_path = pv_dir / "g_patent.tsv"
    wipo_path = pv_dir / "g_wipo_technology.tsv"

    _require_file(assignee_path)
    _require_file(patent_path)
    _require_file(wipo_path)

    con = duckdb.connect()
    try:
        patent_ids = _collect_patent_ids(con, assignee_path, dedup_aliases, match=match_mode)
        if limit is not None and limit >= 0:
            patent_ids = patent_ids[: limit]

        if not patent_ids:
            return PatentsViewLocalResult([], {}, 0, 0)

        df_kinds = _fetch_wipo_kinds(con, patent_path, patent_ids)
        df_wipo = _fetch_wipo_taxonomy(con, wipo_path, patent_ids)

        df = pd.DataFrame({"patent_id": patent_ids})
        if not df_kinds.empty:
            df = df.merge(df_kinds, on="patent_id", how="left")
        else:
            df["wipo_kind"] = ""

        if not df_wipo.empty:
            df = df.merge(df_wipo, on="patent_id", how="left")
        else:
            df["wipo_sector_title"] = ""
            df["wipo_field_title"] = ""

        df["wipo_kind"] = df["wipo_kind"].fillna("").astype(str).str.strip().str.upper()
        df["wipo_sector_title"] = df["wipo_sector_title"].fillna("").astype(str).str.strip()
        df["wipo_field_title"] = df["wipo_field_title"].fillna("").astype(str).str.strip()

        pubnorms: List[str] = []
        metadata: Dict[str, Dict[str, str]] = {}
        seen_pubnorms: set[str] = set()
        missing_kind = 0

        for row in df.itertuples(index=False):
            patent_id = str(row.patent_id).strip()
            if not patent_id:
                continue
            kind = getattr(row, "wipo_kind", "") or ""
            sector = getattr(row, "wipo_sector_title", "") or ""
            field = getattr(row, "wipo_field_title", "") or ""

            base_key = build_pubnorm("US", patent_id, None).lower()
            payload = {
                "patent_id": patent_id,
                "wipo_kind": kind,
                "wipo_sector_title": sector,
                "wipo_field_title": field,
            }
            metadata[base_key] = payload

            if kind:
                key = build_pubnorm("US", patent_id, kind).lower()
                metadata[key] = payload
            else:
                missing_kind += 1
                key = base_key

            if key not in seen_pubnorms:
                pubnorms.append(key)
                seen_pubnorms.add(key)

        return PatentsViewLocalResult(pubnorms, metadata, len(df), missing_kind)
    finally:
        con.close()
