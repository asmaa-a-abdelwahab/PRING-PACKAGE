from __future__ import annotations

"""PubChemRDF REST (triple-pattern) extraction.

This module implements the *seeded* KG build strategy for PRING:
- User provides chemical IDs (CID/SID/compound:CURIE) and target IDs (UniProt/GeneID/gene:CURIE)
- We retrieve ONLY the relevant subgraph (intersection by default)
- We materialize rows aligned with the PRING DOT schema

Why RDF-REST?
- For thesis-sized graphs (CYP450-focused), the REST triple-pattern endpoint lets us avoid
  downloading massive dumps.
- We use a compact, restartable on-disk cache to keep iteration fast.

Notes on response format:
- The REST endpoint returns SPARQL Results JSON ("head/vars" + "results/bindings").
- Variable names can vary (s/p/o vs subject/predicate/object). We handle both.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import hashlib
import json
import re

from pring.config import RdfRestConfig
from pring.io.http import HttpClient
from pring.transform.normalizer import make_stable_id, normalize_id


def parse_sparql_results_json(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Parse SPARQL Results JSON to a list of simple dicts {var:value}."""
    out: List[Dict[str, str]] = []
    vars_ = (data.get("head") or {}).get("vars") or []
    bindings = ((data.get("results") or {}).get("bindings")) or []
    for b in bindings:
        row: Dict[str, str] = {}
        for v in vars_:
            if v in b and isinstance(b[v], dict) and "value" in b[v]:
                row[v] = b[v]["value"]
        if row:
            out.append(row)
    return out


@dataclass
class PubChemRdfRestClient:
    cfg: RdfRestConfig
    cache_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        self.http = HttpClient(timeout_s=self.cfg.timeout_s, max_retries=self.cfg.max_retries, headers=headers)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.http.close()

    def _cache_key(self, url: str, params: Dict[str, Any]) -> str:
        raw = (url + "|" + json.dumps(params, sort_keys=True, ensure_ascii=True)).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def query(
        self,
        *,
        graph: str,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        object: Optional[str] = None,
        pred: Optional[str] = None,
        obj: Optional[str] = None,
        name: Optional[str] = None,
        contain: Optional[bool] = None,
        return_: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        format: str = "json",
    ) -> List[Dict[str, str]]:
        """Run a PubChem RDF REST triple-pattern query.

        Endpoint:
          {base_url}/query

        Common params:
          graph=... (required)
          subject=..., predicate/pred=..., object/obj=...  (triple-pattern)
          limit, offset, format=json

        Returns:
          list of dicts {var:value} from SPARQL Results JSON.
        """
        url = self.cfg.base_url.rstrip("/") + "/query"
        params: Dict[str, Any] = {"graph": graph, "format": format}

        if subject is not None:
            params["subject"] = subject
        if predicate is not None:
            params["predicate"] = predicate
        if object is not None:
            params["object"] = object

        if pred is not None:
            params["pred"] = pred
        if obj is not None:
            params["obj"] = obj

        if name is not None:
            params["name"] = name
        if contain is not None:
            params["contain"] = "true" if contain else "false"
        if return_ is not None:
            params["return"] = return_

        if limit is not None:
            params["limit"] = int(limit)
        if offset is not None:
            params["offset"] = int(offset)

        # Cache (optional)
        if self.cache_dir is not None:
            ck = self._cache_key(url, params)
            p = self.cache_dir / f"rdfrest_{graph}_{ck}.json"
            if p.exists():
                try:
                    return json.loads(p.read_text("utf-8"))
                except Exception:
                    pass

        data = self.http.get_json(url, params=params)
        rows = parse_sparql_results_json(data)

        if self.cache_dir is not None:
            try:
                p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return rows


