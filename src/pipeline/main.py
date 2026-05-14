# src/pipeline/main.py
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import os
import re
import pandas as pd
import typer
from dotenv import load_dotenv

from .company_query import seed_aliases
from .normalizer import build_pubnorm, normalize_doc_number, from_lens_publication_reference
from .uspto_assignment_client import collect_numbers_for_parties_ac as collect_numbers_for_parties
from .openalex_client import fetch_works_by_ids, fetch_institution_oaids
from .lens_client import fetch_company_patents

# optional enrichment
try:
    from .patentsview_client import fetch_kinds_for_patent_numbers
    HAS_PATENTSVIEW = True
except Exception:
    HAS_PATENTSVIEW = False

from .patentsview_local import collect_pubnorms_from_local_patentsview, fetch_metadata_for_patent_ids
from .topic_filter import apply_topic_filter, load_topic_profile


app = typer.Typer(add_completion=False)
load_dotenv()


def outdir() -> Path:
    d = Path(__file__).resolve().parents[2] / "data" / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_sheet_name(name: str) -> str:
    # Remove forbidden chars and trim to 31
    cleaned = re.sub(r"[:\\/*?\[\]]", " ", name).strip()
    if len(cleaned) > 31:
        cleaned = cleaned[:31].rstrip()
    if not cleaned:
        cleaned = "Sheet"
    return cleaned


def _slugify(value: str) -> str:
    """
    Build a filesystem-friendly slug (lowercase, underscores).
    """
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "output"


def _normalize_oaid_str(x: Optional[str]) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    return s if s.upper().startswith("W") else f"W{s}"


def _pubnorm_to_patent_id(patent: Optional[str]) -> Optional[str]:
    if not patent:
        return None
    s = str(patent).strip().lower()
    if not s.startswith("us-"):
        return None
    bits = s.split("-")
    if len(bits) < 2:
        return None
    body = bits[1]
    body = body.split("/")[0]
    digits = re.sub(r"[^0-9]", "", body)
    return digits or None


