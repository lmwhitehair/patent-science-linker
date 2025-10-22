import os
from typing import Iterable, List, Optional, Tuple
import duckdb
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

def connect(db_path: Optional[str] = None) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path or ":memory:")

def ensure_view(con: duckdb.DuckDBPyConnection, path: str, fmt: str = "parquet") -> None:
    fmt = (fmt or "parquet").lower()
    if fmt == "parquet":
        con.sql(f"CREATE OR REPLACE VIEW pcs AS SELECT * FROM read_parquet('{path}');")
    elif fmt == "csv":
        con.sql(f"CREATE OR REPLACE VIEW pcs AS SELECT * FROM read_csv_auto('{path}', HEADER=TRUE);")
    else:
        raise ValueError("Unsupported PCS_FORMAT. Use 'parquet' or 'csv'.")

def chunked(iterable: Iterable, size: int) -> Iterable[List]:
    chunk = []
    for x in iterable:
        chunk.append(x)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk

def get_oaids_for_pubnorms(
    pubnorms: List[str],
    confscore_min: int = 8,
    wherefound: Optional[List[str]] = None,
    reftype: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    pcs_path: Optional[str] = None,
    pcs_format: Optional[str] = None,
    batch_size: int = 5000,
) -> pd.DataFrame:
    """Return distinct OAIDs with evidence rows for the provided pubnorms.


    Columns: patent, oaid, confscore, reftype, wherefound
    """
    con = connect(db_path)
    pcs_path = pcs_path or os.getenv("PCS_PATH", "pcs_oa.parquet")
    pcs_format = pcs_format or os.getenv("PCS_FORMAT", "parquet")
    ensure_view(con, pcs_path, pcs_format)

    where_sql = []
    params = []

    where_sql.append("confscore >= ?")
    params.append(confscore_min)

    if wherefound:
        placeholders = ", ".join(["?"] * len(wherefound))
        where_sql.append(f"wherefound IN ({placeholders})")
        params.extend(wherefound)

    if reftype:
        placeholders = ", ".join(["?"] * len(reftype))
        where_sql.append(f"reftype IN ({placeholders})")
        params.extend(reftype)

    where_clause_core = " AND ".join(where_sql) if where_sql else "1=1"

    frames = []
    for group in chunked(pubnorms, batch_size):
        ph = ", ".join(["?"] * len(group))
        sql = f"""            SELECT patent, oaid, confscore, reftype, wherefound
            FROM pcs
            WHERE patent IN ({ph}) AND {where_clause_core}
        """
        df = con.sql(sql, params=group + params).df()
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["patent", "oaid", "confscore", "reftype", "wherefound"])

    out = pd.concat(frames, ignore_index=True)
    return out

def distinct_oaids(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop_duplicates(subset=["oaid"])[["oaid"]].sort_values("oaid").reset_index(drop=True)
