from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from pring.transform.normalizer import make_stable_id, normalize_id
from pring.transform.target_normalization import (
    infer_cyp_symbol,
    infer_uniprot_id,
    normalize_gene_props,
    normalize_protein_props,
)


@dataclass(frozen=True)
class PubChemRow:
    kind: str
    data: Dict


_DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
_PMID_RE = re.compile(r"PMID[:\s/_-]*(\d+)", re.IGNORECASE)
_PATENT_RE = re.compile(r"PATENT[:\s/_-]*([A-Z0-9-]+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_CID_RE = re.compile(r"(?:CID|compound[:/]|compound_)(\d+)", re.IGNORECASE)


def to_graph_records(rows: Iterable[PubChemRow]) -> Tuple[List[Dict], List[Dict]]:
    """Convert extracted rows into schema-aligned Neo4j node/relationship records."""
    nodes: List[Dict] = []
    rels: List[Dict] = []
    for rec_type, rec in iter_graph_records(rows):
        if rec_type == "node":
            nodes.append(rec)
        else:
            rels.append(rec)
    return nodes, rels


def iter_graph_records(rows: Iterable[PubChemRow]) -> Iterator[Tuple[str, Dict]]:
    """Stream graph node and relationship records aligned to the implementation-ready schema."""
    q: deque[Tuple[str, Dict]] = deque()

    def node(label: str, key: Dict[str, Any], props: Dict[str, Any]) -> None:
        q.append(("node", {"label": label, "key": key, "props": _drop_none(props)}))

    def rel(
        schema_label: str,
        start: Dict[str, Any],
        end: Dict[str, Any],
        props: Dict[str, Any] | None = None,
        *,
        rel_type: str | None = None,
    ) -> None:
        record = {
            "schema_label": schema_label,
            "start": start,
            "end": end,
            "props": _drop_none(props or {}),
        }
        if rel_type:
            record["type"] = rel_type
        q.append(("rel", record))

    for r in rows:
        d = r.data

        if r.kind == "compound":
            cid = _as_int(d.get("cid"))
            if cid is None:
                continue
            compound_key = {"label": "Compound", "key": {"cid": cid}}
            preferred_name = _first_nonempty(d, "preferred_name", "name", "title")
            compound_uri = _first_nonempty(d, "compound_term", "pubchem_uri")
            node("Compound", {"cid": cid}, {
                "cid": cid,
                "preferred_name": preferred_name,
                "pubchem_uri": compound_uri,
            })

            structure_props = {
                "cid": cid,
                "smiles": _first_nonempty(d, "smiles", "canonical_smiles", "isomeric_smiles"),
                "canonical_smiles": d.get("canonical_smiles"),
                "isomeric_smiles": d.get("isomeric_smiles"),
                "inchi": d.get("inchi"),
                "inchikey": d.get("inchikey"),
            }
            if _has_non_key_payload(structure_props, "cid"):
                node("Structure", {"cid": cid}, structure_props)
                rel("HAS_STRUCTURE", compound_key, {"label": "Structure", "key": {"cid": cid}})

            properties_props = {
                "cid": cid,
                "formula": d.get("formula"),
                "molecular_weight": d.get("molecular_weight"),
                "exact_mass": d.get("exact_mass"),
                "xlogp3": d.get("xlogp3"),
                "tpsa": d.get("tpsa"),
                "hbond_donor_count": d.get("hbond_donor_count"),
                "hbond_acceptor_count": d.get("hbond_acceptor_count"),
                "rotatable_bond_count": d.get("rotatable_bond_count"),
            }
            if _has_non_key_payload(properties_props, "cid"):
                node("Properties", {"cid": cid}, properties_props)
                rel("HAS_PROPERTIES", compound_key, {"label": "Properties", "key": {"cid": cid}})

            synonyms = _listify(d.get("synonyms"))
            synonym_props = {
                "cid": cid,
                "preferred": preferred_name,
                "synonyms": synonyms or None,
                "synonym_count": len(synonyms) if synonyms else None,
            }
            if _has_non_key_payload(synonym_props, "cid"):
                node("Synonyms", {"cid": cid}, synonym_props)
                rel("HAS_SYNONYMS", compound_key, {"label": "Synonyms", "key": {"cid": cid}})

            similar = _listify(d.get("similar_compounds"))
            parents = _listify(d.get("parent_compounds"))
            components = _listify(d.get("component_compounds"))
            raw_neighbors = _listify(d.get("neighbors"))
            neighbor_props = {
                "cid": cid,
                "raw_neighbors": raw_neighbors or None,
                "similar_compounds": similar or None,
                "parent_compounds": parents or None,
                "component_compounds": components or None,
                "neighbor_count": len(raw_neighbors) if raw_neighbors else None,
            }
            if _has_non_key_payload(neighbor_props, "cid"):
                node("Neighbors", {"cid": cid}, neighbor_props)
                rel("HAS_NEIGHBOR_SET", compound_key, {"label": "Neighbors", "key": {"cid": cid}})

            for rel_name, payloads in (
                ("SIMILAR_TO", similar),
                ("HAS_PARENT_COMPOUND", parents),
                ("HAS_COMPONENT_COMPOUND", components),
            ):
                for payload in payloads:
                    target_cid, rel_props = _compound_neighbor_target(payload)
                    if target_cid is None or target_cid == cid:
                        continue
                    rel(
                        rel_name,
                        compound_key,
                        {"label": "Compound", "key": {"cid": target_cid}},
                        rel_props,
                        rel_type=rel_name,
                    )

        elif r.kind == "substance":
            sid = _as_int(d.get("sid"))
            if sid is None:
                continue
            substance_key = {"label": "Substance", "key": {"sid": sid}}
            source_term = _first_nonempty(d, "source_term", "source_name")
            depositor = _source_display_name(source_term)
            node("Substance", {"sid": sid}, {
                "sid": sid,
                "pubchem_uri": _first_nonempty(d, "substance_term", "pubchem_uri"),
                "record_name": _first_nonempty(d, "record_name", "title", "name"),
                "depositor": depositor,
            })

            if source_term:
                source_id = _source_id(d)
                node("Source", {"source_id": source_id}, {
                    "source_id": source_id,
                    "name": depositor,
                    "provider": source_term if depositor != source_term else None,
                    "source_type": d.get("source_type") or "depositor",
                })
                rel("SUBMITTED_BY", substance_key, {"label": "Source", "key": {"source_id": source_id}})

            cid = _as_int(d.get("cid"))
            if cid is not None:
                rel("STANDARDIZED_TO", substance_key, {"label": "Compound", "key": {"cid": cid}})

        elif r.kind == "protein":
            pid = _as_text(d.get("protein_id"))
            if not pid:
                continue
            protein_key = {"label": "Protein", "key": {"protein_id": pid}}
            protein_props = normalize_protein_props({
                "protein_id": pid,
                "uniprot_id": infer_uniprot_id(pid, _first_nonempty(d, "protein_term", "pubchem_uri")),
                "name": _first_nonempty(d, "name", "label", "title"),
                "protein_type": _first_nonempty(d, "protein_type", "type"),
                "domain": d.get("domain"),
                "sequence": d.get("sequence"),
                "taxid": _as_int(_first_nonempty(d, "taxid", "tax_id")),
                "pubchem_uri": _first_nonempty(d, "protein_term", "pubchem_uri"),
            }, {"protein_id": pid})
            node("Protein", {"protein_id": pid}, protein_props)

            gid = _as_text(d.get("gene_id"))
            if gid:
                gene_props = normalize_gene_props({
                    "gene_id": gid,
                    "ncbi_gene_id": gid,
                    "symbol": _first_nonempty(d, "gene_symbol", "symbol") or protein_props.get("cyp_symbol"),
                    "pubchem_uri": d.get("gene_term"),
                }, {"gene_id": gid})
                node("Gene", {"gene_id": gid}, gene_props)
                rel("ENCODED_BY", protein_key, {"label": "Gene", "key": {"gene_id": gid}})

        elif r.kind == "gene":
            gid = _as_text(d.get("gene_id"))
            if not gid:
                continue
            gene_props = normalize_gene_props({
                "gene_id": gid,
                "ncbi_gene_id": gid,
                "symbol": _first_nonempty(d, "symbol", "gene_symbol"),
                "name": _first_nonempty(d, "name", "label", "title"),
                "gene_type": _first_nonempty(d, "gene_type", "type"),
                "encoding": d.get("encoding"),
                "pubchem_uri": _first_nonempty(d, "gene_term", "pubchem_uri"),
            }, {"gene_id": gid})
            node("Gene", {"gene_id": gid}, gene_props)

        elif r.kind == "organism":
            taxid = _as_int(_first_nonempty(d, "taxid", "tax_id"))
            if taxid is None:
                continue
            node("Organism", {"taxid": taxid}, {
                "taxid": taxid,
                "scientific_name": _first_nonempty(d, "scientific_name", "name"),
                "common_name": d.get("common_name"),
                "pubchem_uri": _first_nonempty(d, "tax_term", "pubchem_uri"),
            })

        elif r.kind == "bioassay":
            aid = _as_int(d.get("aid"))
            if aid is None:
                continue
            assay_key = {"label": "BioAssay", "key": {"aid": aid}}
            node("BioAssay", {"aid": aid}, _with_raw_fields({
                "aid": aid,
                "title": _first_nonempty(d, "title", "name", "assay_title"),
                "name": _first_nonempty(d, "name", "title", "assay_title"),
                "assay_type": _first_nonempty(d, "assay_type", "type"),
                "activity_outcome_method": _first_nonempty(d, "activity_outcome_method", "outcome_method", "method"),
                "source_name": _first_nonempty(d, "source_name", "source_term"),
                "pubchem_uri": _first_nonempty(d, "bioassay_term", "pubchem_uri"),
            }, d))
            source_term = _first_nonempty(d, "source_term", "source_name")
            if source_term:
                source_id = _source_id(d)
                node("Source", {"source_id": source_id}, {
                    "source_id": source_id,
                    "name": _source_display_name(source_term),
                    "provider": source_term,
                    "source_type": d.get("source_type") or "assay_source",
                })
                rel("HAS_SOURCE", assay_key, {"label": "Source", "key": {"source_id": source_id}})

        elif r.kind == "measuregroup":
            mg_id = _as_text(d.get("mg_id"))
            if not mg_id:
                continue
            node("MeasureGrp", {"mg_id": mg_id}, {
                "mg_id": mg_id,
                "pubchem_uri": _first_nonempty(d, "mg_term", "pubchem_uri"),
                "name": d.get("name"),
            })

        elif r.kind == "endpoint":
            endpoint_id = _as_text(d.get("endpoint_id"))
            if not endpoint_id:
                continue
            node("Endpoint", {"endpoint_id": endpoint_id}, {
                "endpoint_id": endpoint_id,
                "endpoint_type": _first_nonempty(d, "endpoint_type", "type"),
                "value": d.get("value"),
                "unit": d.get("unit"),
                "qualifier": d.get("qualifier"),
                "outcome_label": _first_nonempty(d, "label", "outcome"),
                "score": d.get("score"),
                "pubchem_uri": _first_nonempty(d, "endpoint_term", "pubchem_uri"),
            })
            sid = _as_int(d.get("sid"))
            if sid is not None:
                rel("ABOUT_SUBSTANCE", {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, {"label": "Substance", "key": {"sid": sid}})

            mg_id = _as_text(d.get("mg_id"))
            if mg_id:
                rel("HAS_ENDPOINT", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}})

        elif r.kind == "reference":
            reference_id, ref_props = _reference_identity(d)
            if not reference_id:
                continue
            node("Reference", {"reference_id": reference_id}, _with_raw_fields(ref_props, d))

        elif r.kind == "cellline":
            cellline_id = _as_text(d.get("cellline_id"))
            if not cellline_id:
                continue
            node("CellLine", {"cellline_id": cellline_id}, {
                "cellline_id": cellline_id,
                "name": _source_display_name(d.get("cell_term")),
                "pubchem_uri": d.get("cell_term"),
            })

        elif r.kind == "anatomy":
            anatomy_id = _as_text(d.get("anatomy_id"))
            if not anatomy_id:
                continue
            node("Anatomy", {"anatomy_id": anatomy_id}, {
                "anatomy_id": anatomy_id,
                "label": _source_display_name(d.get("anatomy_term")),
                "ontology_source": "PubChem",
                "pubchem_uri": d.get("anatomy_term"),
            })

        elif r.kind == "disease":
            disease_id = _as_text(d.get("disease_id"))
            if not disease_id:
                continue
            node("Disease", {"disease_id": disease_id}, {
                "disease_id": disease_id,
                "label": _source_display_name(d.get("disease_term")),
                "ontology_source": "PubChem",
                "pubchem_uri": d.get("disease_term"),
            })

        elif r.kind == "pathway":
            pathway_id = _as_text(d.get("pathway_id"))
            if not pathway_id:
                continue
            node("Pathway", {"pathway_id": pathway_id}, {
                "pathway_id": pathway_id,
                "title": _first_nonempty(d, "title", "name"),
                "pathway_type": d.get("type"),
                "participants": d.get("participants"),
                "pubchem_uri": _first_nonempty(d, "pathway_term", "pubchem_uri"),
            })

        elif r.kind == "mg_bioassay":
            mg_id = _as_text(d.get("mg_id"))
            aid = _as_int(d.get("aid"))
            if mg_id and aid is not None:
                rel("HAS_MEASURE_GROUP", {"label": "BioAssay", "key": {"aid": aid}}, {"label": "MeasureGrp", "key": {"mg_id": mg_id}})

        elif r.kind == "mg_protein":
            mg_id = _as_text(d.get("mg_id"))
            pid = _as_text(d.get("protein_id"))
            if mg_id and pid:
                rel("TESTED_ON", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Protein", "key": {"protein_id": pid}})

        elif r.kind == "mg_gene":
            mg_id = _as_text(d.get("mg_id"))
            gid = _as_text(d.get("gene_id"))
            if mg_id and gid:
                rel("TESTED_ON", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Gene", "key": {"gene_id": gid}})

        elif r.kind == "mg_organism":
            mg_id = _as_text(d.get("mg_id"))
            taxid = _as_int(_first_nonempty(d, "taxid", "tax_id"))
            if mg_id and taxid is not None:
                rel("IN_ORGANISM", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Organism", "key": {"taxid": taxid}})

        elif r.kind == "mg_cellline":
            mg_id = _as_text(d.get("mg_id"))
            cellline_id = _as_text(d.get("cellline_id"))
            if mg_id and cellline_id:
                rel("IN_CELL_LINE", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "CellLine", "key": {"cellline_id": cellline_id}})

        elif r.kind == "cell_anatomy":
            cellline_id = _as_text(d.get("cellline_id"))
            anatomy_id = _as_text(d.get("anatomy_id"))
            if cellline_id and anatomy_id:
                rel("DERIVED_FROM", {"label": "CellLine", "key": {"cellline_id": cellline_id}}, {"label": "Anatomy", "key": {"anatomy_id": anatomy_id}})

        elif r.kind == "ep_reference":
            endpoint_id = _as_text(d.get("endpoint_id"))
            reference_id, _ = _reference_identity(d)
            if endpoint_id and reference_id:
                rel("SUPPORTED_BY", {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, {"label": "Reference", "key": {"reference_id": reference_id}})

        elif r.kind == "protein_pathway":
            protein_id = _as_text(d.get("protein_id"))
            pathway_id = _as_text(d.get("pathway_id"))
            if protein_id and pathway_id:
                rel("PARTICIPATES_IN", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "Pathway", "key": {"pathway_id": pathway_id}})

        elif r.kind == "textmine":
            textmine_id = _as_text(_first_nonempty(d, "textmine_id", "method_id"))
            if not textmine_id:
                continue
            node("TextMine", {"textmine_id": textmine_id}, {
                "textmine_id": textmine_id,
                "method_id": _as_text(d.get("method_id")) or textmine_id,
                "name": _first_nonempty(d, "name", "method_name"),
                "version": d.get("version"),
                "source": d.get("source"),
            })

        elif r.kind == "cooc":
            cooc_id = _as_text(d.get("cooc_id"))
            if not cooc_id:
                continue
            node("Cooc", {"cooc_id": cooc_id}, {
                "cooc_id": cooc_id,
                "score": d.get("score"),
                "sentence_count": d.get("sentence_count"),
                "mention_context": d.get("mention_context"),
                "association_type": d.get("association_type"),
                "evidence_level": d.get("evidence_level") or "text_mined",
                "direction": d.get("direction"),
            })

        elif r.kind == "cooc_compound":
            cooc_id = _as_text(d.get("cooc_id"))
            cid = _as_int(d.get("cid"))
            if cooc_id and cid is not None:
                rel("MENTIONS_COMPOUND", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Compound", "key": {"cid": cid}})

        elif r.kind == "cooc_protein":
            cooc_id = _as_text(d.get("cooc_id"))
            protein_id = _as_text(d.get("protein_id"))
            if cooc_id and protein_id:
                rel("MENTIONS_PROTEIN", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Protein", "key": {"protein_id": protein_id}})

        elif r.kind == "cooc_gene":
            cooc_id = _as_text(d.get("cooc_id"))
            gene_id = _as_text(d.get("gene_id"))
            if cooc_id and gene_id:
                rel("MENTIONS_GENE", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Gene", "key": {"gene_id": gene_id}}, rel_type="MENTIONS_GENE")

        elif r.kind == "cooc_disease":
            cooc_id = _as_text(d.get("cooc_id"))
            disease_id = _as_text(d.get("disease_id"))
            if cooc_id and disease_id:
                rel("MENTIONS_DISEASE", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Disease", "key": {"disease_id": disease_id}})

        elif r.kind == "cooc_reference":
            cooc_id = _as_text(d.get("cooc_id"))
            reference_id = _as_text(d.get("reference_id")) or _as_text(d.get("ref_id"))
            if cooc_id and reference_id:
                rel("FOUND_IN_REFERENCE", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Reference", "key": {"reference_id": reference_id}})

        elif r.kind == "cooc_textmine":
            cooc_id = _as_text(d.get("cooc_id"))
            textmine_id = _as_text(_first_nonempty(d, "textmine_id", "method_id"))
            if cooc_id and textmine_id:
                rel("EXTRACTED_BY", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "TextMine", "key": {"textmine_id": textmine_id}})



        elif r.kind == "uniprot":
            acc = _as_text(_first_nonempty(d, "uniprot_acc", "accession", "acc"))
            if not acc:
                continue
            node("UniProt", {"uniprot_acc": acc}, _with_raw_fields({
                "uniprot_acc": acc,
                "reviewed": d.get("reviewed"),
                "isoform_count": _as_int(d.get("isoform_count")),
                "function": d.get("function"),
                "protein_name": _first_nonempty(d, "protein_name", "name"),
                "organism": d.get("organism"),
                "taxid": _as_int(d.get("taxid")),
                "sequence_length": _as_int(d.get("sequence_length")),
            }, d))
            protein_id = _as_text(d.get("protein_id"))
            if protein_id:
                rel("HAS_UNIPROT_RECORD", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "UniProt", "key": {"uniprot_acc": acc}})

        elif r.kind == "go":
            go_id = _as_text(_first_nonempty(d, "go_id", "id"))
            if not go_id:
                continue
            node("GO", {"go_id": go_id}, _with_raw_fields({
                "go_id": go_id,
                "name": _first_nonempty(d, "name", "label"),
                "aspect": d.get("aspect"),
                "evidence_code": d.get("evidence_code"),
            }, d))
            protein_id = _as_text(d.get("protein_id"))
            if protein_id:
                rel("HAS_GO_ANNOTATION", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "GO", "key": {"go_id": go_id}})

        elif r.kind == "reactome":
            reactome_id = _as_text(_first_nonempty(d, "reactome_id", "pathway_id", "id"))
            if not reactome_id:
                continue
            pathway_id = _as_text(d.get("pathway_id")) or f"Reactome:{reactome_id}"
            pathway_name = _first_nonempty(d, "name", "title", "label")
            node("Reactome", {"reactome_id": reactome_id}, _with_raw_fields({
                "reactome_id": reactome_id,
                "name": pathway_name,
                "species": d.get("species"),
                "pathway_id": pathway_id,
                "source_url": f"https://reactome.org/content/detail/{reactome_id}",
            }, d))
            # Bridge plugin-specific Reactome records to the generic Pathway
            # layer used by schema-level KG queries and GCN context features.
            node("Pathway", {"pathway_id": pathway_id}, {
                "pathway_id": pathway_id,
                "title": pathway_name,
                "name": pathway_name,
                "source": "Reactome",
                "pathway_type": "reactome",
                "species": d.get("species"),
                "external_id": reactome_id,
                "pubchem_uri": d.get("pubchem_uri"),
                "source_url": f"https://reactome.org/content/detail/{reactome_id}",
            })
            protein_id = _as_text(d.get("protein_id"))
            if protein_id:
                rel("MAPS_TO_REACTOME_PATHWAY", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "Reactome", "key": {"reactome_id": reactome_id}})
                rel("PARTICIPATES_IN", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "Pathway", "key": {"pathway_id": pathway_id}}, rel_type="PARTICIPATES_IN")
            rel("ALIGNS_TO_PATHWAY", {"label": "Reactome", "key": {"reactome_id": reactome_id}}, {"label": "Pathway", "key": {"pathway_id": pathway_id}})

        elif r.kind == "interpro":
            interpro_id = _as_text(_first_nonempty(d, "interpro_id", "id"))
            if not interpro_id:
                continue
            node("InterPro", {"interpro_id": interpro_id}, _with_raw_fields({
                "interpro_id": interpro_id,
                "name": _first_nonempty(d, "name", "label"),
                "type": d.get("type"),
            }, d))
            protein_id = _as_text(d.get("protein_id"))
            if protein_id:
                rel("HAS_INTERPRO_DOMAIN", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "InterPro", "key": {"interpro_id": interpro_id}})

        elif r.kind == "pdb":
            pdb_id = _as_text(_first_nonempty(d, "pdb_id", "id"))
            if not pdb_id:
                continue
            pdb_id = pdb_id.upper()
            node("PDB", {"pdb_id": pdb_id}, _with_raw_fields({
                "pdb_id": pdb_id,
                "method": d.get("method"),
                "resolution": d.get("resolution"),
                "chain_map": d.get("chain_map"),
                "pdb_url": d.get("pdb_url") or f"https://www.rcsb.org/structure/{pdb_id}",
                "source_url": d.get("source_url") or f"https://www.rcsb.org/structure/{pdb_id}",
            }, d))
            protein_id = _as_text(d.get("protein_id"))
            if protein_id:
                rel("HAS_PDB_STRUCTURE", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "PDB", "key": {"pdb_id": pdb_id}})

        elif r.kind == "alphafold":
            alphafold_id = _as_text(_first_nonempty(d, "alphafold_id", "id", "model_id"))
            if not alphafold_id:
                continue
            node("AlphaFold", {"alphafold_id": alphafold_id}, _with_raw_fields({
                "alphafold_id": alphafold_id,
                "model_version": d.get("model_version"),
                "confidence_summary": _first_nonempty(d, "confidence_summary", "plddt_summary"),
                "average_plddt": d.get("average_plddt"),
                "pdb_url": d.get("pdb_url"),
                "cif_url": d.get("cif_url"),
                "pae_url": d.get("pae_url"),
                "storage_uri": d.get("storage_uri") or d.get("pdb_url") or d.get("cif_url"),
                "model_status": d.get("model_status"),
            }, d))
            protein_id = _as_text(d.get("protein_id"))
            if protein_id:
                rel("HAS_ALPHAFOLD_MODEL", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "AlphaFold", "key": {"alphafold_id": alphafold_id}})

        elif r.kind in {"protembed", "protein_embedding"}:
            embedding_id = _as_text(_first_nonempty(d, "embedding_id", "id"))
            if not embedding_id:
                continue
            node("ProtEmbed", {"embedding_id": embedding_id}, _with_raw_fields({
                "embedding_id": embedding_id,
                "method": d.get("method"),
                "dim": _as_int(d.get("dim")),
                "storage_uri": d.get("storage_uri"),
                "version": d.get("version"),
            }, d))
            uniprot_acc = _as_text(d.get("uniprot_acc"))
            if uniprot_acc:
                rel("HAS_PROTEIN_EMBEDDING", {"label": "UniProt", "key": {"uniprot_acc": uniprot_acc}}, {"label": "ProtEmbed", "key": {"embedding_id": embedding_id}})

        elif r.kind in {"molgraph", "molecular_representation"}:
            repr_id = _as_text(_first_nonempty(d, "repr_id", "id"))
            cid = _as_int(d.get("cid"))
            if not repr_id and cid is not None:
                repr_id = f"molgraph:CID{cid}:pubchem_features_v1"
            if not repr_id:
                continue
            node("MolGraph", {"repr_id": repr_id}, _with_raw_fields({
                "repr_id": repr_id,
                "method": d.get("method") or "pubchem_features_v1",
                "fp_type": d.get("fp_type"),
                "dim": _as_int(d.get("dim")),
                "storage_uri": d.get("storage_uri"),
                "version": d.get("version"),
            }, d))
            if cid is not None:
                rel("HAS_MOLECULAR_REPRESENTATION", {"label": "Compound", "key": {"cid": cid}}, {"label": "MolGraph", "key": {"repr_id": repr_id}})

        elif r.kind == "chembl":
            chembl_id = _as_text(_first_nonempty(d, "chembl_id", "id"))
            if not chembl_id:
                continue
            node("ChEMBL", {"chembl_id": chembl_id}, _with_raw_fields({
                "chembl_id": chembl_id,
                "entity_type": d.get("entity_type"),
                "assay_id": d.get("assay_id"),
                "activity_id": d.get("activity_id"),
            }, d))
            cid = _as_int(d.get("cid"))
            endpoint_id = _as_text(d.get("endpoint_id"))
            if cid is not None:
                rel("HAS_CHEMBL_RECORD", {"label": "Compound", "key": {"cid": cid}}, {"label": "ChEMBL", "key": {"chembl_id": chembl_id}})
            if endpoint_id:
                rel("HARMONIZED_TO_CHEMBL", {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, {"label": "ChEMBL", "key": {"chembl_id": chembl_id}})

        elif r.kind == "bindingdb":
            bindingdb_id = _as_text(_first_nonempty(d, "bindingdb_id", "id"))
            if not bindingdb_id:
                continue
            node("BindingDB", {"bindingdb_id": bindingdb_id}, _with_raw_fields({
                "bindingdb_id": bindingdb_id,
                "kd": d.get("kd"),
                "ki": d.get("ki"),
                "ic50": d.get("ic50"),
                "source_ref": d.get("source_ref"),
            }, d))
            cid = _as_int(d.get("cid"))
            endpoint_id = _as_text(d.get("endpoint_id"))
            if cid is not None:
                rel("HAS_BINDINGDB_RECORD", {"label": "Compound", "key": {"cid": cid}}, {"label": "BindingDB", "key": {"bindingdb_id": bindingdb_id}})
            if endpoint_id:
                rel("VALIDATED_BY_BINDINGDB", {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, {"label": "BindingDB", "key": {"bindingdb_id": bindingdb_id}})

        elif r.kind == "drugbank":
            drugbank_id = _as_text(_first_nonempty(d, "drugbank_id", "id"))
            if not drugbank_id:
                continue
            node("DrugBank", {"drugbank_id": drugbank_id}, _with_raw_fields({
                "drugbank_id": drugbank_id,
                "name": _first_nonempty(d, "name", "label"),
                "category": d.get("category"),
                "mechanism": d.get("mechanism"),
            }, d))
            cid = _as_int(d.get("cid"))
            protein_id = _as_text(d.get("protein_id"))
            if cid is not None:
                rel("HAS_DRUGBANK_RECORD", {"label": "Compound", "key": {"cid": cid}}, {"label": "DrugBank", "key": {"drugbank_id": drugbank_id}})
            if protein_id:
                rel("HAS_DRUGBANK_ENZYME_LINK", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "DrugBank", "key": {"drugbank_id": drugbank_id}})

        elif r.kind == "interaction":
            interaction_id = _as_text(d.get("interaction_id"))
            if not interaction_id:
                cid = _as_int(d.get("cid"))
                protein_id = _as_text(d.get("protein_id"))
                if cid is not None and protein_id:
                    interaction_id = make_stable_id(f"CID{cid}|{protein_id}", "interaction")
            if not interaction_id:
                continue
            node("Interaction", {"interaction_id": interaction_id}, _with_raw_fields({
                "interaction_id": interaction_id,
                "label": d.get("label"),
                "confidence": d.get("confidence"),
                "evidence_count": _as_int(d.get("evidence_count")),
                "aggregation_rule": d.get("aggregation_rule"),
                "split": d.get("split"),
                "created_by": d.get("created_by") or "PRING",
            }, d))
            cid = _as_int(d.get("cid"))
            protein_id = _as_text(d.get("protein_id"))
            endpoint_id = _as_text(d.get("endpoint_id"))
            aid = _as_int(d.get("aid"))
            reference_id = _as_text(d.get("reference_id"))
            taxid = _as_int(d.get("taxid"))
            if cid is not None:
                rel("ASSERTS_CHEMICAL", {"label": "Interaction", "key": {"interaction_id": interaction_id}}, {"label": "Compound", "key": {"cid": cid}})
            if protein_id:
                rel("ASSERTS_TARGET", {"label": "Interaction", "key": {"interaction_id": interaction_id}}, {"label": "Protein", "key": {"protein_id": protein_id}})
            if endpoint_id:
                rel("SUPPORTED_BY_ENDPOINT", {"label": "Interaction", "key": {"interaction_id": interaction_id}}, {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}})
            if aid is not None:
                rel("SUPPORTED_BY_ASSAY", {"label": "Interaction", "key": {"interaction_id": interaction_id}}, {"label": "BioAssay", "key": {"aid": aid}})
            if reference_id:
                rel("SUPPORTED_BY_REFERENCE", {"label": "Interaction", "key": {"interaction_id": interaction_id}}, {"label": "Reference", "key": {"reference_id": reference_id}})
            if taxid is not None:
                rel("SCOPED_TO_ORGANISM", {"label": "Interaction", "key": {"interaction_id": interaction_id}}, {"label": "Organism", "key": {"taxid": taxid}})

        while q:
            yield q.popleft()


