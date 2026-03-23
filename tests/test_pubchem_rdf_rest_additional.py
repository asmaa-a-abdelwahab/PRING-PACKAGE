from __future__ import annotations

import json
from pathlib import Path

import pytest

import pring.extract.pubchem_rdf_rest as rest_mod
from pring.config import BuildCaps, BuildFlags, RdfRestConfig
from pring.extract.pubchem_rdf_rest import (
    PubChemPugClient,
    PubChemRdfRestClient,
    PubChemRdfRestExtractor,
    parse_html_table_to_rows,
    parse_ntriples_to_rows,
    parse_sparql_json_to_rows,
)


class FakeHttp:
    def __init__(self, text_map=None, json_map=None):
        self.text_map = text_map or {}
        self.json_map = json_map or {}
        self.calls = []
        self.closed = False

    def get_text(self, url, params=None):
        key = (url, tuple(sorted((params or {}).items())))
        self.calls.append(("text", key))
        v = self.text_map.get(key, "")
        if isinstance(v, Exception):
            raise v
        return v

    def get_json(self, url, params=None):
        key = (url, tuple(sorted((params or {}).items())))
        self.calls.append(("json", key))
        v = self.json_map.get(key, {})
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        self.closed = True


@pytest.fixture()
def extractor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(PubChemRdfRestExtractor, "__post_init__", lambda self: None)
    ex = PubChemRdfRestExtractor.__new__(PubChemRdfRestExtractor)
    ex.client = object()
    ex.pug = None
    return ex


def test_parse_row_formats_cover_ntriples_html_and_sparql_json():
    nt = '<http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244> <http://www.w3.org/2004/02/skos/core#prefLabel> "caffeine" .\n'
    html = "<table><tr><th>subject</th><th>predicate</th><th>object</th></tr><tr><td>compound:CID2244</td><td>skos:prefLabel</td><td>\"caffeine\"</td></tr></table>"
    sparql = {
        "head": {"vars": ["subject", "predicate", "object"]},
        "results": {"bindings": [{
            "subject": {"type": "uri", "value": "http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244"},
            "predicate": {"type": "uri", "value": "http://www.w3.org/2004/02/skos/core#prefLabel"},
            "object": {"type": "literal", "value": "caffeine"},
        }]},
    }

    assert parse_ntriples_to_rows(nt)[0]["subject"] == "<http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244>"
    assert parse_html_table_to_rows(html)[0]["predicate"] == "skos:prefLabel"
    assert parse_sparql_json_to_rows(sparql)[0]["subject"].endswith("CID2244")


