import json
from pathlib import Path

from src.pipeline.topic_filter import (
    apply_topic_filter,
    load_topic_profile,
    match_topic,
)


def test_load_topic_profile_requires_name(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"include_terms_title": ["quantum"]}), encoding="utf-8")

    try:
        load_topic_profile(str(p))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "name" in str(exc).lower()


def test_match_topic_include_and_exclude_rules():
    profile = {
        "name": "quantum_strict",
        "include_terms_title": ["quantum computing", "qubit"],
        "include_terms_abstract": ["qubit", "quantum algorithm"],
        "exclude_terms_title": ["quantum dot"],
        "exclude_terms_abstract": ["quantum well"],
        "cpc_include_prefixes": ["G06N10"],
    }
    metadata = {
        "patent_title": "Quantum dot structure for display",
        "patent_abstract": "Uses a qubit register",
        "cpc_groups": "H01L; G06N10/40",
    }

    result = match_topic(metadata, profile)
    assert result["topic_pass"] is False
    assert "qubit" in result["matched_include_terms"].lower()
    assert "quantum dot" in result["matched_exclude_terms"].lower()
    assert "G06N10" in result["matched_cpc_prefixes"]


def test_match_topic_cpc_rescue_when_text_missing():
    profile = {
        "name": "quantum_strict",
        "include_terms_title": ["qubit"],
        "include_terms_abstract": ["quantum computing"],
        "exclude_terms_title": [],
        "exclude_terms_abstract": [],
        "cpc_include_prefixes": ["G06N10"],
    }
    metadata = {
        "patent_title": "Signal processor",
        "patent_abstract": "General computing methods",
        "cpc_groups": "G06N10/80; H04L9/0858",
    }

    result = match_topic(metadata, profile)
    assert result["topic_pass"] is True
    assert result["matched_include_terms"] == ""
    assert result["matched_cpc_prefixes"] == "G06N10"


def test_apply_topic_filter_returns_audit_rows():
    profile = {
        "name": "quantum_broad",
        "include_terms_title": ["quantum"],
        "include_terms_abstract": [],
        "exclude_terms_title": ["quantum dot"],
        "exclude_terms_abstract": [],
        "cpc_include_prefixes": [],
    }
    pubnorms = ["us-1-b2", "us-2-b2"]
    metadata_by_pubnorm = {
        "us-1-b2": {"patent_title": "Quantum computer", "patent_abstract": "", "cpc_groups": ""},
        "us-2-b2": {"patent_title": "Quantum dot display", "patent_abstract": "", "cpc_groups": ""},
    }

    filtered, audit_rows = apply_topic_filter(pubnorms, metadata_by_pubnorm, profile)
    assert filtered == ["us-1-b2"]
    assert len(audit_rows) == 2
    assert audit_rows[0]["rule_profile"] == "quantum_broad"
    assert set(audit_rows[0].keys()) >= {
        "patent",
        "topic_pass",
        "matched_include_terms",
        "matched_exclude_terms",
        "matched_cpc_prefixes",
        "rule_profile",
    }