def _build_evidence_for_company(
    company: str,
    *,
    confscore: int,
    aliases_file: Optional[str],
    include_predecessors: bool,
    limit: Optional[int],
    pcs_path: Optional[str],
    pcs_format: Optional[str],
    wherefound: Optional[List[str]],
    reftype: Optional[List[str]],
    enrich_kind: bool,
    assignment_source: str,
    fallback_lens: bool,
    local_match: str,
    topic_profile: Optional[str] = None,
):
    typer.echo(f"Starting pipeline for: {company}")

    # aliases
    extra: list[str] = []
    if aliases_file:
        with open(aliases_file, "r", encoding="utf-8") as f:
            extra = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    aliases = seed_aliases(company, extra=extra, include_predecessors=include_predecessors)
    preview = aliases[:3]
    more = " ..." if len(aliases) > 3 else ""
    typer.echo(f"Aliases: {len(aliases)} -> {preview}{more}")

    # collect starting pubnorms
    pubnorms: list[str] = []
    wipo_metadata: dict[str, dict[str, str]] = {}
    audit_rows: list[dict] = []

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
                pubref = None
                j = (r.get("jurisdiction") or "").upper()
                dn = r.get("doc_number")
                kd = r.get("kind")
                if j and dn:
                    pubref = {"jurisdiction": j, "doc_number": dn, "kind": kd}
                if not pubref:
                    bibl = r.get("biblio") or {}
                    pubref = bibl.get("publication_reference")
                key = from_lens_publication_reference(pubref) if pubref else None
                if key and key.startswith("us-") and key not in seen:
                    seen.add(key)
                    out.append(key)
        typer.echo(f"Lens returned {len(out)} US pubnorms across aliases.")
        return out

    local_match_mode = (local_match or "exact").lower()
    if local_match_mode not in {"exact", "contains"}:
        raise typer.BadParameter("local_match must be 'exact' or 'contains'.")

    source = assignment_source.lower()
    if source == "lens":
        pubnorms = _collect_via_lens(aliases, limit)
    elif source in {"local", "patentsview"}:
        if source == "patentsview":
            typer.echo("[note] assignment_source 'patentsview' is deprecated; use 'local'.")
        try:
            typer.echo("Collecting starting set via local PatentsView data.")
            pv_dir_override = os.getenv("PATENTSVIEW_DATA_DIR")
            pv_result = collect_pubnorms_from_local_patentsview(
                aliases, limit=limit, data_dir=pv_dir_override, match=local_match_mode
            )
            pubnorms = pv_result.pubnorms
            wipo_metadata = pv_result.metadata_by_pubnorm
            typer.echo(
                f"PatentsView matched {pv_result.matched_patent_count} unique patents "
                f"-> {len(pubnorms)} pubnorm candidates."
            )
            if pv_result.missing_kind_count:
                typer.echo(
                    f"[note] {pv_result.missing_kind_count} patents missing wipo_kind; "
                    "falling back to base pubnorm for those entries."
                )
            if not pubnorms and fallback_lens:
                typer.echo("No PatentsView matches. Falling back to Lens company search.")
                pubnorms = _collect_via_lens(aliases, limit)
                wipo_metadata = {}
        except Exception as exc:
            typer.echo("Error querying local PatentsView data.")
            typer.echo(str(exc))
            if fallback_lens:
                typer.echo("Falling back to Lens company search.")
                pubnorms = _collect_via_lens(aliases, limit)
                wipo_metadata = {}
            else:
                raise
    else:
        try:
            us_patents, us_apps = collect_numbers_for_parties(aliases)
            typer.echo(
                f"USPTO Assignments returned {len(us_patents)} US patent numbers and {len(us_apps)} application numbers."
            )
            norm_nums = [normalize_doc_number(x).replace("/", "") for x in us_patents]
            norm_nums = [n for n in norm_nums if n]
            if limit is not None and len(norm_nums) > limit:
                norm_nums = norm_nums[:limit]
                typer.echo(f"Limiting to first {limit} patent numbers for downstream steps.")
            kind_map: dict[str, str] = {}
            if enrich_kind:
                if not HAS_PATENTSVIEW:
                    typer.echo("[note] patentsview_client not available; skipping kind enrichment.")
                else:
                    typer.echo("Fetching kind codes from PatentsView.")
                    kind_map = fetch_kinds_for_patent_numbers(norm_nums)
                    typer.echo(f"Kind codes resolved for {len(kind_map)} / {len(norm_nums)} patents.")
            for num in norm_nums:
                kind = kind_map.get(num)
                # include both kinded and unkinded variants to maximize join hits
                pubnorms.append(build_pubnorm("US", num, kind))
                pubnorms.append(build_pubnorm("US", num, None))
        except Exception as e:
            typer.echo("Error querying USPTO Assignments.")
            typer.echo(str(e))
            if fallback_lens:
                pubnorms = _collect_via_lens(aliases, limit)
            else:
                raise

    # De-duplicate and also include unkinded variants for any kinded keys from any source
    base_set = set(pubnorms)
    for key in list(base_set):
        parts = key.split("-")
        if len(parts) >= 3:
            base_set.add(f"{parts[0]}-{parts[1]}")
    pubnorms = sorted(base_set)

    if topic_profile:
        # Ensure topic gating has metadata even when assignment source is USPTO/Lens.
        # Local source already returns metadata_by_pubnorm, but other sources need hydration
        # from local PatentsView files using patent IDs extracted from pubnorms.
        patent_ids_for_topic: list[str] = []
        pubnorm_to_patent_id: dict[str, str] = {}
        for pat in pubnorms:
            pid = _pubnorm_to_patent_id(pat)
            if not pid:
                continue
            patent_ids_for_topic.append(pid)
            pubnorm_to_patent_id[str(pat).lower()] = pid

        if patent_ids_for_topic:
            try:
                pv_dir_override = os.getenv("PATENTSVIEW_DATA_DIR")
                pv_metadata = fetch_metadata_for_patent_ids(
                    list(dict.fromkeys(patent_ids_for_topic)),
                    data_dir=pv_dir_override,
                )
                for key, pid in pubnorm_to_patent_id.items():
                    meta = pv_metadata.get(pid)
                    if not meta:
                        continue
                    merged = dict(wipo_metadata.get(key, {}))
                    merged.update(meta)
                    wipo_metadata[key] = merged

                    # Also backfill base key (e.g., us-1234567) so kinded/unkinded lookups both work.
                    parts = key.split("-")
                    if len(parts) >= 2:
                        base_key = "-".join(parts[:2])
                        base_merged = dict(wipo_metadata.get(base_key, {}))
                        base_merged.update(meta)
                        wipo_metadata[base_key] = base_merged
            except Exception as exc:
                typer.echo(f"[warn] Topic metadata hydration unavailable: {exc}")

        profile = load_topic_profile(topic_profile)
        filtered_pubnorms, audit_rows = apply_topic_filter(pubnorms, wipo_metadata, profile)
        typer.echo(
            f"Topic profile '{profile['name']}' kept {len(filtered_pubnorms)} / {len(pubnorms)} patents before PCS join."
        )
        pubnorms = filtered_pubnorms

    if not pubnorms:
        typer.echo("Could not build any pubnorms from assignment results.")
        audit_df = pd.DataFrame(audit_rows) if audit_rows else pd.DataFrame()
        return pd.DataFrame(), pd.DataFrame(), audit_df

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
    typer.echo(f"Evidence rows: {len(df)}; distinct OAIDs: {len(dfo)}")

    # OpenAlex enrichment (paper metadata)
    typer.echo("Enriching OpenAlex metadata (titles, years, abstracts, grants, funders).")
    rows = df.to_dict(orient="records")
    # If no evidence rows, still emit one row per pubnorm with only patent populated
    if not rows:
        rows = [{"patent": p} for p in pubnorms]

    def _wipo_for_patent(pat: Optional[str]) -> dict[str, str]:
        if not wipo_metadata or not pat:
            return {}
        key = str(pat).lower()
        meta = wipo_metadata.get(key)
        if meta:
            return meta
        bits = key.split("-")
        if len(bits) >= 2:
            base_key = "-".join(bits[:2])
            meta = wipo_metadata.get(base_key)
            if meta:
                return meta
        return {}

    for r in rows:
        meta = _wipo_for_patent(r.get("patent"))
        r["wipo_kind"] = meta.get("wipo_kind", "")
        r["wipo_sector_title"] = meta.get("wipo_sector_title", "")
        r["wipo_field_title"] = meta.get("wipo_field_title", "")

    oaids = sorted({_normalize_oaid_str(r.get("oaid")) for r in rows if r.get("oaid")})
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
        wid = _normalize_oaid_str(r.get("oaid"))
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
            r["funder"] = join_unique(w.get("_funders_list") or [])

            grant_bits = []
            for g in ((w.get("awards") or []) + (w.get("grants") or [])):
                fname = g.get("funder_display_name") or g.get("funder") or g.get("funder_name") or ""
                award = g.get("award_id") or g.get("id") or ""
                if fname and award:
                    grant_bits.append(f"{fname}:{award}")
                elif fname or award:
                    grant_bits.append(str(fname or award))
            r["grants"] = join_unique(grant_bits)
            r["institutions"] = join_unique(w.get("_institutions_list") or [])
            r["award_ids"] = join_unique(w.get("_grants_award_ids") or [])

            concept_names = []
            for c in (w.get("concepts") or []):
                name = c.get("display_name")
                if name:
                    concept_names.append((c.get("score") or 0.0, name))
            concept_names = [name for _, name in sorted(concept_names, reverse=True)]
            r["concepts"] = "; ".join(concept_names[:10])

            topic_names = []
            for t in (w.get("topics") or []):
                name = t.get("display_name")
                if name:
                    topic_names.append((t.get("score") or 0.0, name))
            topic_names = [name for _, name in sorted(topic_names, reverse=True)]
            r["topics"] = "; ".join(topic_names)

    # prune to enriched sheet
    for r in rows:
        for k in ("oaid", "confscore", "reftype", "wherefound"):
            r.pop(k, None)

    final_cols = [
        "patent",
        "wipo_kind",
        "wipo_sector_title",
        "wipo_field_title",
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
    audit_df = pd.DataFrame(audit_rows) if audit_rows else pd.DataFrame()

    return df_final, dfo, audit_df


def _build_university_patent_view(
    university: str,
    *,
    confscore: int,
    limit: Optional[int],
    pcs_path: Optional[str],
    pcs_format: Optional[str],
    wherefound: Optional[List[str]],
    reftype: Optional[List[str]],
) -> pd.DataFrame:
    typer.echo(f"Starting university pipeline for: {university}")
    max_works = limit if limit is not None else None
    oaids = fetch_institution_oaids(university, max_works=max_works)
    if not oaids:
        typer.echo("No OpenAlex works returned for this institution.")
        return pd.DataFrame()
    typer.echo(f"Collected {len(oaids)} works for institution (limit={max_works or 'env default'}). Checking PCS joins.")

    # LAZY IMPORT to keep startup light
    from .duck import get_patents_for_oaids

    df = get_patents_for_oaids(
        oaids=oaids,
        confscore_min=confscore,
        wherefound=wherefound,
        reftype=reftype,
        pcs_path=pcs_path,
        pcs_format=pcs_format,
    )

    if df.empty:
        typer.echo("No patents referencing those works were found in PCS.")
        return pd.DataFrame()

    target_oaids = sorted({_normalize_oaid_str(x) for x in df["oaid"].tolist() if x})
    typer.echo(f"Matched {len(target_oaids)} OAIDs in PCS. Enriching metadata from OpenAlex.")
    works = fetch_works_by_ids(target_oaids) if target_oaids else {}
    typer.echo("Enriching PatentsView metadata for assignees + WIPO fields.")

    patent_key_to_id: dict[str, str] = {}
    patent_ids: list[str] = []
    for pat in df["patent"].tolist():
        key = str(pat).strip().lower()
        pid = _pubnorm_to_patent_id(pat)
        if not pid:
            continue
        if key and key not in patent_key_to_id:
            patent_key_to_id[key] = pid
        base_bits = key.split("-")
        if len(base_bits) >= 2:
            base_key = "-".join(base_bits[:2])
            if base_key not in patent_key_to_id:
                patent_key_to_id[base_key] = pid
        patent_ids.append(pid)

    pv_metadata: dict[str, dict[str, str]] = {}
    unique_patent_ids = list(dict.fromkeys(patent_ids))
    if unique_patent_ids:
        try:
            pv_metadata = fetch_metadata_for_patent_ids(unique_patent_ids)
        except Exception as exc:
            typer.echo(f"[warn] PatentsView metadata unavailable: {exc}")
            pv_metadata = {}

    rows = []
    def join_unique(parts):
        return "; ".join(sorted({p for p in parts if p}))

    for record in df.to_dict(orient="records"):
        wid = _normalize_oaid_str(record.get("oaid"))
        w = works.get(wid or "", {})

        patent_key = str(record.get("patent") or "").strip().lower()
        base_key = "-".join(patent_key.split("-")[:2]) if patent_key else ""
        patent_id = patent_key_to_id.get(patent_key) or patent_key_to_id.get(base_key or "")
        meta = pv_metadata.get(patent_id or "", {})

        grant_bits = []
        for g in ((w.get("awards") or []) + (w.get("grants") or [])):
            fname = g.get("funder_display_name") or g.get("funder") or g.get("funder_name") or ""
            award = g.get("award_id") or g.get("id") or ""
            if fname and award:
                grant_bits.append(f"{fname}:{award}")
            elif fname or award:
                grant_bits.append(str(fname or award))

        rows.append(
            {
                "university": university,
                "oaid": wid or "",
                "paper_title": (w.get("title") or w.get("display_name") or "").strip(),
                "publication_year": w.get("publication_year") or "",
                "grants": join_unique(grant_bits),
                "award_ids": join_unique(w.get("_grants_award_ids") or []),
                "institutions": "; ".join(w.get("_institutions_list") or []),
                "topics": "; ".join(w.get("_topics_list") or []),
                "patent": record.get("patent") or "",
                "wipo_kind": meta.get("wipo_kind", ""),
                "wipo_sector_title": meta.get("wipo_sector_title", ""),
                "wipo_field_title": meta.get("wipo_field_title", ""),
                "wherefound": record.get("wherefound") or "",
                "reftype": record.get("reftype") or "",
                "confscore": record.get("confscore") or "",
                "assignees": meta.get("assignees", ""),
            }
        )

    columns = [
        "university",
        "oaid",
        "paper_title",
        "publication_year",
        "grants",
        "award_ids",
        "institutions",
        "topics",
        "patent",
        "wipo_kind",
        "wipo_sector_title",
        "wipo_field_title",
        "wherefound",
        "reftype",
        "confscore",
        "assignees",
    ]
    return pd.DataFrame(rows, columns=columns)


@app.command()
def run(
    company: Optional[str] = typer.Option(None, help="Company/assignee/owner name (exact-match variants generated)."),
    from_query: Optional[str] = typer.Option(None, help="Path to .txt file with one company per line."),
    from_university: Optional[str] = typer.Option(
        None, help="OpenAlex institution id/name. Enables university-driven mode."
    ),
    confscore: int = typer.Option(8, help="Minimum confscore to accept a citation match."),
    aliases_file: Optional[str] = typer.Option(None, help="Extra aliases (one per line; '#' comments allowed)."),
    include_predecessors: bool = typer.Option(False, help="Include known predecessor names (mergers/legacy)."),
    limit: Optional[int] = typer.Option(None, help="Overall max patent records (post de-dup)."),
    pcs_path: Optional[str] = typer.Option(None, help="Override PCS path (else env PCS_PATH)."),
    pcs_format: Optional[str] = typer.Option(None, help="parquet or csv (else env PCS_FORMAT)."),
    wherefound: Optional[List[str]] = typer.Option(None, help="Filter citations by wherefound (repeatable)."),
    reftype: Optional[List[str]] = typer.Option(None, help="Filter citations by reftype (repeatable)."),
    enrich_kind: bool = typer.Option(True, help="Use PatentsView to fetch kind codes for pubnorms."),
    assignment_source: str = typer.Option(
        "local", help="Source for collecting starting patents: 'local', 'uspto', or 'lens'"
    ),
    fallback_lens: bool = typer.Option(
        False, help="Fallback to Lens company search if USPTO assignments fail."
    ),
    local_match: str = typer.Option(
        "exact", help="Local PatentsView name match mode: 'exact' or 'contains'."
    ),
    topic_profile: Optional[str] = typer.Option(
        None, help="Path to topic profile JSON for rule-based filtering before PCS join."
    ),
    topic_audit: bool = typer.Option(
        True, "--topic-audit/--no-topic-audit", help="Write topic audit sheet when topic profile is provided."
    ),
):
    out = outdir()

    if from_university:
        if company or from_query:
            raise typer.BadParameter("--from-university cannot be combined with --company/--from_query.")
        df_university = _build_university_patent_view(
            from_university,
            confscore=confscore,
            limit=limit,
            pcs_path=pcs_path,
            pcs_format=pcs_format,
            wherefound=wherefound,
            reftype=reftype,
        )
        if df_university.empty:
            typer.echo("No results found for this institution.")
            return
        uni_slug = _slugify(from_university)
        csv_path = out / f"{uni_slug}_university_patents.csv"
        df_university.to_csv(csv_path, index=False)
        typer.echo(f"Wrote university report: {csv_path}")
        return

    # Build company list: include --company plus all names from file (if provided)
    companies: list[str] = []
    if company:
        companies.append(company.strip())
    if from_query:
        p = Path(from_query)
        if not p.exists():
            raise typer.BadParameter(f"Query file not found: {from_query}")
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name:
                    companies.append(name)
    # de-duplicate while preserving order
    seen: set[str] = set()
    companies = [c for c in companies if not (c in seen or seen.add(c))]

    if not companies:
        raise typer.BadParameter("Provide --company and/or --from_query with at least one name.")

    # If a query file is used, write a single combined workbook named after the file stem
    if from_query:
        stem = Path(from_query).stem
        combined_xlsx = out / f"{stem}_evidence.xlsx"
        typer.echo(f"Batch mode: {len(companies)} companies -> {combined_xlsx}")
        used_sheet_names: set[str] = set()

        with pd.ExcelWriter(combined_xlsx, engine="xlsxwriter") as writer:
            fmt = writer.book.add_format({"text_wrap": False})
            for comp in companies:
                df_final, _, audit_df = _build_evidence_for_company(
                    comp,
                    confscore=confscore,
                    aliases_file=aliases_file,
                    include_predecessors=include_predecessors,
                    limit=limit,
                    pcs_path=pcs_path,
                    pcs_format=pcs_format,
                    wherefound=wherefound,
                    reftype=reftype,
                    enrich_kind=enrich_kind,
                    assignment_source=assignment_source,
                    fallback_lens=fallback_lens,
                    local_match=local_match,
                    topic_profile=topic_profile,
                )

                sheet = _sanitize_sheet_name(comp)
                base = sheet
                idx = 2
                while sheet in used_sheet_names:
                    suffix = f"-{idx}"
                    sheet = (base[: 31 - len(suffix)] + suffix)[:31]
                    idx += 1
                used_sheet_names.add(sheet)

                # Write only enriched evidence per company
                df_final.to_excel(writer, index=False, sheet_name=sheet)
                ws = writer.sheets[sheet]
                ws.set_column(0, max(0, len(df_final.columns) - 1), None, fmt)

                if topic_profile and topic_audit and not audit_df.empty:
                    audit_df = audit_df.copy()
                    audit_df.insert(0, "company", comp)
                    audit_sheet = _sanitize_sheet_name(f"{comp} audit")
                    base_audit = audit_sheet
                    idx_a = 2
                    while audit_sheet in used_sheet_names:
                        suffix = f"-{idx_a}"
                        audit_sheet = (base_audit[: 31 - len(suffix)] + suffix)[:31]
                        idx_a += 1
                    used_sheet_names.add(audit_sheet)
                    audit_df.to_excel(writer, index=False, sheet_name=audit_sheet)
                    ws_a = writer.sheets[audit_sheet]
                    ws_a.set_column(0, max(0, len(audit_df.columns) - 1), None, fmt)

        typer.echo(f"Wrote evidence: {combined_xlsx}")
        return

    # Single-company mode
    comp = companies[0]
    df_final, _, audit_df = _build_evidence_for_company(
        comp,
        confscore=confscore,
        aliases_file=aliases_file,
        include_predecessors=include_predecessors,
        limit=limit,
        pcs_path=pcs_path,
        pcs_format=pcs_format,
        wherefound=wherefound,
        reftype=reftype,
        enrich_kind=enrich_kind,
        assignment_source=assignment_source,
        fallback_lens=fallback_lens,
        local_match=local_match,
        topic_profile=topic_profile,
    )

    company_slug = _slugify(comp)
    evidence_xlsx = out / f"{company_slug}_evidence.xlsx"

    with pd.ExcelWriter(evidence_xlsx, engine="xlsxwriter") as writer:
        df_final.to_excel(writer, index=False, sheet_name="evidence")
        ws = writer.sheets["evidence"]
        fmt = writer.book.add_format({"text_wrap": False})
        ws.set_column(0, max(0, len(df_final.columns) - 1), None, fmt)

        if topic_profile and topic_audit and not audit_df.empty:
            audit_df.to_excel(writer, index=False, sheet_name="topic_audit")
            ws_a = writer.sheets["topic_audit"]
            ws_a.set_column(0, max(0, len(audit_df.columns) - 1), None, fmt)

    typer.echo(f"Wrote evidence: {evidence_xlsx}")


if __name__ == "__main__":
    app()