def test_rdf_rest_client_query_parses_json_html_and_ntriples(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake_http = FakeHttp()
    monkeypatch.setattr(rest_mod, "HttpClient", lambda *a, **k: fake_http)
    client = PubChemRdfRestClient(RdfRestConfig(), cache_dir=tmp_path)

    base = client.cfg.base_url.rstrip("/") + "/query"
    def key(graph, subject, predicate):
        return (
            base,
            tuple(sorted({"graph": graph, "format": "json", "subject": subject, "predicate": predicate}.items())),
        )

    fake_http.text_map[key("compound", "compound:CID2244", "skos:prefLabel")] = json.dumps({
        "head": {"vars": ["subject", "predicate", "object"]},
        "results": {"bindings": [{
            "subject": {"value": "http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244"},
            "predicate": {"value": "http://www.w3.org/2004/02/skos/core#prefLabel"},
            "object": {"value": "caffeine"},
        }]},
    })
    fake_http.text_map[key("substance", "substance:SID1", "dcterms:source")] = (
        "<table><tr><th>subject</th><th>predicate</th><th>object</th></tr>"
        "<tr><td>substance:SID1</td><td>dcterms:source</td><td>source:Demo</td></tr></table>"
    )
    fake_http.text_map[key("endpoint", "endpoint:EP1", "obo:IAO_0000136")] = (
        '<http://rdf.ncbi.nlm.nih.gov/pubchem/endpoint/EP1> '
        '<http://purl.obolibrary.org/obo/IAO_0000136> '
        '<http://rdf.ncbi.nlm.nih.gov/pubchem/substance/SID1> .\n'
    )

    rows1 = client.query(graph="compound", subject="compound:CID2244", predicate="skos:prefLabel")
    rows2 = client.query(graph="substance", subject="substance:SID1", predicate="dcterms:source")
    rows3 = client.query(graph="endpoint", subject="endpoint:EP1", predicate="obo:IAO_0000136")

    assert rows1[0]["subject"] == "compound:CID2244"
    assert rows2[0]["object"] == "source:Demo"
    assert rows3[0]["object"] == "<http://rdf.ncbi.nlm.nih.gov/pubchem/substance/SID1>"
    client.close()
    assert fake_http.closed is True


def test_pug_client_extracts_cids_and_empty_inputs(monkeypatch: pytest.MonkeyPatch):
    fake_http = FakeHttp(
        json_map={
            ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/BSYNRYMUTXBXSQ-UHFFFAOYSA-N/cids/JSON", ()): {"IdentifierList": {"CID": [2244, "bad"]}},
            ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/CN1C%3DNC2%3DC1/cids/JSON", ()): {"IdentifierList": {"CID": [1]}},
            ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchi/InChI%3D1S%2FCH4%2Fh1H4/cids/JSON", ()): {"IdentifierList": {"CID": [2]}},
        }
    )
    monkeypatch.setattr(rest_mod, "HttpClient", lambda *a, **k: fake_http)
    pug = PubChemPugClient()
    assert pug.cids_from_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") == [2244]
    assert pug.cids_from_smiles("CN1C=NC2=C1") == [1]
    assert pug.cids_from_inchi("InChI=1S/CH4/h1H4") == [2]
    assert pug.cids_from_smiles(None) == []


def test_rest_seed_parsing_and_normalization_cover_supported_formats(extractor, monkeypatch: pytest.MonkeyPatch):
    class Pug:
        def cids_from_inchikey(self, _): return [2244, 2244]
        def cids_from_smiles(self, _): return [111]
        def cids_from_inchi(self, _): return [222]
    extractor.pug = Pug()

    assert extractor.parse_chemical_seed("2244")["compound"] == "compound:CID2244"
    assert extractor.parse_chemical_seed("CID=2244")["compound"] == "compound:CID2244"
    assert extractor.parse_chemical_seed("SID=7")["substance"] == "substance:SID7"
    assert extractor.parse_chemical_seed("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")["kind"] == "inchikey"
    assert extractor.parse_chemical_seed("SMILES:CCO")["kind"] == "smiles"
    assert extractor.parse_chemical_seed("InChI:InChI=1S/CH4/h1H4")["kind"] == "inchi"
    assert extractor.parse_target_seed("UNIPROT:P08684")["protein"] == "protein:ACCP08684"
    assert extractor.parse_target_seed("GENEID:1576")["gene"] == "gene:GID1576"
    assert extractor.parse_target_seed("BRCA1")["symbol_term"] == "gene:BRCA1"

    seeds = extractor.normalize_chemical_seeds([
        "2244", "CID2244", "SID7", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "SMILES:CCO", "InChI:InChI=1S/CH4/h1H4"
    ])
    terms = {s.get("compound") or s.get("substance") for s in seeds}
    assert {"compound:CID2244", "substance:SID7", "compound:CID111", "compound:CID222"}.issubset(terms)


def test_rest_resolvers_and_objects_for_support_taxid_filtering(extractor, monkeypatch: pytest.MonkeyPatch):
    def fake_query(*, graph, predicate=None, object=None, subject=None, limit=None, **kwargs):
        if graph == "gene" and predicate == "bao:BAO_0002870":
            return [{"subject": "gene:GID1576", "predicate": predicate, "object": object}]
        if graph == "gene" and subject == "gene:GID1576" and predicate == "up:organism":
            return [{"subject": subject, "predicate": predicate, "object": "taxonomy:TAXID9606"}]
        if graph == "protein" and predicate == "up:encodedBy":
            return [{"subject": "protein:ACCP08684", "predicate": predicate, "object": object}]
        if graph == "protein" and subject == "protein:ACCP08684" and predicate == "up:organism":
            return [{"subject": subject, "predicate": predicate, "object": "taxonomy:TAXID9606"}]
        raise AssertionError((graph, subject, predicate, object, limit))

    extractor.client = type("C", (), {"query": staticmethod(fake_query)})()
    assert extractor.resolve_symbols_to_genes(["BRCA1"], taxids=(9606,)) == ["gene:GID1576"]
    assert extractor.resolve_genes_to_proteins(["gene:GID1576"], taxids=(9606,)) == ["protein:ACCP08684"]

    extractor.client = type("C", (), {"query": staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))})()
    assert extractor.objects_for("endpoint", "endpoint:1", "cito:citesAsDataSource", strict=False) == []
    with pytest.raises(RuntimeError):
        extractor.objects_for("endpoint", "endpoint:1", "cito:citesAsDataSource", strict=True)


