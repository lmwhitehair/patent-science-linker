from pathlib import Path

from src.pipeline.patentsview_local import _resolve_data_file


def test_resolve_data_file_prefers_unzipped(tmp_path: Path):
    tsv = tmp_path / "g_patent.tsv"
    zipf = tmp_path / "g_patent.tsv.zip"
    tsv.write_text("x", encoding="utf-8")
    zipf.write_text("y", encoding="utf-8")

    out = _resolve_data_file(tmp_path, "g_patent.tsv")
    assert out == tsv


def test_resolve_data_file_falls_back_to_zip(tmp_path: Path):
    zipf = tmp_path / "g_patent.tsv.zip"
    zipf.write_text("y", encoding="utf-8")

    out = _resolve_data_file(tmp_path, "g_patent.tsv")
    assert out == zipf
