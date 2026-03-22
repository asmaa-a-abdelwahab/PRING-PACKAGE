from __future__ import annotations

from pring.config import BuildCaps, BuildFlags
from pring.extract.pubchem_rdf_rest import PubChemRdfRestExtractor


def test_optional_endpoint_reference_failure_does_not_abort_build():
    ex = object.__new__(PubChemRdfRestExtractor)

    ex.normalize_chemical_seeds = lambda chem_ids: [{"compound": "compound:CID2244"}]
    ex.substances_for_compound = lambda cmp_term, cap=200: ["substance:SID1"]
    ex.compound_for_substance = lambda sub: "compound:CID2244"
    ex.measuregroups_for_substance = lambda sub, cap=200: ["measuregroup:MG1"]
    ex.bioassays_for_measuregroup = lambda mg: ["bioassay:AID1"]
    ex.endpoints_for_measuregroup = lambda mg, cap=50: ["endpoint:SID1_AID1_VALUE1"]
    ex.substance_for_endpoint = lambda ep: "substance:SID1"
    ex._term_id = lambda term: term.split(":", 1)[-1]
    ex._extract_taxid = lambda term: None

    def objects_for(graph, subject, predicate, cap=50, strict=True):
        if (graph, predicate) == ("compound", "skos:prefLabel"):
            return ['"caffeine"']
        if (graph, predicate) == ("measuregroup", "obo:RO_0000057"):
            return ["protein:ACCQ9XYZ1"]
        if (graph, predicate) == ("endpoint", "rdfs:label"):
            return ['"IC50"']
        if (graph, predicate) == ("endpoint", "sio:SIO_000300"):
            return ['"3.2"']
        if (graph, predicate) == ("endpoint", "obo:IAO_0000136"):
            return ["substance:SID1"]
        if (graph, predicate) == ("endpoint", "cito:citesAsDataSource"):
            if strict:
                raise RuntimeError("503")
            return []
        return []

    ex.objects_for = objects_for

    rows = list(PubChemRdfRestExtractor.iter_expand_from_compounds(
        ex,
        ["2244"],
        caps=BuildCaps(max_endpoints_per_pair=10),
        flags=BuildFlags(include_optional_context=True),
    ))

    endpoint_rows = [r for r in rows if r["kind"] == "endpoint"]
    assert endpoint_rows, "endpoint row should still be emitted when optional refs fail"
    assert endpoint_rows[0]["data"]["type"] == "IC50"