def test_iter_expand_from_compounds_emits_core_and_optional_rows(extractor, monkeypatch: pytest.MonkeyPatch):
    extractor.normalize_chemical_seeds = lambda chem_ids: [{"kind": "cid", "cid": 2244, "compound": "compound:CID2244"}]
    extractor.substances_for_compound = lambda cmp_term, cap=None: ["substance:SID1"]
    extractor.measuregroups_for_substance = lambda sub, cap=None: ["measuregroup:MG1"]
    extractor.bioassays_for_measuregroup = lambda mg: ["bioassay:AID10"]
    extractor.endpoints_for_measuregroup = lambda mg, cap=None: ["endpoint:EP1"]
    extractor.substance_for_endpoint = lambda ep: "substance:SID1"
    extractor.compound_for_substance = lambda sub: "compound:CID2244"

    data_map = {
        ("compound", "compound:CID2244", "skos:prefLabel"): ['"caffeine"'],
        ("compound", "compound:CID2244", "vocab:smiles"): ['"CN1C=NC"'],
        ("compound", "compound:CID2244", "vocab:inchikey"): ['"BSYN"'],
        ("compound", "compound:CID2244", "vocab:iupac_inchi"): ['"InChI=1S"'],
        ("compound", "compound:CID2244", "vocab:molecular_weight"): ['"194.19"'],
        ("compound", "compound:CID2244", "vocab:molecular_formula"): ['"C8H10N4O2"'],
        ("compound", "compound:CID2244", "vocab:has_parent"): ["compound:CID1"],
        ("substance", "substance:SID1", "dcterms:source"): ["source:Demo"],
        ("bioassay", "bioassay:AID10", "dcterms:title"): ['"Assay"'],
        ("bioassay", "bioassay:AID10", "dcterms:source"): ["source:Lab"],
        ("measuregroup", "measuregroup:MG1", "obo:RO_0000057"): ["protein:ACCP08684", "taxonomy:TAXID9606", "cell:CL1"],
        ("protein", "protein:ACCP08684", "skos:prefLabel"): ['"Target"'],
        ("protein", "protein:ACCP08684", "bao:BAO_0002817"): ['"MSEQ"'],
        ("protein", "protein:ACCP08684", "up:encodedBy"): ["gene:GID1576"],
        ("protein", "protein:ACCP08684", "up:organism"): ["taxonomy:TAXID9606"],
        ("cell", "cell:CL1", "obo:RO_0001000"): ["anatomy:UBERON_0002107"],
        ("endpoint", "endpoint:EP1", "rdfs:label"): ['"IC50"'],
        ("endpoint", "endpoint:EP1", "sio:SIO_000300"): ['"1.2"'],
        ("endpoint", "endpoint:EP1", "sio:SIO_000221"): ["uM"],
        ("endpoint", "endpoint:EP1", "vocab:hasQualifier"): ['">"'],
        ("endpoint", "endpoint:EP1", "vocab:PubChemAssayOutcome"): ["pubchem:Active"],
        ("endpoint", "endpoint:EP1", "obo:IAO_0000136"): ["substance:SID1"],
        ("endpoint", "endpoint:EP1", "cito:citesAsDataSource"): ["pmid:123"],
    }
    def fake_objects_for(graph, subject, predicate, cap=50, strict=True):
        return list(data_map.get((graph, subject, predicate), []))
    extractor.objects_for = fake_objects_for

    rows = list(extractor.iter_expand_from_compounds(
        ["2244"],
        caps=BuildCaps(max_substances_per_compound=1, max_measuregroups_per_compound=1, max_targets_per_compound=5, max_endpoints_per_pair=1),
        flags=BuildFlags(include_optional_context=True, include_endpoint_metadata=True, include_endpoint_references=True),
    ))
    kinds = {r["kind"] for r in rows}
    assert {"compound", "substance", "measuregroup", "bioassay", "mg_bioassay", "protein", "mg_protein", "organism", "mg_organism", "cellline", "mg_cellline", "anatomy", "cell_anatomy", "endpoint", "reference", "ep_reference"}.issubset(kinds)


