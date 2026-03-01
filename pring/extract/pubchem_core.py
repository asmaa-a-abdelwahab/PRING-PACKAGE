from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from pring.transform.normalizer import make_stable_id, normalize_id


@dataclass(frozen=True)
class PubChemRow:
    kind: str
    data: Dict


def to_graph_records(rows: Iterable[PubChemRow]) -> Tuple[List[Dict], List[Dict]]:
    """Convert extracted rows into schema-aligned Neo4j node/relationship records.

    Design goals:
    - Follow the PRING DOT schema node labels (Compound/Substance/Protein/Gene/MeasureGrp/Endpoint/...)
    - Follow DOT edge labels via `schema_label` (Neo4jLoader maps to REL_TYPE automatically)
    - Keep extraction vs transformation concerns separate
    """

    nodes: List[Dict] = []
    rels: List[Dict] = []

    def node(label: str, key: Dict, props: Dict) -> None:
        nodes.append({"label": label, "key": key, "props": props})

    def rel(schema_label: str, start: Dict, end: Dict, props: Dict | None = None) -> None:
        rels.append({"schema_label": schema_label, "start": start, "end": end, "props": props or {}})

    for r in rows:
        d = r.data

        # ----------------
        # Core entities
        # ----------------
        if r.kind == "compound":
            cid = d.get("cid")
            if cid is None:
                continue
            cid = int(cid)
            node("Compound", {"cid": cid}, {
                "cid": cid,
                "name": d.get("name"),
                "term": d.get("compound_term"),
            })

            # Structure node
            node("Structure", {"cid": cid}, {
                "cid": cid,
                "smiles": d.get("smiles"),
                "inchikey": d.get("inchikey"),
                "inchi": d.get("inchi"),
            })
            rel("has structure", {"label": "Compound", "key": {"cid": cid}}, {"label": "Structure", "key": {"cid": cid}})

            # Properties node
            node("Properties", {"cid": cid}, {
                "cid": cid,
                "formula": d.get("formula"),
                "molecular_weight": d.get("molecular_weight"),
                "xlogp3": d.get("xlogp3"),
                "tpsa": d.get("tpsa"),
            })
            rel("has properties", {"label": "Compound", "key": {"cid": cid}}, {"label": "Properties", "key": {"cid": cid}})

            # Synonyms node (optional; we at least store preferred name)
            node("Synonyms", {"cid": cid}, {
                "cid": cid,
                "preferred": d.get("name"),
                "synonyms": d.get("synonyms"),
            })
            rel("has names", {"label": "Compound", "key": {"cid": cid}}, {"label": "Synonyms", "key": {"cid": cid}})

            # Neighbors (optional)
            node("Neighbors", {"cid": cid}, {
                "cid": cid,
                "neighbors": d.get("neighbors"),
            })
            rel("has neighbors / parents", {"label": "Compound", "key": {"cid": cid}}, {"label": "Neighbors", "key": {"cid": cid}})

        elif r.kind == "substance":
            sid = d.get("sid")
            if sid is None:
                continue
            sid = int(sid)
            node("Substance", {"sid": sid}, {
                "sid": sid,
                "term": d.get("substance_term"),
            })

            # Substance -> Source (submitted by)
            src_term = d.get("source_term")
            if src_term:
                src_id = normalize_id(src_term)
                node("Source", {"source_id": src_id}, {"source_id": src_id, "term": src_term})
                rel("submitted by", {"label": "Substance", "key": {"sid": sid}}, {"label": "Source", "key": {"source_id": src_id}})

            # Normalization edge Substance -> Compound is created from endpoint-derived mapping
            # (we don't force it here because extractor may load Substance before Compound)
            if d.get("cid") is not None:
                cid = int(d["cid"])
                rel("standardized to\n(normalized)", {"label": "Substance", "key": {"sid": sid}}, {"label": "Compound", "key": {"cid": cid}})

        elif r.kind == "protein":
            pid = d.get("protein_id")
            if not pid:
                continue
            pid = str(pid)
            node("Protein", {"protein_id": pid}, {
                "protein_id": pid,
                "name": d.get("name"),
                "sequence": d.get("sequence"),
                "term": d.get("protein_term"),
            })

            # Protein -> Gene (encoded by)
            gid = d.get("gene_id")
            if gid:
                gid = str(gid)
                node("Gene", {"gene_id": gid}, {"gene_id": gid, "term": d.get("gene_term")})
                rel("encoded by", {"label": "Protein", "key": {"protein_id": pid}}, {"label": "Gene", "key": {"gene_id": gid}})

        elif r.kind == "gene":
            gid = d.get("gene_id")
            if not gid:
                continue
            gid = str(gid)
            node("Gene", {"gene_id": gid}, {
                "gene_id": gid,
                "name": d.get("name"),
                "symbol": d.get("symbol"),
                "term": d.get("gene_term"),
            })

        elif r.kind == "organism":
            tax_id = d.get("tax_id")
            if tax_id is None:
                continue
            tax_id = int(tax_id)
            node("Organism", {"tax_id": tax_id}, {"tax_id": tax_id, "term": d.get("tax_term")})

        elif r.kind == "bioassay":
            aid = d.get("aid")
            if aid is None:
                continue
            aid = int(aid)
            node("BioAssay", {"aid": aid}, {
                "aid": aid,
                "name": d.get("name"),
                "term": d.get("bioassay_term"),
            })

            # BioAssay -> Source
            src_term = d.get("source_term")
            if src_term:
                src_id = normalize_id(src_term)
                node("Source", {"source_id": src_id}, {"source_id": src_id, "term": src_term})
                rel("has source", {"label": "BioAssay", "key": {"aid": aid}}, {"label": "Source", "key": {"source_id": src_id}})

        elif r.kind == "measuregroup":
            mg_id = d.get("mg_id")
            if not mg_id:
                continue
            mg_id = str(mg_id)
            node("MeasureGrp", {"mg_id": mg_id}, {"mg_id": mg_id, "term": d.get("mg_term")})

        elif r.kind == "endpoint":
            eid = d.get("endpoint_id")
            if not eid:
                continue
            eid = str(eid)
            node("Endpoint", {"endpoint_id": eid}, {
                "endpoint_id": eid,
                "type": d.get("type"),
                "value": d.get("value"),
                "unit": d.get("unit"),
                "qualifier": d.get("qualifier"),
                "outcome": d.get("outcome"),
                "label": d.get("label"),
                "term": d.get("endpoint_term"),
            })

            # Endpoint -> Substance
            sid = d.get("sid")
            if sid is not None:
                sid = int(sid)
                rel("is about tested record", {"label": "Endpoint", "key": {"endpoint_id": eid}}, {"label": "Substance", "key": {"sid": sid}})

            # MeasureGrp -> Endpoint
            mg = d.get("mg_id")
            if mg:
                rel("produces endpoint", {"label": "MeasureGrp", "key": {"mg_id": str(mg)}}, {"label": "Endpoint", "key": {"endpoint_id": eid}})

        elif r.kind == "reference":
            rid = d.get("ref_id")
            if not rid:
                continue
            rid = str(rid)
            node("Reference", {"ref_id": rid}, {
                "ref_id": rid,
                "term": d.get("ref_term"),
            })

        elif r.kind == "cellline":
            cid = d.get("cellline_id")
            if not cid:
                continue
            cid = str(cid)
            node("CellLine", {"cellline_id": cid}, {"cellline_id": cid, "term": d.get("cell_term")})

        elif r.kind == "anatomy":
            aid = d.get("anatomy_id")
            if not aid:
                continue
            aid = str(aid)
            node("Anatomy", {"anatomy_id": aid}, {"anatomy_id": aid, "term": d.get("anatomy_term")})

        # ----------------
        # Join rows => edges
        # ----------------
        elif r.kind == "mg_bioassay":
            mg_id = str(d.get("mg_id"))
            aid = d.get("aid")
            if not mg_id or aid is None:
                continue
            rel("has measure group", {"label": "BioAssay", "key": {"aid": int(aid)}}, {"label": "MeasureGrp", "key": {"mg_id": mg_id}})

        elif r.kind == "mg_protein":
            mg_id = str(d.get("mg_id"))
            pid = d.get("protein_id")
            if not mg_id or not pid:
                continue
            rel("tested on protein", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Protein", "key": {"protein_id": str(pid)}})

        elif r.kind == "mg_organism":
            mg_id = str(d.get("mg_id"))
            tax_id = d.get("tax_id")
            if not mg_id or tax_id is None:
                continue
            rel("in organism", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Organism", "key": {"tax_id": int(tax_id)}})

        elif r.kind == "mg_cellline":
            mg_id = str(d.get("mg_id"))
            cellline_id = d.get("cellline_id")
            if not mg_id or not cellline_id:
                continue
            rel("in cell line (optional)", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "CellLine", "key": {"cellline_id": str(cellline_id)}})

        elif r.kind == "cell_anatomy":
            cellline_id = d.get("cellline_id")
            anatomy_id = d.get("anatomy_id")
            if not cellline_id or not anatomy_id:
                continue
            rel("derived from (optional)", {"label": "CellLine", "key": {"cellline_id": str(cellline_id)}}, {"label": "Anatomy", "key": {"anatomy_id": str(anatomy_id)}})

        elif r.kind == "ep_reference":
            eid = d.get("endpoint_id")
            rid = d.get("ref_id")
            if not eid or not rid:
                continue
            rel("supported by", {"label": "Endpoint", "key": {"endpoint_id": str(eid)}}, {"label": "Reference", "key": {"ref_id": str(rid)}})

        # else: ignore unknown kinds

    return nodes, rels
