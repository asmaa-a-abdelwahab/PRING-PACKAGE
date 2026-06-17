from __future__ import annotations

import pytest

import pring.extract.pubchem_sparql_mirror as sparql_mod
from pring.config import BuildCaps, BuildFlags, SparqlConfig
from pring.extract.pubchem_sparql_mirror import PubChemSparqlMirrorExtractor, SparqlMirrorClient


class FakeHttp:
    def __init__(self, payload=None):
        self.payload = payload or {"results": {"bindings": []}}
        self.calls = []
        self.closed = False

    def post_json(self, url, data=None, headers=None):
        self.calls.append((url, data, headers))
        return self.payload

    def close(self):
        self.closed = True


class DummyClient:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.queries = []

    def select(self, query):
        self.queries.append(query)
        if self.rows and isinstance(self.rows[0], list):
            return self.rows.pop(0)
        return list(self.rows)

    def close(self):
        return None


def test_sparql_client_select_and_close(monkeypatch: pytest.MonkeyPatch):
    fake_http = FakeHttp(payload={"results": {"bindings": [{"x": {"value": "1"}}]}})
    monkeypatch.setattr(sparql_mod, "HttpClient", lambda *a, **k: fake_http)
    client = SparqlMirrorClient(SparqlConfig(endpoint_url="https://example.org/sparql"))
    rows = client.select("SELECT * WHERE {}")
    assert rows == [{"x": {"value": "1"}}]
    client.close()
    assert fake_http.closed is True


def test_sparql_helper_queries_cover_tax_filters_and_caps():
    client = DummyClient(rows=[
        [{"gene": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/gene/GID1576"}}],
        [{"protein": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/protein/ACCP08684"}}],
        [{"mg": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/measuregroup/MG1"}}],
        [{"sub": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/substance/SID1"}}],
        [{"mg": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/measuregroup/MG2"}}],
    ])
    ex = PubChemSparqlMirrorExtractor(client)

    geneids = ex._resolve_symbols_to_geneids(["gene:BRCA1"])
    prots = ex._genes_to_proteins(geneids, BuildFlags(taxids=(9606,)))
    mgs = ex._select_measuregroups_for_proteins(prots + geneids, BuildCaps(max_measuregroups_per_target=2), BuildFlags(taxids=(9606,)))
    subs = ex._select_substances_for_compounds(["compound:CID2244", "substance:SID9"], BuildCaps(max_substances_per_compound=1))
    mgs2 = ex._select_measuregroups_for_substances(subs, BuildCaps(max_measuregroups_per_compound=2))

    assert geneids == ["gene:GID1576"]
    assert prots == ["protein:ACCP08684"]
    assert mgs == ["measuregroup:MG1"]
    assert subs == ["substance:SID9", "substance:SID1"]
    assert mgs2 == ["measuregroup:MG2"]
    assert ex._tax_filter_on_var("?protein", "?tax", BuildFlags(taxids=(9606, 10090)))
    assert ex._tax_mg_filter(BuildFlags(taxids=(9606,))) == ""


def test_emit_from_measuregroups_yields_full_row_stream_with_optional_context():
    binding = {
        "mg": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/measuregroup/MG1"},
        "bioassay": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/bioassay/AID10"},
        "baname": {"value": "Assay"},
        "endpoint": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/endpoint/EP1"},
        "sub": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/substance/SID1"},
        "compound": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244"},
        "protein": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/protein/ACCP08684"},
        "tax": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/taxonomy/TAXID9606"},
        "geneTarget": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/gene/GID1576"},
        "gname": {"value": "gene name"},
        "gsNode": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/gene/CYP3A4"},
        "cell": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/cell/CL1"},
        "anat": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/anatomy/UBERON_1"},
        "value": {"value": "1.2"},
        "unit": {"value": "uM"},
        "qual": {"value": ">"},
        "outcome": {"value": "pubchem:Active"},
        "eplabel": {"value": "IC50"},
        "ref": {"value": "pmid:123"},
        "pname": {"value": "Protein name"},
        "seq": {"value": "MSEQ"},
        "gene": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/gene/GID1576"},
        "cname": {"value": "caffeine"},
        "smiles": {"value": "CN1C=NC"},
        "inchikey": {"value": "BSYN"},
        "inchi": {"value": "InChI=1S"},
        "formula": {"value": "C8H10N4O2"},
        "mw": {"value": "194.19"},
        "xlogp3": {"value": "-0.1"},
        "tpsa": {"value": "61.8"},
        "source": {"value": "source:Demo"},
    }
    ex = PubChemSparqlMirrorExtractor(DummyClient())
    ex._select_evidence_rows_for_measuregroups = lambda *a, **k: [binding]

    rows = list(ex._emit_from_measuregroups(["measuregroup:MG1"], BuildCaps(max_endpoints_per_pair=1), BuildFlags(include_optional_context=True, include_endpoint_references=True)))
    kinds = {r["kind"] for r in rows}
    assert {"measuregroup", "bioassay", "mg_bioassay", "gene", "mg_gene", "protein", "mg_protein", "cellline", "mg_cellline", "anatomy", "cell_anatomy", "organism", "mg_organism", "compound", "substance", "endpoint", "reference", "ep_reference"}.issubset(kinds)


def test_sparql_iterators_delegate_and_apply_restrictions():
    ex = PubChemSparqlMirrorExtractor(DummyClient())
    ex._parse_targets = lambda ids: (["protein:ACCP08684"], ["gene:BRCA1"])
    ex._resolve_symbols_to_geneids = lambda genes: ["gene:GID1576"]
    ex._genes_to_proteins = lambda geneids, flags: ["protein:ACCQ9Y6K9"]
    ex._select_measuregroups_for_proteins = lambda targets, caps, flags: ["measuregroup:MG1"]
    ex._parse_compounds = lambda ids: ["compound:CID2244"]
    ex._select_substances_for_compounds = lambda terms, caps: ["substance:SID1"]
    ex._select_measuregroups_for_substances = lambda subs, caps: ["measuregroup:MG2"]
    seen = []
    def fake_emit(mgs, caps, flags, restrict_compounds=None, restrict_proteins=None):
        seen.append((tuple(mgs), restrict_compounds, restrict_proteins))
        yield {"kind": "measuregroup", "data": {"mg_id": mgs[0]}}
    ex._emit_from_measuregroups = fake_emit

    rows_t = list(ex.iter_expand_from_targets(["P08684"], BuildCaps(max_measuregroups_per_target=1), BuildFlags()))
    rows_c = list(ex.iter_expand_from_compounds(["2244"], BuildCaps(max_measuregroups_per_compound=1), BuildFlags(taxids=(9606,))))

    ex._parse_compounds = lambda ids: ["compound:CID2244"]
    ex.client = DummyClient(rows=[[{"mg": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/measuregroup/MG3"}}]])
    rows_i = list(ex.iter_intersection_evidence(["2244"], ["P08684"], BuildCaps(max_measuregroups_per_target=1), BuildFlags()))

    assert rows_t and rows_c and rows_i
    assert seen[0][2] == {"protein:ACCP08684", "protein:ACCQ9Y6K9"}
    assert seen[1][1] == {"compound:CID2244"}