def test_iter_intersection_evidence_emits_gene_protein_and_filters_by_compound(extractor, monkeypatch: pytest.MonkeyPatch):
    extractor.normalize_chemical_seeds = lambda chem_ids: [{"kind": "cid", "cid": 2244, "compound": "compound:CID2244"}]
    extractor.parse_target_seed = lambda raw: {"kind": "protein", "protein": "protein:ACCP08684"} if raw == "P08684" else {"kind": "gene", "gene": "gene:GID1576"}
    extractor.resolve_symbols_to_genes = lambda symbols, taxids=None: ["gene:GID999"]
    extractor.resolve_genes_to_proteins = lambda genes, taxids=None: ["protein:ACCQ9Y6K9"]
    extractor.measuregroups_for_participant = lambda part, cap=None: ["measuregroup:MG1"]
    extractor._mg_matches_taxids = lambda mg, taxids: True
    extractor.endpoints_for_measuregroup = lambda mg, cap=None: ["endpoint:EP1"]
    # Intersection is now compound-anchored, so we stub the compound->substance->measuregroup traversal.
    extractor.substances_for_compound = lambda cmp_term, cap=None: ["substance:SID1"]
    extractor.measuregroups_for_substance = lambda sub, cap=None: ["measuregroup:MG1"]
    extractor.substance_for_endpoint = lambda ep: "substance:SID1"
    extractor.compound_for_substance = lambda sub: "compound:CID2244"
    extractor.bioassays_for_measuregroup = lambda mg: ["bioassay:AID10"]

    describe_map = {
        ("protein", "protein:ACCP08684"): [
            ("protein:ACCP08684", "skos:prefLabel", '"Target P"'),
            ("protein:ACCP08684", "bao:BAO_0002817", '"MSEQ"'),
            ("protein:ACCP08684", "up:encodedBy", "gene:GID1576"),
            ("protein:ACCP08684", "up:organism", "taxonomy:TAXID9606"),
        ],
        ("protein", "protein:ACCQ9Y6K9"): [("protein:ACCQ9Y6K9", "skos:prefLabel", '"Target Q"')],
        ("gene", "gene:GID1576"): [("gene:GID1576", "bao:BAO_0002870", "gene:CYP3A4"), ("gene:GID1576", "skos:prefLabel", '"Gene 1576"')],
        ("bioassay", "bioassay:AID10"): [("bioassay:AID10", "dcterms:title", '"Assay"'), ("bioassay:AID10", "dcterms:source", "source:Lab")],
        ("endpoint", "endpoint:EP1"): [
            ("endpoint:EP1", "rdfs:label", '"Ki"'),
            ("endpoint:EP1", "sio:SIO_000300", '"2.5"'),
            ("endpoint:EP1", "sio:SIO_000221", "nM"),
            ("endpoint:EP1", "vocab:PubChemAssayOutcome", "pubchem:Active"),
            ("endpoint:EP1", "cito:citesAsDataSource", "pmid:123"),
            ("endpoint:EP1", "obo:IAO_0000136", "substance:SID1"),
        ],
        ("substance", "substance:SID1"): [("substance:SID1", "dcterms:source", "source:Demo")],
        ("compound", "compound:CID2244"): [("compound:CID2244", "skos:prefLabel", '"caffeine"'), ("compound:CID2244", "vocab:molecular_weight", '"194.19"')],
        ("cell", "cell:CL1"): [("cell:CL1", "obo:RO_0001000", "anatomy:UBERON_1")],
    }
    extractor.describe_subject = lambda graph, subject, limit=500: list(describe_map.get((graph, subject), []))
    extractor.objects_for = lambda graph, subject, predicate, cap=50, strict=True: {
        ("measuregroup", "measuregroup:MG1", "obo:RO_0000057"): ["protein:ACCP08684", "gene:GID1576", "taxonomy:TAXID9606", "cell:CL1"],
    }.get((graph, subject, predicate), [])

    rows = list(extractor.iter_intersection_evidence(
        ["2244"], ["P08684", "1576"],
        caps=BuildCaps(max_measuregroups_per_target=1, max_endpoints_per_pair=1),
        flags=BuildFlags(include_optional_context=True, include_endpoint_references=True, taxids=(9606,))
    ))
    kinds = {r["kind"] for r in rows}
    assert {"protein", "gene", "measuregroup", "mg_protein", "mg_gene", "organism", "cellline", "anatomy", "endpoint", "reference", "ep_reference", "compound", "substance", "bioassay"}.issubset(kinds)
