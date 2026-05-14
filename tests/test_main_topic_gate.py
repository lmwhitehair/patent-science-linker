import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.pipeline import main


@dataclass(frozen=True)
class _PVResult:
    pubnorms: list[str]
    metadata_by_pubnorm: dict[str, dict[str, str]]
    matched_patent_count: int
    missing_kind_count: int


def test_build_evidence_applies_topic_profile_before_duck_join(monkeypatch, tmp_path: Path):
    profile_path = tmp_path / "quantum_strict.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "quantum_strict",
                "include_terms_title": ["qubit"],
                "include_terms_abstract": [],
                "exclude_terms_title": ["quantum dot"],
                "exclude_terms_abstract": [],
                "cpc_include_prefixes": ["G06N10"],
            }
        ),
        encoding="utf-8",
    )

    pv_result = _PVResult(
        pubnorms=["us-1-b2", "us-2-b2"],
        metadata_by_pubnorm={
            "us-1-b2": {
                "patent_id": "1",
                "patent_title": "Qubit processor",
                "patent_abstract": "",
                "cpc_groups": "G06N10/40",
                "wipo_kind": "B2",
                "wipo_sector_title": "Electrical engineering",
                "wipo_field_title": "Computer technology",
            },
            "us-2-b2": {
                "patent_id": "2",
                "patent_title": "Quantum dot panel",
                "patent_abstract": "",
                "cpc_groups": "H01L",
                "wipo_kind": "B2",
                "wipo_sector_title": "Electrical engineering",
                "wipo_field_title": "Semiconductors",
            },
        },
        matched_patent_count=2,
        missing_kind_count=0,
    )

    monkeypatch.setattr(main, "seed_aliases", lambda *a, **k: ["IBM"])
    monkeypatch.setattr(main, "collect_pubnorms_from_local_patentsview", lambda *a, **k: pv_result)

    captured = {}

    def fake_get_oaids_for_pubnorms(*, pubnorms, **kwargs):
        captured["pubnorms"] = list(pubnorms)
        return pd.DataFrame(
            [
                {
                    "patent": "us-1-b2",
                    "oaid": "W123",
                    "confscore": 10,
                    "reftype": "app",
                    "wherefound": "frontonly",
                }
            ]
        )

    monkeypatch.setattr("src.pipeline.duck.get_oaids_for_pubnorms", fake_get_oaids_for_pubnorms)
    monkeypatch.setattr("src.pipeline.duck.distinct_oaids", lambda df: pd.DataFrame([{"oaid": "W123"}]))
    monkeypatch.setattr(main, "fetch_works_by_ids", lambda oaids: {"W123": {"title": "Paper", "publication_year": 2020}})

    df_final, _dfo, audit_df = main._build_evidence_for_company(
        "IBM",
        confscore=8,
        aliases_file=None,
        include_predecessors=False,
        limit=None,
        pcs_path="dummy.parquet",
        pcs_format="parquet",
        wherefound=None,
        reftype=None,
        enrich_kind=False,
        assignment_source="local",
        fallback_lens=False,
        local_match="exact",
        topic_profile=str(profile_path),
    )

    assert captured["pubnorms"] == ["us-1-b2"]
    assert not df_final.empty
    assert audit_df is not None
    assert set(audit_df.columns) >= {"patent", "topic_pass", "rule_profile"}
