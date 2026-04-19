# patent-science-linker

An open-data pipeline that links **patents → cited scientific publications → OpenAlex works (OAIDs)** so you can measure and trace how science shows up in downstream invention.

At a high level, this repo:
- builds an initial set of patents associated with a company / assignee using one of several sources:
  - **PatentsView (local dump)** (default, free + reliable)
  - **USPTO Assignments API** (free, but can be brittle depending on name/assignment coverage)
  - **Lens** (paid/credentialed option; robust search UX)
- joins that patent set to **patent→paper citation evidence** (Reliance-on-Science / PCS-style records)
- uses **DuckDB** to join/filter evidence at scale
- resolves cited publications to **OpenAlex Work IDs (OAIDs)** and enriches with metadata (title/year/abstract/institutions/topics, etc.)

This is designed for reproducible, query-driven analyses (e.g., “what scientific work is being cited by patents associated with Company X?”) and for generating evidence tables suitable for downstream portfolio evaluation.

## Data source options (Lens vs PatentsView vs local PatentsView)

You can choose how the pipeline builds the *starting set of patents* for a company:

- **`assignment_source=local` (recommended default)**
  - Uses a **local PatentsView data dump** via `patentsview_local`.
  - This is the **free** option and avoids external service flakiness.
  - It exists partly as a **fail-safe** because the hosted PatentsView API/client can be unavailable at times (server-side outages).

- **`assignment_source=uspto`**
  - Uses the **USPTO Assignments** service to collect patent numbers for assignee/owner name variants.
  - Free, but can fail or under-retrieve depending on naming and assignment records.
  - You can set `--fallback-lens` to fall back to Lens if this path fails.

- **`assignment_source=lens`**
  - Uses the **Lens** API to search for patents by company name (exact-match variants).
  - Useful if you already have Lens access and want a strong first-pass retrieval.

Notes:
- In some older versions you may see `assignment_source=patentsview`; this is deprecated in favor of `local`.


## Quickstart

### 1) Setup
Create and activate a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2) Configure environment variables
Copy `.env.example` → `.env` and fill in values:

```bash
cp .env.example .env
```

**Do not commit `.env`.** It is intentionally ignored by git.

### 3) (Optional) Convert PCS evidence to Parquet
If you have a `pcs_oa.csv`, converting it to Parquet can make repeated runs faster. pcs_oa.csv is a file that was constructed by the Reliance on Science team: https://relianceonscience.org/about-us, and contains raw patent-paper pairs. Pcs_oa.csv can be downloaded here: https://relianceonscience.org/patent-to-paper-citations.  

```bash
python -c "import duckdb; duckdb.sql(\"COPY (SELECT * FROM read_csv_auto('pcs_oa.csv', HEADER=TRUE)) TO 'pcs_oa.parquet' (FORMAT PARQUET);\")"
```

### 4) Run the CLI
Example run:

```bash
python -m src.pipeline.main --company "Company X" --confscore 8 --limit 5000
```

## Execution parameters (what changes pipeline behavior)

The CLI exposes a few parameters that materially change behavior:

### Query mode
- `--company "…"`
  - Primary mode: run the pipeline for a single company name.
  - The pipeline automatically generates **exact-match variants** (aliases) for the company.

- `--from-query path/to/companies.txt`
  - Batch mode: a plain text file with **one company per line**.

- `--from-university <OpenAlex institution id/name>`
  - University-driven mode: collects OpenAlex works associated with an institution and then traces patent citation uptake.

### Evidence filters
- `--confscore <int>`
  - Minimum citation confidence score to accept a patent→paper match.
  - Higher = stricter (fewer links, typically cleaner); lower = more recall.

- `--wherefound both --wherefound bodyonly --wherefound frontonly`
  - Filters citations by where they appear in the patent document (front page vs body vs both).

- `--reftype …`
  - Filters by reference type (front, body, both).

### Aliases / name handling
- `--aliases-file path/to/aliases.txt`
  - Extra aliases (one per line). Lines starting with `#` are ignored.

- `--include-predecessors`
  - Include known predecessor names (mergers/legacy) when generating alias variants.

### Patent collection source
- `--assignment-source local|uspto|lens`
  - Chooses the starting patent set source (see “Data source options” above).

- `--local-match exact|contains`
  - For `assignment_source=local`: how to match company names inside the local PatentsView data.
  - `exact` is safer/cleaner; `contains` increases recall but can introduce false matches.

- `--fallback-lens`
  - If `assignment_source=uspto` or `local` fails to return patents, fall back to Lens company search.

### PCS evidence input
- `--pcs-path /path/to/pcs_oa.(csv|parquet)`
  - Override PCS evidence location (otherwise uses env `PCS_PATH`).

- `--pcs-format csv|parquet`
  - Override PCS evidence format (otherwise uses env `PCS_FORMAT`).

### Misc
- `--limit <int>`
  - Caps the overall patent records used downstream (post de-dup). Helpful for testing.

- `--enrich-kind/--no-enrich-kind`
  - If enabled, uses PatentsView to fetch kind codes (when available) to improve normalization/joining.

## Outputs

By default, outputs are written under `data/outputs/` (which is ignored by git):

- `<company>_oaids.csv`
  - distinct OpenAlex Work IDs (OAIDs) linked via patent citation evidence

- `<company>_evidence.xlsx`
  - `evidence`: enriched evidence table (e.g., patent, paper_title, publication_year, abstract, grants, award_ids, institutions, topics)
  - `evidence_raw`: raw PCS evidence (e.g., patent, oaid, confscore, reftype, wherefound)

## Notes / implementation details

- Patent identifiers are expected in a normalized form like `us-11426570-b2` or `wo-2022020260-a1`.
- The Lens client supports both offset pagination and scroll/cursor pagination for large result sets.
- `--resolved-only` biases toward patents with resolved NPL (more likely to map cleanly to OAIDs).

## Common pitfalls

- If you see missing results, check that your company/assignee string matches how it appears in Lens.
- If outputs aren’t appearing, confirm you have required env vars set and that `data/outputs/` exists (the pipeline will create it in most cases).

## License

Add a LICENSE before making the repository public (e.g., MIT or Apache-2.0), depending on how you want others to reuse the code.

