from __future__ import annotations

from pring.extract.filters import keep_endpoints
from pring.extract.pubchem_rdf_rest import (
    parse_html_table_to_rows,
    parse_ntriples_to_rows,
    parse_sparql_json_to_rows,
)
from pring.transform.normalizer import make_stable_id, normalize_id, rel_type_from_schema_label


def test_parse_ntriples_to_rows_extracts_spo():
    text = "<s1> <p1> <o1> .\n# ignore\ncompound:CID2244 rdf:type cheminf:CHEMINF_000000 .\n"
    rows = parse_ntriples_to_rows(text)
    assert rows[0] == {"subject": "<s1>", "predicate": "<p1>", "object": "<o1>"}
    assert rows[1]["subject"] == "compound:CID2244"


def test_parse_html_table_to_rows_prefers_href_values():
    html = """
    <table>
      <tr><th>subject</th><th>predicate</th><th>object</th></tr>
      <tr>
        <td><a href=\"https://pubchem.ncbi.nlm.nih.gov/compound/CID2244\">compound</a></td>
        <td>rdf:type</td>
        <td><a href=\"http://example.org/Thing\">Thing</a></td>
      </tr>
    </table>
    """
    rows = parse_html_table_to_rows(html)
    assert rows == [{
        "subject": "https://pubchem.ncbi.nlm.nih.gov/compound/CID2244",
        "predicate": "rdf:type",
        "object": "http://example.org/Thing",
    }]


def test_parse_sparql_json_to_rows_flattens_bindings():
    data = {
        "results": {
            "bindings": [
                {
                    "subject": {"type": "uri", "value": "s"},
                    "predicate": {"type": "uri", "value": "p"},
                    "object": {"type": "literal", "value": "o"},
                }
            ]
        }
    }
    assert parse_sparql_json_to_rows(data) == [{"subject": "s", "predicate": "p", "object": "o"}]


def test_normalizer_helpers_are_stable_and_clean():
    assert normalize_id("  Hello / World  ") == "HelloWorld"
    assert rel_type_from_schema_label("standardized to\n(normalized)") == "STANDARDIZED_TO"
    assert make_stable_id("a", 1, prefix="mg:").startswith("mg:")


def test_keep_endpoints_filters_type_and_numeric_values():
    endpoints = [
        {"type": "IC50", "value": "3.2"},
        {"type": "Ki", "value": "not-a-number"},
        {"type": "EC50", "value": 9},
    ]
    kept = list(keep_endpoints(endpoints, allowed_types={"ic50", "ec50"}, require_numeric_value=True))
    assert kept == [
        {"type": "IC50", "value": "3.2"},
        {"type": "EC50", "value": 9},
    ]