@dataclass
class PubChemRdfRestExtractor:
    """Extract PRING graph from PubChemRDF via RDF REST triple-pattern queries."""

    client: PubChemRdfRestClient

    _PUBCHEM_IRI_RE = re.compile(r"^https?://rdf\.ncbi\.nlm\.nih\.gov/pubchem/([^/]+)/([^/#]+)$")

    # ---------- term normalization ----------
    def iri_to_term(self, value: str) -> str:
        if value is None:
            return value
        v = str(value).strip()
        m = self._PUBCHEM_IRI_RE.match(v)
        if m:
            return f"{m.group(1)}:{m.group(2)}"
        return v

    def _term_id(self, term: str) -> str:
        return normalize_id(term) or term

    def _subjects(self, rows: List[Dict[str, str]]) -> List[str]:
        out: List[str] = []
        for r in rows:
            s = r.get("subject") or r.get("s") or r.get("sub")
            if s:
                out.append(self.iri_to_term(s))
        return out

    def _objects(self, rows: List[Dict[str, str]]) -> List[str]:
        out: List[str] = []
        for r in rows:
            o = r.get("object") or r.get("o") or r.get("obj")
            if o:
                out.append(self.iri_to_term(o))
        return out

    def _triples(self, rows: List[Dict[str, str]]) -> List[Tuple[str, str, str]]:
        out: List[Tuple[str, str, str]] = []
        for r in rows:
            s = r.get("subject") or r.get("s") or r.get("sub")
            p = r.get("predicate") or r.get("p") or r.get("pred")
            o = r.get("object") or r.get("o") or r.get("obj")
            if s and p and o:
                out.append((self.iri_to_term(s), self.iri_to_term(p), self.iri_to_term(o)))
        return out

    # ---------- seed parsing ----------
    def parse_chemical_seed(self, raw: str) -> Dict[str, Any]:
        s = raw.strip()
        u = s.upper()
        if u.startswith("CID:"):
            cid = int(s.split(":", 1)[1])
            return {"kind": "cid", "cid": cid, "compound": f"compound:CID{cid}"}
        if u.startswith("CID") and u[3:].isdigit():
            cid = int(u[3:])
            return {"kind": "cid", "cid": cid, "compound": f"compound:CID{cid}"}
        if u.startswith("SID:"):
            sid = int(s.split(":", 1)[1])
            return {"kind": "sid", "sid": sid, "substance": f"substance:SID{sid}"}
        if u.startswith("SID") and u[3:].isdigit():
            sid = int(u[3:])
            return {"kind": "sid", "sid": sid, "substance": f"substance:SID{sid}"}
        if s.startswith("compound:") or "/compound/" in s:
            term = self.iri_to_term(s)
            m = re.search(r"CID(\d+)$", term)
            cid = int(m.group(1)) if m else None
            return {"kind": "cid", "cid": cid, "compound": term}
        if s.startswith("substance:") or "/substance/" in s:
            term = self.iri_to_term(s)
            m = re.search(r"SID(\d+)$", term)
            sid = int(m.group(1)) if m else None
            return {"kind": "sid", "sid": sid, "substance": term}
        return {"kind": "unresolved", "raw": s}

    def parse_target_seed(self, raw: str) -> Dict[str, Any]:
        s = raw.strip()
        u = s.upper()
        if u.startswith("UNIPROT:"):
            acc = s.split(":", 1)[1].strip()
            return {"kind": "protein", "uniprot": acc, "protein": f"protein:ACC{acc}"}
        if u.startswith("GENEID:"):
            gid = int(s.split(":", 1)[1])
            return {"kind": "gene", "gene_id": gid, "gene": f"gene:GID{gid}"}
        if u.startswith("SYMBOL:"):
            sym = s.split(":", 1)[1].strip()
            return {"kind": "symbol", "symbol": sym, "symbol_term": f"gene:{sym}"}
        if s.startswith("protein:") or "/protein/" in s:
            term = self.iri_to_term(s)
            m = re.search(r"ACC([^:]+)$", term)
            acc = m.group(1) if m else None
            return {"kind": "protein", "uniprot": acc, "protein": term}
        if s.startswith("gene:") or "/gene/" in s:
            term = self.iri_to_term(s)
            m = re.search(r"GID(\d+)$", term)
            if m:
                return {"kind": "gene", "gene_id": int(m.group(1)), "gene": term}
            return {"kind": "symbol", "symbol": term.split(":", 1)[1], "symbol_term": term}
        if re.fullmatch(r"[A-NR-Z][0-9][A-Z0-9]{3}[0-9](?:-\d+)?", s):
            return {"kind": "protein", "uniprot": s, "protein": f"protein:ACC{s}"}
        if s.isdigit():
            gid = int(s)
            return {"kind": "gene", "gene_id": gid, "gene": f"gene:GID{gid}"}
        return {"kind": "unresolved", "raw": s}

    # ---------- describe helpers ----------
    def describe_subject(self, graph: str, subject: str, *, limit: int = 10_000) -> List[Tuple[str, str, str]]:
        rows = self.client.query(graph=graph, subject=subject, limit=limit)
        return self._triples(rows)

    # ---------- resolvers ----------
    def resolve_symbols_to_genes(self, symbols: List[str]) -> List[str]:
        genes: List[str] = []
        for sym in symbols:
            rows = self.client.query(graph="gene", predicate="bao:BAO_0002870", object=f"gene:{sym}")
            genes.extend(self._subjects(rows))
        return sorted(set(genes))

    def resolve_genes_to_proteins(self, gene_terms: List[str]) -> List[str]:
        prots: List[str] = []
        for g in gene_terms:
            rows = self.client.query(graph="protein", predicate="up:encodedBy", object=g)
            prots.extend(self._subjects(rows))
        return sorted(set(prots))

    # ---------- one-hop getters ----------
    def measuregroups_for_participant(self, participant_term: str, *, cap: Optional[int] = None) -> List[str]:
        rows = self.client.query(graph="measuregroup", predicate="obo:RO_0000057", object=participant_term, limit=cap or 10_000)
        mgs = self._subjects(rows)
        return mgs[:cap] if cap else mgs

    def endpoints_for_measuregroup(self, mg_term: str, *, cap: Optional[int] = None) -> List[str]:
        rows = self.client.query(graph="measuregroup", subject=mg_term, predicate="obo:OBI_0000299", limit=cap or 10_000)
        eps = self._objects(rows)
        return eps[:cap] if cap else eps

    def bioassays_for_measuregroup(self, mg_term: str, *, cap: int = 5) -> List[str]:
        rows = self.client.query(graph="bioassay", predicate="bao:BAO_0000209", object=mg_term, limit=cap)
        return self._subjects(rows)

    def substance_for_endpoint(self, endpoint_term: str) -> Optional[str]:
        rows = self.client.query(graph="endpoint", subject=endpoint_term, predicate="obo:IAO_0000136", limit=10)
        objs = self._objects(rows)
        return objs[0] if objs else None

    def compound_for_substance(self, substance_term: str) -> Optional[str]:
        rows = self.client.query(graph="substance", subject=substance_term, predicate="cheminf:CHEMINF_000477", limit=10)
        objs = self._objects(rows)
        return objs[0] if objs else None

    # ---------- main entry points ----------

    def iter_intersection_evidence(self, chem_ids: List[str], target_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Case A — Both inputs provided: keep the intersection evidence only."""
        if not target_ids:
            return iter(())

        chem = [self.parse_chemical_seed(x) for x in chem_ids]
        tgt = [self.parse_target_seed(x) for x in target_ids]

        compound_terms = {c["compound"] for c in chem if c.get("compound")}

        protein_terms = {t["protein"] for t in tgt if t.get("protein")}
        gene_terms = {t["gene"] for t in tgt if t.get("gene")}
        symbols = [t["symbol"] for t in tgt if t.get("symbol")]

        if symbols:
            gene_terms.update(self.resolve_symbols_to_genes(symbols))
        if gene_terms:
            protein_terms.update(self.resolve_genes_to_proteins(sorted(gene_terms)))

        participants = sorted(set(protein_terms) | set(gene_terms))
        if not participants:
            return iter(())

        mg_cap = getattr(caps, "max_measuregroups_per_target", None)
        max_eps = getattr(caps, "max_endpoints_per_pair", None)

        seen: Dict[str, set[str]] = {
            "compound": set(),
            "substance": set(),
            "protein": set(),
            "gene": set(),
            "bioassay": set(),
            "measuregroup": set(),
            "endpoint": set(),
            "reference": set(),
            "organism": set(),
            "cellline": set(),
            "anatomy": set(),
        }

        def emit(kind: str, key: str, data: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
            if key in seen[kind]:
                return
            seen[kind].add(key)
            yield {"kind": kind, "data": data}

        # Proteins (materialize first)
        for p in sorted(protein_terms):
            acc = re.sub(r"^protein:ACC", "", p)
            triples = self.describe_subject("protein", p)
            props: Dict[str, Any] = {"protein_id": acc, "protein_term": p}
            for (_, pred, obj) in triples:
                if pred == "skos:prefLabel":
                    props.setdefault("name", obj.strip('"'))
                elif pred == "bao:BAO_0002817":
                    props["sequence"] = obj.strip('"')
                elif pred == "up:encodedBy":
                    g = self.iri_to_term(obj)
                    m = re.search(r"GID(\d+)$", g)
                    if m:
                        props["gene_id"] = m.group(1)
                        props["gene_term"] = g
                elif pred == "up:organism":
                    tax = self.iri_to_term(obj)
                    m = re.search(r"TAXID(\d+)$", tax)
                    if m:
                        props["tax_id"] = int(m.group(1))
                        props["tax_term"] = tax
            yield from emit("protein", acc, props)

        # Genes
        for g in sorted(gene_terms):
            m = re.search(r"GID(\d+)$", g)
            gid = m.group(1) if m else self._term_id(g)
            triples = self.describe_subject("gene", g)
            props: Dict[str, Any] = {"gene_id": gid, "gene_term": g}
            for (_, pred, obj) in triples:
                if pred == "bao:BAO_0002870":
                    props["symbol"] = self.iri_to_term(obj).split(":", 1)[-1]
                elif pred == "skos:prefLabel":
                    props.setdefault("name", obj.strip('"'))
            yield from emit("gene", gid, props)

        # MeasureGroups per participant
        all_mgs: List[str] = []
        for part in participants:
            all_mgs.extend(self.measuregroups_for_participant(part, cap=mg_cap))
        all_mgs = sorted(set(all_mgs))
        if not all_mgs:
            return iter(())

        for mg in all_mgs:
            mg_id = self._term_id(mg)
            yield from emit("measuregroup", mg_id, {"mg_id": mg_id, "mg_term": mg})

            # Optional context: organism/cell/anatomy
            if getattr(flags, "include_optional_context", True):
                mg_triples = self.describe_subject("measuregroup", mg)
                for (_, pred, obj) in mg_triples:
                    if pred != "obo:RO_0000057":
                        continue
                    term = self.iri_to_term(obj)
                    # Core connectivity: MeasureGroup -> Protein/Gene participants
                    if term.startswith("protein:ACC"):
                        acc = re.sub(r"^protein:ACC", "", term)
                        yield {"kind": "mg_protein", "data": {"mg_id": mg_id, "protein_id": acc}}
                    if term.startswith("gene:GID"):
                        mgid = re.search(r"GID(\d+)$", term)
                        if mgid:
                            yield {"kind": "mg_gene", "data": {"mg_id": mg_id, "gene_id": mgid.group(1)}}
                    if term.startswith("taxonomy:"):
                        m = re.search(r"TAXID(\d+)$", term)
                        if m:
                            tax_id = int(m.group(1))
                            yield from emit("organism", str(tax_id), {"tax_id": tax_id, "tax_term": term})
                            yield {"kind": "mg_organism", "data": {"mg_id": mg_id, "tax_id": tax_id}}
                    if term.startswith("cell:"):
                        cell_id = self._term_id(term)
                        yield from emit("cellline", cell_id, {"cellline_id": cell_id, "cell_term": term})
                        yield {"kind": "mg_cellline", "data": {"mg_id": mg_id, "cellline_id": cell_id}}
                        cell_triples = self.describe_subject("cell", term)
                        for (_, p2, o2) in cell_triples:
                            if p2 == "obo:RO_0001000":
                                anat = self.iri_to_term(o2)
                                anat_id = self._term_id(anat)
                                yield from emit("anatomy", anat_id, {"anatomy_id": anat_id, "anatomy_term": anat})
                                yield {"kind": "cell_anatomy", "data": {"cellline_id": cell_id, "anatomy_id": anat_id}}

            eps = self.endpoints_for_measuregroup(mg, cap=max_eps)
            for ep in eps:
                ep_id = self._term_id(ep)
                ep_triples = self.describe_subject("endpoint", ep)
                ep_props: Dict[str, Any] = {"endpoint_id": ep_id, "endpoint_term": ep, "mg_id": mg_id}
                sub_term: Optional[str] = None
                refs: List[str] = []

                for (_, pred, obj) in ep_triples:
                    if pred == "sio:SIO_000300":
                        try:
                            ep_props["value"] = float(obj.strip('"'))
                        except Exception:
                            pass
                    elif pred == "sio:SIO_000221":
                        ep_props["unit"] = self.iri_to_term(obj)
                    elif pred == "vocab:hasQualifier":
                        ep_props["qualifier"] = obj.strip('"')
                    elif pred == "vocab:PubChemAssayOutcome":
                        ep_props["outcome"] = self.iri_to_term(obj).split(":")[-1]
                    elif pred == "rdfs:label":
                        ep_props["label"] = obj.strip('"')
                    elif pred == "obo:IAO_0000136":
                        sub_term = self.iri_to_term(obj)
                    elif pred == "cito:citesAsDataSource":
                        refs.append(self.iri_to_term(obj))

                if sub_term is None:
                    sub_term = self.substance_for_endpoint(ep)
                if not sub_term:
                    continue

                cmp_term = self.compound_for_substance(sub_term)
                if not cmp_term:
                    continue
                if compound_terms and cmp_term not in compound_terms:
                    continue

                # Pre-parse CID (used for Substance -> Compound normalization edge)
                m2 = re.search(r"CID(\d+)$", cmp_term)
                cid = int(m2.group(1)) if m2 else None

                # Substance
                m = re.search(r"SID(\d+)$", sub_term)
                sid = int(m.group(1)) if m else None
                if sid is not None:
                    sub_triples = self.describe_subject("substance", sub_term)
                    sub_props: Dict[str, Any] = {"sid": sid, "substance_term": sub_term, "cid": cid}
                    for (_, pred, obj) in sub_triples:
                        if pred == "dcterms:source":
                            sub_props["source_term"] = self.iri_to_term(obj)
                    yield from emit("substance", str(sid), sub_props)
                    ep_props["sid"] = sid

                # Compound + basic features
                if cid is not None:
                    cmp_triples = self.describe_subject("compound", cmp_term)
                    cmp_props: Dict[str, Any] = {"cid": cid, "compound_term": cmp_term}
                    neighbors: List[str] = []
                    for (_, pred, obj) in cmp_triples:
                        if pred == "skos:prefLabel":
                            cmp_props["name"] = obj.strip('"')
                        elif pred == "vocab:smiles":
                            cmp_props["smiles"] = obj.strip('"')
                        elif pred == "vocab:iupac_inchi":
                            cmp_props["inchi"] = obj.strip('"')
                        elif pred == "vocab:inchikey":
                            cmp_props["inchikey"] = obj.strip('"')
                        elif pred == "vocab:molecular_formula":
                            cmp_props["formula"] = obj.strip('"')
                        elif pred == "vocab:molecular_weight":
                            try:
                                cmp_props["molecular_weight"] = float(obj.strip('"'))
                            except Exception:
                                pass
                        elif pred == "vocab:xlogp3":
                            try:
                                cmp_props["xlogp3"] = float(obj.strip('"'))
                            except Exception:
                                pass
                        elif pred == "vocab:tpsa":
                            try:
                                cmp_props["tpsa"] = float(obj.strip('"'))
                            except Exception:
                                pass
                        elif pred in ("vocab:has_parent", "cheminf:CHEMINF_000455", "cheminf:CHEMINF_000461", "cheminf:CHEMINF_000462", "cheminf:CHEMINF_000480"):
                            neighbors.append(self.iri_to_term(obj))
                    if neighbors:
                        cmp_props["neighbors"] = neighbors[:200]
                    yield from emit("compound", str(cid), cmp_props)

                # Endpoint type (best-effort) from label
                lab = (ep_props.get("label") or "").upper()
                for k in ("IC50", "KI", "KD", "EC50", "AC50"):
                    if k in lab:
                        ep_props.setdefault("type", k)
                        break

                yield from emit("endpoint", ep_id, ep_props)

                # BioAssay (for provenance)
                assays = self.bioassays_for_measuregroup(mg)
                if assays:
                    assay = assays[0]
                    ma = re.search(r"AID(\d+)$", assay)
                    if ma:
                        aid = int(ma.group(1))
                        assay_triples = self.describe_subject("bioassay", assay)
                        aprops: Dict[str, Any] = {"aid": aid, "bioassay_term": assay}
                        for (_, pred, obj) in assay_triples:
                            if pred == "dcterms:title":
                                aprops["name"] = obj.strip('"')
                            elif pred == "dcterms:source":
                                aprops["source_term"] = self.iri_to_term(obj)
                        yield from emit("bioassay", str(aid), aprops)
                        yield {"kind": "mg_bioassay", "data": {"mg_id": mg_id, "aid": aid}}

                # References (endpoint supported by)
                for ref in sorted(set(refs)):
                    rid = self._term_id(ref)
                    yield from emit("reference", rid, {"ref_id": rid, "ref_term": ref})
                    yield {"kind": "ep_reference", "data": {"endpoint_id": ep_id, "ref_id": rid}}

        return iter(())

    def iter_expand_from_targets(self, target_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Case B — Only targets provided.

        We reuse the same pipeline but without compound filtering.
        Use caps to prevent explosion.
        """
        if not target_ids:
            return iter(())
        return self.iter_intersection_evidence([], target_ids, caps=caps, flags=flags)

    def iter_expand_from_compounds(self, chem_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Case C — Only compounds provided.

        Full compound-driven expansion can become large. In this starter, it is left
        as a TODO; implement by traversing:
          Substance participates in MeasureGroup (obo:RO_0000056)
          Endpoint is about Substance (obo:IAO_0000136)
          MeasureGroup has participant Protein/Gene (obo:RO_0000057)
        """
        if not chem_ids:
            return iter(())
        return iter(())
