# src/pipeline/main.py
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import os
import pandas as pd
import typer
from dotenv import load_dotenv

from .company_query import seed_aliases
from .normalizer import build_pubnorm, normalize_doc_number, from_lens_publication_reference
from .uspto_assignment_client import collect_numbers_for_parties_ac as collect_numbers_for_parties
from .openalex_client import fetch_works_by_ids
from .lens_client import fetch_company_patents

# optional enrichment
try:
    from .patentsview_client import fetch_kinds_for_patent_numbers
    HAS_PATENTSVIEW = True
except Exception:
    HAS_PATENTSVIEW = False


app = typer.Typer(add_completion=False)
load_dotenv()


def outdir() -> Path:
    d = Path(__file__).resolve().parents[2] / "data" / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.command()
def run(
    company: str = typer.Option(..., help="Company/assignee/owner name (exact-match variants generated)."),
    confscore: int = typer.Option(8, help="Minimum confscore to accept a citation match."),
    aliases_file: str | None = typer.Option(None, help="Extra aliases (one per line; '#' comments allowed)."),
    include_predecessors: bool = typer.Option(False, help="Include known predecessor names (mergers/legacy)."),
    limit: Optional[int] = typer.Option(None, help="Overall max patent records (post de-dup)."),
    pcs_path: Optional[str] = typer.Option(None, help="Override PCS path (else env PCS_PATH)."),
    pcs_format: Optional[str] = typer.Option(None, help="parquet or csv (else env PCS_FORMAT)."),
    wherefound: Optional[List[str]] = typer.Option(None, help="Filter citations by wherefound (repeatable)."),
    reftype: Optional[List[str]] = typer.Option(None, help="Filter citations by reftype (repeatable)."),
    enrich_kind: bool = typer.Option(True, help="Use PatentsView to fetch kind codes for pubnorms."),
    assignment_source: str = typer.Option(
        "uspto", help="Source for collecting starting patents: 'uspto' or 'lens'"
    ),
    fallback_lens: bool = typer.Option(
        False, help="Fallback to Lens company search if USPTO assignments fail."
    ),
):

    typer.echo(f"Starting pipeline for: {company}")

    # --- aliases ---
    extra: list[str] = []
    if aliases_file:
        with open(aliases_file, "r", encoding="utf-8") as f:
            extra = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    aliases = seed_aliases(company, extra=extra, include_predecessors=include_predecessors)

    preview = aliases[:3]
    more = " ..." if len(aliases) > 3 else ""
    typer.echo(f"Aliases: {len(aliases)} -> {preview}{more}")

    # --- collect starting set of pubnorms ---
    pubnorms: list[str] = []

    def _collect_via_lens(a_list: list[str], limit_val: Optional[int]) -> list[str]:
        typer.echo("Collecting starting set via Lens company search.")
        seen = set()
        out: list[str] = []
        per_alias_limit = limit_val
        for alias in a_list:
            recs = fetch_company_patents(
                company=alias,
                exact=True,
                resolved_npl_only=False,
                use_scroll=True,
                limit=per_alias_limit,
                include_minimal=True,
            )
            for r in recs:
                # Try top-level publication fields first
                pubref = None
                j = (r.get("jurisdiction") or "").upper()
                dn = r.get("doc_number")
                kd = r.get("kind")
                if j and dn:
                    pubref = {"jurisdiction": j, "doc_number": dn, "kind": kd}
                # Else look into biblio.publication_reference
                if not pubref:
                    bibl = r.get("biblio") or {}
                    pubref = bibl.get("publication_reference")
                key = from_lens_publication_reference(pubref) if pubref else None
                # Restrict to US to keep parity with the USPTO-based flow
                if key and key.startswith("us-") and key not in seen:
                    seen.add(key)
                    out.append(key)
        typer.echo(f"Lens returned {len(out)} US pubnorms across aliases.")
        return out

    if assignment_source.lower() == "lens":
        pubnorms = _collect_via_lens(aliases, limit)
    else:
        # Default: USPTO Assignments
        try:
            us_patents, us_apps = collect_numbers_for_parties(aliases)
            typer.echo(
                f"USPTO Assignments returned {len(us_patents)} US patent numbers and {len(us_apps)} application numbers."
            )
            # normalize patent numbers (remove commas/spaces)
            norm_nums = [normalize_doc_number(x).replace("/", "") for x in us_patents]
            norm_nums = [n for n in norm_nums if n]  # drop empties
            if limit is not None and len(norm_nums) > limit:
                norm_nums = norm_nums[:limit]
                typer.echo(f"Limiting to first {limit} patent numbers for downstream steps.")
            # optional enrichment for kind codes
            kind_map: dict[str, str] = {}
            if enrich_kind:
                if not HAS_PATENTSVIEW:
                    typer.echo("[note] patentsview_client not available; skipping kind enrichment.")
                else:
                    typer.echo("Fetching kind codes from PatentsView.")
                    kind_map = fetch_kinds_for_patent_numbers(norm_nums)
                    typer.echo(f"Kind codes resolved for {len(kind_map)} / {len(norm_nums)} patents.")
            # build pubnorms
            for num in norm_nums:
                kind = kind_map.get(num)
                pubnorms.append(build_pubnorm("US", num, kind))
        except Exception as e:
            typer.echo("Error querying USPTO Assignments.")
            typer.echo(str(e))
            if fallback_lens:
                pubnorms = _collect_via_lens(aliases, limit)
            else:
                raise typer.Exit(code=1)

    typer.echo(
        f"USPTO Assignments returned {len(us_patents)} US patent numbers and {len(us_apps)} application numbers."
    )

    if not us_patents and not us_apps:
        typer.echo("No assignment-linked identifiers found for that company/aliases.")
        raise typer.Exit(code=0)

    # normalize patent numbers (remove commas/spaces)
    norm_nums = [normalize_doc_number(x).replace("/", "") for x in us_patents]
    # strictly US here; assignment feed is USPTO. If you later pull EP/WO, adapt jurisdiction logic.
    norm_nums = [n for n in norm_nums if n]  # drop empties

    # Optional: limit (helps with very large owners)
    if limit is not None and len(norm_nums) > limit:
        norm_nums = norm_nums[:limit]
        typer.echo(f"Limiting to first {limit} patent numbers for downstream steps.")

    # --- optional enrichment for kind codes ---
    kind_map: dict[str, str] = {}
    if enrich_kind:
        if not HAS_PATENTSVIEW:
            typer.echo("[note] patentsview_client not available; skipping kind enrichment.")
        else:
            typer.echo("Fetching kind codes from PatentsView.")
            kind_map = fetch_kinds_for_patent_numbers(norm_nums)
            typer.echo(f"Kind codes resolved for {len(kind_map)} / {len(norm_nums)} patents.")

    pubnorms = sorted(set(pubnorms))
    if not pubnorms:
        typer.echo("Could not build any pubnorms from assignment results.")
        raise typer.Exit(code=1)

    typer.echo(f"Normalized to {len(pubnorms)} pubnorms. Starting DuckDB join.")

    # LAZY IMPORT to keep startup light
    from .duck import get_oaids_for_pubnorms, distinct_oaids

    df = get_oaids_for_pubnorms(
        pubnorms=pubnorms,
        confscore_min=confscore,
        wherefound=wherefound,
        reftype=reftype,
        pcs_path=pcs_path,
        pcs_format=pcs_format,
    )
    dfo = distinct_oaids(df)
    # Preserve a raw evidence view for output
    try:
        df_evidence_raw = df[["patent", "oaid", "confscore", "reftype", "wherefound"]].copy()
    except Exception:
        df_evidence_raw = df.copy()

    # --- OpenAlex enrichment (paper metadata) ---
    typer.echo("Enriching OpenAlex metadata (titles, years, abstracts, grants, funders).")
    rows = df.to_dict(orient="records")

    # OAIDs from DuckDB may be ints; normalize to 'W.' strings
    def _norm_oaid(x):
        if x is None:
            return None
        s = str(x)
        return s if s.startswith("W") else f"W{s}"

    oaids = sorted({_norm_oaid(r.get("oaid")) for r in rows if r.get("oaid")})
    oaids = [x for x in oaids if x]

    works_by_id: dict[str, dict] = {}
    if oaids:
        batch_size = int(os.getenv("OPENALEX_MAX_IDS_PER_CALL", "100"))
        num_batches = (len(oaids) + batch_size - 1) // batch_size
        typer.echo(
            f"OpenAlex: fetching {len(oaids)} works in {num_batches} batches (x{batch_size} IDs each)."
        )
        works_by_id = fetch_works_by_ids(oaids)
    else:
        typer.echo("[note] No OAIDs to enrich; writing base evidence only.")

    def join_unique(parts):
        return "; ".join(sorted({p for p in parts if p}))

    for r in rows:
        wid = _norm_oaid(r.get("oaid"))
        w = works_by_id.get(wid or "", {})

        if not w:
            r["paper_title"] = ""
            r["publication_year"] = ""
            r["abstract"] = ""
            r["grants"] = ""
            r["funder"] = ""
            r["institutions"] = ""
            r["award_ids"] = ""
        else:
            r["paper_title"] = (w.get("title") or w.get("display_name") or "").strip()
            r["publication_year"] = w.get("publication_year") or ""
            r["abstract"] = (w.get("abstract") or "").strip()

            # pretty funder names (already flattened in client)
            r["funder"] = join_unique(w.get("_funders_list") or [])

            # grants as "FunderName:AwardID"
            grant_bits = []
            for g in (w.get("grants") or []):
                fname = g.get("funder_display_name") or g.get("funder") or ""
                award = g.get("award_id") or ""
                if fname and award:
                    grant_bits.append(f"{fname}:{award}")
                elif fname or award:
                    grant_bits.append(fname or award)
            r["grants"] = join_unique(grant_bits)
            r["institutions"] = join_unique(w.get("_institutions_list") or [])
            r["award_ids"] = join_unique(w.get("_grants_award_ids") or [])

            # Concepts: top N by score
            concept_names = []
            for c in (w.get("concepts") or []):
                name = c.get("display_name")
                if name:
                    concept_names.append((c.get("score") or 0.0, name))
            concept_names = [name for _, name in sorted(concept_names, reverse=True)]
            r["concepts"] = "; ".join(concept_names[:10])

            # Topics
            topic_names = []
            for t in (w.get("topics") or []):
                name = t.get("display_name")
                if name:
                    topic_names.append((t.get("score") or 0.0, name))
            topic_names = [name for _, name in sorted(topic_names, reverse=True)]
            r["topics"] = "; ".join(topic_names)

    # prune columns you don't want in the primary enriched sheet
    for r in rows:
        for k in ("oaid", "confscore", "reftype", "wherefound"):
            r.pop(k, None)

    final_cols = [
        "patent",
        "paper_title",
        "publication_year",
        "abstract",
        "grants",
        "award_ids",
        "institutions",
        "topics",
    ]
    final_rows = [{k: r.get(k, "") for k in final_cols} for r in rows]
    df_final = pd.DataFrame(final_rows, columns=final_cols)

    # Save outputs
    out = outdir()
    company_slug = company.lower().replace(" ", "_")
    evidence_xlsx = out / f"{company_slug}_evidence.xlsx"
    oaids_path = out / f"{company_slug}_oaids.csv"

    # Optional: blocker for empties (helps in xlsx to stop visual spill)
    BLOCKER = "\u2060"
    for col in ["grants", "award_ids", "institutions", "funder"]:
        if col in df_final.columns:
            df_final[col] = df_final[col].replace({"": BLOCKER})

    with pd.ExcelWriter(evidence_xlsx, engine="xlsxwriter") as writer:
        # Enriched evidence sheet
        df_final.to_excel(writer, index=False, sheet_name="evidence")
        ws = writer.sheets["evidence"]
        fmt = writer.book.add_format({"text_wrap": False})
        ws.set_column(0, len(df_final.columns) - 1, None, fmt)

        # Raw evidence sheet
        df_evidence_raw.to_excel(writer, index=False, sheet_name="evidence_raw")
        ws2 = writer.sheets["evidence_raw"]
        ws2.set_column(0, len(df_evidence_raw.columns) - 1, None, fmt)

    # OAIDs CSV is fine in plain CSV
    dfo.to_csv(oaids_path, index=False)

    typer.echo(f"Wrote evidence: {evidence_xlsx}")
    typer.echo(f"Wrote OAIDs:    {oaids_path}")


if __name__ == "__main__":
    app()
