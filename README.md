# patent-science-linker

Pipeline: Lens + DuckDB + OpenAlex to map Company + Patents + OAIDs (OpenAlex Work IDs).

## Quickstart
1. Create and activate a venv, then install deps:
   ```bash
   python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and set values.
3. Convert your `pcs_oa.csv` to Parquet once (optional but recommended):
   ```bash
   python -c "import duckdb; duckdb.sql(\"COPY (SELECT * FROM read_csv_auto('pcs_oa.csv', HEADER=TRUE)) TO 'pcs_oa.parquet' (FORMAT PARQUET);\")"
   ```
4. Run the CLI:
   ```bash
   python -m src.pipeline.main --company "Lockheed Martin" --confscore 8 --limit 5000
   ```

Outputs are written under `data/outputs/`:
- `<company>_oaids.csv` (distinct OAIDs)
- `<company>_evidence.xlsx` with two sheets:
  - `evidence` (enriched: patent, paper_title, publication_year, abstract, grants, award_ids, institutions, topics)
  - `evidence_raw` (raw PCS evidence: patent, oaid, confscore, reftype, wherefound)

## Notes
- The DuckDB query assumes your `patent` field matches normalized form like `us-11426570-b2` or `wo-2022020260-a1`.
- The Lens client supports offset and scroll (cursor) pagination for large sets.
- Use `--resolved-only` to bias toward patents with resolved NPL (more likely to map to OAIDs).

