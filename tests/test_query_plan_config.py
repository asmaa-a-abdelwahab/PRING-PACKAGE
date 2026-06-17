from __future__ import annotations

from pathlib import Path

import pytest

from pring.config import _parse_taxids
from pring.extract.query_plan import Scope, decide_mode, decide_scope, load_id_file, Mode


def test_load_id_file_skips_comments_and_blank_lines(tmp_path: Path):
    p = tmp_path / "ids.txt"
    p.write_text("\n# comment\n2244\n\nP12345\n", encoding="utf-8")
    assert load_id_file(p) == ["2244", "P12345"]


def test_decide_scope_defaults_match_available_inputs():
    assert decide_scope(None, ["2244"], ["P12345"]) == Scope.intersection
    assert decide_scope(None, [], ["P12345"]) == Scope.expand_from_targets
    assert decide_scope(None, ["2244"], []) == Scope.expand_from_compounds


def test_decide_scope_rejects_invalid_combinations():
    with pytest.raises(ValueError):
        decide_scope("intersection", ["2244"], [])
    with pytest.raises(ValueError):
        decide_scope("expand-from-targets", ["2244"], [])
    with pytest.raises(ValueError):
        decide_scope("expand-from-compounds", [], ["P12345"])


def test_decide_mode_defaults_to_rdf_rest():
    assert decide_mode(None) == Mode.rdf_rest
    assert decide_mode("sparql") == Mode.sparql


def test_parse_taxids_accepts_mixed_formats_and_deduplicates():
    assert _parse_taxids("9606,TAXID10090,9606") == (9606, 10090)
    assert _parse_taxids("") is None
    assert _parse_taxids(None) is None


def test_settings_from_env_reads_latest_modeling_and_textmining_knobs(monkeypatch):
    from pring.config import Settings

    monkeypatch.setenv("PRING_INCLUDE_ENDPOINT_REFERENCES", "true")
    monkeypatch.setenv("PRING_TEXTMINING_PUBMED_FALLBACK", "false")
    monkeypatch.setenv("PRING_MAX_CANDIDATE_MISSING_PAIRS", "none")
    monkeypatch.setenv("PRING_CANDIDATE_PAIR_MODE", "all")

    settings = Settings.from_env()

    assert settings.flags.include_endpoint_references is True
    assert settings.textmining_pubmed_fallback is False
    assert settings.max_candidate_missing_pairs is None
    assert settings.candidate_pair_mode == "all"


def test_settings_default_endpoint_references_are_throttle_safe(monkeypatch):
    from pring.config import Settings

    monkeypatch.delenv("PRING_INCLUDE_ENDPOINT_REFERENCES", raising=False)
    settings = Settings.from_env()
    assert settings.flags.include_endpoint_references is False
