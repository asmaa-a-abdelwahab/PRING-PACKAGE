from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from pring.transform.normalizer import make_stable_id, normalize_id


@dataclass(frozen=True)
class PubChemRow:
    kind: str
    data: Dict


def to_graph_records(rows: Iterable[PubChemRow]) -> Tuple[List[Dict], List[Dict]]:
    """Convert extracted rows into base schema node/relationship records.

    Keep this focused on *schema-aligned* nodes/edges.
    Extraction produces normalized rows; transformation here produces Neo4j-ready records.
    """
    nodes: List[Dict] = []
    rels: List[Dict] = []

    for r in rows:
        d = r.data

        if r.kind == "compound":
            cid = d.get("cid")
            if cid is None:
                continue
            cid = int(cid)
            nodes.append({"label": "Compound", "key": {"cid": cid}, "props": {"cid": cid, "name": d.get("name")}})
            nodes.append({"label": "Structure", "key": {"cid": cid}, "props": {"cid": cid, "smiles": d.get("smiles"), "inchikey": d.get("inchikey"), "inchi": d.get("inchi")}})
            rels.append({"type": "HAS_STRUCTURE", "start": {"label": "Compound", "key": {"cid": cid}}, "end": {"label": "Structure", "key": {"cid": cid}}, "props": {}})

        elif r.kind == "substance":
            sid = d.get("sid"); cid = d.get("cid")
            if sid is None or cid is None:
                continue
            sid = int(sid); cid = int(cid)
            nodes.append({"label": "Substance", "key": {"sid": sid}, "props": {"sid": sid}})
            rels.append({"type": "STANDARDIZED_TO", "start": {"label": "Substance", "key": {"sid": sid}}, "end": {"label": "Compound", "key": {"cid": cid}}, "props": {}})
            if d.get("source_id"):
                src = normalize_id(d["source_id"])
                nodes.append({"label": "Source", "key": {"source_id": src}, "props": {"source_id": src, "name": d.get("source_name")}})
                rels.append({"type": "SUBMITTED_BY", "start": {"label": "Substance", "key": {"sid": sid}}, "end": {"label": "Source", "key": {"source_id": src}}, "props": {}})

        elif r.kind == "bioassay":
            aid = d.get("aid")
            if aid is None:
                continue
            aid = int(aid)
            nodes.append({"label": "BioAssay", "key": {"aid": aid}, "props": {"aid": aid, "name": d.get("name"), "description": d.get("description")}})

        elif r.kind == "measuregroup":
            mg_id = d.get("mg_id")
            aid = d.get("aid")
            if mg_id is None:
                continue
            mg_id = str(mg_id)
            nodes.append({"label": "MeasureGrp", "key": {"mg_id": mg_id}, "props": {"mg_id": mg_id}})
            if aid is not None:
                aid = int(aid)
                rels.append({"type": "DERIVED_FROM_ASSAY", "start": {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, "end": {"label": "BioAssay", "key": {"aid": aid}}, "props": {}})

            # Optional links (if present)
            if d.get("protein_id"):
                pid = normalize_id(d.get("protein_id"))
                nodes.append({"label": "Protein", "key": {"protein_id": pid}, "props": {"protein_id": pid}})
                rels.append({"type": "TESTED_ON_PROTEIN", "start": {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, "end": {"label": "Protein", "key": {"protein_id": pid}}, "props": {}})

            if d.get("gene_id"):
                gid = normalize_id(d.get("gene_id"))
                nodes.append({"label": "Gene", "key": {"gene_id": gid}, "props": {"gene_id": gid}})
                rels.append({"type": "TESTED_ON_GENE", "start": {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, "end": {"label": "Gene", "key": {"gene_id": gid}}, "props": {}})

        elif r.kind == "endpoint":
            sid = d.get("sid")
            mg = d.get("mg_id")
            aid = d.get("aid")
            if sid is None or mg is None:
                continue
            endpoint_id = d.get("endpoint_id") or make_stable_id(aid, mg, sid, d.get("type"), d.get("value"), prefix="ep:")
            nodes.append({"label": "Endpoint", "key": {"endpoint_id": endpoint_id}, "props": {
                "endpoint_id": endpoint_id,
                "type": d.get("type"),
                "value": d.get("value"),
                "unit": d.get("unit"),
                "outcome": d.get("outcome"),
            }})
            rels.append({"type": "IS_ABOUT_TESTED_RECORD", "start": {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, "end": {"label": "Substance", "key": {"sid": int(sid)}}, "props": {}})
            rels.append({"type": "PRODUCES_ENDPOINT", "start": {"label": "MeasureGrp", "key": {"mg_id": str(mg)}}, "end": {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, "props": {}})

        # TODO: Reference, Pathway, CellLine, Organism, TextMine, Cooc, etc.

    return nodes, rels