def _drop_none(props: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in props.items() if v is not None}


def _first_nonempty(d: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        m = re.search(r"(\d+)", str(value))
        return int(m.group(1)) if m else None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    txt = str(value).strip()
    return txt or None


def _listify(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]


def _has_non_key_payload(props: Dict[str, Any], key_name: str) -> bool:
    return any(v is not None for k, v in props.items() if k != key_name)


def _source_display_name(value: Any) -> str | None:
    txt = _as_text(value)
    if not txt:
        return None
    if "/" in txt:
        tail = txt.rstrip("/").split("/")[-1]
        if tail and not tail.lower().startswith("http"):
            return tail
    return txt


def _source_id(d: Dict[str, Any]) -> str:
    explicit = _as_text(d.get("source_id"))
    if explicit:
        return explicit
    source_term = _first_nonempty(d, "source_term", "source_name") or "unknown-source"
    return normalize_id(str(source_term)) or make_stable_id(str(source_term), "source")


def _reference_identity(d: Dict[str, Any]) -> tuple[str | None, Dict[str, Any]]:
    raw_id = _as_text(d.get("reference_id")) or _as_text(d.get("ref_id")) or _as_text(d.get("pmid")) or _as_text(d.get("doi"))
    raw_term = _as_text(d.get("ref_term")) or _as_text(d.get("raw_term")) or raw_id
    seed = raw_id or raw_term or _as_text(d.get("title"))
    if not seed:
        return None, {}

    pmid = _as_text(d.get("pmid"))
    doi = _as_text(d.get("doi"))
    patent_id = _as_text(d.get("patent_id"))
    year = _as_int(d.get("year"))
    title = _as_text(_first_nonempty(d, "title", "name", "article_title"))

    haystack = " ".join(v for v in [raw_id, raw_term, doi, pmid, title] if v)
    if not doi:
        m = _DOI_RE.search(haystack)
        if m:
            doi = m.group(1)
    if not pmid:
        m = _PMID_RE.search(haystack)
        if m:
            pmid = m.group(1)
    if not patent_id:
        m = _PATENT_RE.search(haystack)
        if m:
            patent_id = m.group(1)
    if year is None:
        m = _YEAR_RE.search(haystack)
        if m:
            year = int(m.group(1))

    if pmid:
        reference_id = f"PMID:{re.sub(r'[^0-9]', '', pmid) or pmid}"
    elif doi:
        reference_id = f"DOI:{doi}"
    elif raw_id:
        reference_id = raw_id
    else:
        reference_id = make_stable_id(seed, "reference")

    return reference_id, _drop_none({
        "reference_id": reference_id,
        "title": title,
        "doi": doi,
        "pmid": re.sub(r"[^0-9]", "", pmid) if pmid else None,
        "patent_id": patent_id,
        "year": year,
        "raw_term": raw_term,
        "pubchem_uri": raw_term if raw_term and (raw_term.startswith("http") or ":" in raw_term) else None,
    })

def _compound_neighbor_target(payload: Any) -> tuple[int | None, Dict[str, Any]]:
    rel_props: Dict[str, Any] = {}
    if isinstance(payload, dict):
        rel_props = {
            "score": payload.get("score"),
            "method": payload.get("method"),
            "relation_source": payload.get("relation_source"),
            "threshold": payload.get("threshold"),
        }
        candidate = payload.get("cid") or payload.get("target_cid") or payload.get("term") or payload.get("neighbor")
    else:
        candidate = payload
    if candidate is None:
        return None, {}
    if isinstance(candidate, int):
        return candidate, _drop_none(rel_props)
    txt = str(candidate)
    m = _CID_RE.search(txt)
    if m:
        return int(m.group(1)), _drop_none(rel_props)
    try:
        return int(txt), _drop_none(rel_props)
    except ValueError:
        return None, _drop_none(rel_props)

# NOTE: kept at end to avoid changing extraction logic. This helper is used by
# optional/enrichment row materializers to carry through additional parsed source
# fields as flat, Neo4j-safe properties instead of dropping them silently.
def _with_raw_fields(props: Dict[str, Any], source: Dict[str, Any], *, prefix: str = "raw_") -> Dict[str, Any]:
    out = dict(props or {})
    for key, value in (source or {}).items():
        if value is None:
            continue
        safe_key = re.sub(r"[^0-9A-Za-z_]+", "_", str(key)).strip("_") or "value"
        if safe_key in out or f"{prefix}{safe_key}" in out:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            # Keep Neo4j properties scalar/list-of-scalars safe; nested values are
            # still preserved losslessly in graph/rows/*.jsonl.
            if isinstance(value, (list, tuple, set)) and all(not isinstance(x, (dict, list, tuple, set)) for x in value):
                out[f"{prefix}{safe_key}"] = list(value)
            else:
                out[f"{prefix}{safe_key}"] = str(value)
        else:
            out[f"{prefix}{safe_key}"] = value
    return _drop_none(out)
