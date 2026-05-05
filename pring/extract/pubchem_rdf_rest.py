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
- In practice, the PubChem RDF-REST "query" endpoint is backed by a Virtuoso triple store.
  When no explicit output format is requested, it may return an HTML table (human-friendly).
- For machine consumption, request JSON via `format=json`. The JSON is SPARQL Results JSON-like.
- Some instances may still return HTML; we therefore implement a robust parser:
    JSON (preferred) -> HTML table (fallback) -> N-Triples-ish (last resort)
"""

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import hashlib
import json
import logging
import re

from html import unescape

from pring.config import RdfRestConfig
from pring.io.http import HttpClient
from pring.transform.normalizer import make_stable_id, normalize_id
from urllib.parse import quote

log = logging.getLogger("pring")


_NTRIPLE = re.compile(
    r"^\s*(<[^>]*>|[A-Za-z_][\w\-]*:[^\s]+)\s+"
    r"(<[^>]*>|[A-Za-z_][\w\-]*:[^\s]+)\s+"
    r"(.+?)\s*\.\s*$"
)


def parse_ntriples_to_rows(text: str) -> List[Dict[str, str]]:
    """Parse N-Triples-ish output into rows with subject/predicate/object.

    We keep object as a raw token (IRI/CURIE/literal). Downstream code converts
    PubChem IRIs to CURIE terms via iri_to_term().
    """
    rows: List[Dict[str, str]] = []
    if not text:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _NTRIPLE.match(line)
        if not m:
            # If Turtle slips through, we ignore it (and rely on caching + re-run).
            continue
        s, p, o = m.group(1), m.group(2), m.group(3)
        rows.append({"subject": s, "predicate": p, "object": o})
    return rows


_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r"href=\"([^\"]+)\"", re.IGNORECASE)


def _strip_tags(html: str) -> str:
    # very small tag stripper for table cells
    s = re.sub(r"<[^>]+>", "", html)
    return unescape(s).strip()


def parse_html_table_to_rows(text: str) -> List[Dict[str, str]]:
    """Parse the Virtuoso HTML table into rows.

    PubChem's RDF-REST sometimes returns an HTML table with columns such as:
      subject, predicate, object

    We extract link hrefs when present (preferred, full IRI), otherwise cell text.
    """
    if not text or "<table" not in text.lower():
        return []

    trs = _TR_RE.findall(text)
    if not trs:
        return []

    # First <tr> contains <th> headers in most responses
    headers = [_strip_tags(h) for h in _TH_RE.findall(trs[0])]
    headers = [h.lower() for h in headers if h]
    if not headers:
        return []

    rows: List[Dict[str, str]] = []
    for tr in trs[1:]:
        tds = _TD_RE.findall(tr)
        if not tds:
            continue
        vals: List[str] = []
        for cell_html in tds:
            href = _HREF_RE.search(cell_html)
            if href:
                vals.append(unescape(href.group(1)).strip())
            else:
                vals.append(_strip_tags(cell_html))
        row = {headers[i]: vals[i] for i in range(min(len(headers), len(vals)))}
        rows.append(row)
    return rows


def parse_sparql_json_to_rows(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Parse SPARQL Results JSON bindings into rows.

    Expected shapes:
      {"head":{"vars":[...]}, "results":{"bindings":[{"subject":{"type":"uri","value":"..."}}]}}
    """
    rows: List[Dict[str, str]] = []
    bindings = (((data or {}).get("results") or {}).get("bindings"))
    if not isinstance(bindings, list):
        return rows
    for b in bindings:
        if not isinstance(b, dict):
            continue
        row: Dict[str, str] = {}
        for k, v in b.items():
            if isinstance(v, dict) and "value" in v:
                row[k.lower()] = str(v["value"]).strip()
            elif isinstance(v, str):
                row[k.lower()] = v.strip()
        if row:
            rows.append(row)
    return rows


@dataclass
class PubChemRdfRestClient:
    cfg: RdfRestConfig
    cache_dir: Optional[Path] = None
    max_cache_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        headers = {
            "User-Agent": self.cfg.user_agent,
            # Prefer n-triples (easy to parse); allow turtle as fallback.
            "Accept": "application/n-triples,text/plain;q=0.9,text/turtle;q=0.8,*/*;q=0.1",
        }
        self.http = HttpClient(
            timeout_s=self.cfg.timeout_s,
            max_retries=self.cfg.max_retries,
            headers=headers,
            min_delay_s=self.cfg.min_delay_s,
            max_delay_s=self.cfg.max_delay_s,
            honor_throttling_headers=self.cfg.honor_throttling_headers,
        )
        self._cache_bytes_written = 0
        self._cache_budget_warned = False
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._cache_bytes_written = sum(p.stat().st_size for p in self.cache_dir.glob("*") if p.is_file())
            except Exception:
                self._cache_bytes_written = 0

    def close(self) -> None:
        self.http.close()


    def _maybe_write_cache(self, path: Path, payload: str) -> None:
        payload_bytes = len(payload.encode("utf-8"))
        if self.max_cache_bytes is not None and (self._cache_bytes_written + payload_bytes) > self.max_cache_bytes:
            if not self._cache_budget_warned:
                log.warning(
                    "RDF REST cache budget reached (%s bytes). Further RDF REST responses will not be cached.",
                    self.max_cache_bytes,
                )
                self._cache_budget_warned = True
            return
        try:
            path.write_text(payload, encoding="utf-8")
            self._cache_bytes_written += payload_bytes
        except Exception:
            pass

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
        format: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Run a PubChem RDF REST triple-pattern query.

        Endpoint:
          {base_url}/query

        Common params:
          graph=... (required)
          subject=..., predicate/pred=..., object/obj=...  (triple-pattern)
          limit, offset

        Returns:
          list of dicts with keys: subject, predicate, object (raw tokens)
        """
        url = self.cfg.base_url.rstrip("/") + "/query"
        params: Dict[str, Any] = {"graph": graph}

        # IMPORTANT:
        # PubChem RDF-REST may return an HTML table by default. For machine use we
        # request JSON. If the server still responds with HTML, we fall back to
        # parsing the table.
        params["format"] = format or "json"

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

        fixed_s = subject
        fixed_p = predicate or pred
        fixed_o = object or obj

        def to_triple_rows(parsed: List[Dict[str, str]]) -> List[Dict[str, str]]:
            out: List[Dict[str, str]] = []
            for r in parsed:
                s = fixed_s or r.get("subject") or r.get("s")
                p = fixed_p or r.get("predicate") or r.get("p")
                o = fixed_o or r.get("object") or r.get("o")
                if s and p and o:
                    out.append({"subject": str(s), "predicate": str(p), "object": str(o)})
            return out

        # Cache (optional)
        if self.cache_dir is not None:
            ck = self._cache_key(url, params)
            p = self.cache_dir / f"rdfrest_{graph}_{ck}.nt"
            if p.exists():
                try:
                    cached = p.read_text("utf-8", errors="ignore")
                    if cached.lstrip().startswith("{"):
                        return to_triple_rows(parse_sparql_json_to_rows(json.loads(cached)))
                    if "<table" in cached.lower():
                        return to_triple_rows(parse_html_table_to_rows(cached))
                    return to_triple_rows(parse_ntriples_to_rows(cached))
                except Exception:
                    pass

        text = self.http.get_text(url, params=params)
        if not text:
            rows: List[Dict[str, str]] = []
        else:
            t = text.lstrip()
            if t.startswith("{"):
                try:
                    rows = parse_sparql_json_to_rows(json.loads(text))
                except Exception:
                    rows = []
            elif "<table" in t.lower():
                rows = parse_html_table_to_rows(text)
            else:
                rows = parse_ntriples_to_rows(text)

        rows = to_triple_rows(rows)

        if self.cache_dir is not None:
            self._maybe_write_cache(p, text)

        return rows


@dataclass
class PubChemPugClient:
    """Minimal PubChem PUG-REST resolver for convenience seed formats.

    This is used only to resolve user-friendly inputs (InChIKey/SMILES/InChI)
    into PubChem CIDs, after which the KG is built from PubChemRDF.
    """

    base_url: str = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    cache_dir: Optional[Path] = None
    timeout_s: float = 60.0
    max_retries: int = 3
    max_cache_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        headers = {"User-Agent": "pring/0.1 (+https://example.org)", "Accept": "application/json"}
        self.http = HttpClient(timeout_s=self.timeout_s, max_retries=self.max_retries, headers=headers, cache_dir=self.cache_dir, max_cache_bytes=self.max_cache_bytes)

    def close(self) -> None:
        self.http.close()

    def _get(self, path: str) -> Dict[str, Any]:
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        return self.http.get_json(url)

    def _extract_cids(self, data: Dict[str, Any]) -> List[int]:
        # PUG JSON: {"IdentifierList":{"CID":[...]}}
        try:
            cids = (((data or {}).get("IdentifierList") or {}).get("CID"))
            if isinstance(cids, list):
                return [int(x) for x in cids if str(x).isdigit()]
        except Exception:
            pass
        return []

    def cids_from_inchikey(self, inchikey: Optional[str]) -> List[int]:
        if not inchikey:
            return []
        ik = inchikey.strip()
        data = self._get(f"compound/inchikey/{quote(ik)}/cids/JSON")
        return self._extract_cids(data)

    def cids_from_smiles(self, smiles: Optional[str]) -> List[int]:
        if not smiles:
            return []
        smi = smiles.strip()
        data = self._get(f"compound/smiles/{quote(smi, safe='')}/cids/JSON")
        return self._extract_cids(data)

    def cids_from_inchi(self, inchi: Optional[str]) -> List[int]:
        if not inchi:
            return []
        inc = inchi.strip()
        data = self._get(f"compound/inchi/{quote(inc, safe='')}/cids/JSON")
        return self._extract_cids(data)


@dataclass
class PubChemRdfRestExtractor:
    """Extract PRING graph from PubChemRDF via RDF REST triple-pattern queries."""

    client: PubChemRdfRestClient
    pug: "PubChemPugClient" = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Reuse the same base cache folder as rdf-rest when available.
        base_cache: Optional[Path] = None
        if getattr(self.client, "cache_dir", None) is not None:
            try:
                base_cache = Path(self.client.cache_dir).parent
            except Exception:
                base_cache = None
        pug_cache = (base_cache / "pugrest") if base_cache is not None else None
        self.pug = PubChemPugClient(cache_dir=pug_cache, max_cache_bytes=getattr(self.client, "max_cache_bytes", None))

    def close(self) -> None:
        try:
            self.pug.close()
        except Exception:
            pass

    _PUBCHEM_IRI_RE = re.compile(r"^https?://rdf\.ncbi\.nlm\.nih\.gov/pubchem/([^/]+)/([^/#]+)$")

    _PREFIXES = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "dcterms": "http://purl.org/dc/terms/",
        "obo": "http://purl.obolibrary.org/obo/",
        "bao": "http://www.bioassayontology.org/bao#",
        "up": "http://purl.uniprot.org/core/",
        "cito": "http://purl.org/spar/cito/",
        "vocab": "http://rdf.ncbi.nlm.nih.gov/pubchem/vocabulary#",
        # SIO and CHEMINF share the same base in the SHACL model.
        "sio": "http://semanticscience.org/resource/",
        "cheminf": "http://semanticscience.org/resource/",
    }

    # ---------- term normalization ----------
    def iri_to_term(self, value: str) -> str:
        if value is None:
            return value
        v = str(value).strip()
        # Strip N-Triples angle brackets
        if v.startswith("<") and v.endswith(">"):
            v = v[1:-1]

        m = self._PUBCHEM_IRI_RE.match(v)
        if m:
            return f"{m.group(1)}:{m.group(2)}"

        # Compact common ontology IRIs to CURIEs so downstream comparisons work
        for prefix, base in self._PREFIXES.items():
            if v.startswith(base):
                local = v[len(base):]
                if prefix in {"sio", "cheminf"}:
                    # Disambiguate shared base by local naming convention
                    if local.startswith("CHEMINF_"):
                        return f"cheminf:{local}"
                    return f"sio:{local}"
                return f"{prefix}:{local}"
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
        # Common relaxed formats
        s2 = re.sub(r"\s+", "", s)
        u2 = s2.upper()
        if s.isdigit():
            cid = int(s)
            return {"kind": "cid", "cid": cid, "compound": f"compound:CID{cid}"}
        if u2.startswith("CID=") and u2[4:].isdigit():
            cid = int(u2[4:])
            return {"kind": "cid", "cid": cid, "compound": f"compound:CID{cid}"}
        if u.startswith("CID:"):
            cid = int(s.split(":", 1)[1])
            return {"kind": "cid", "cid": cid, "compound": f"compound:CID{cid}"}
        if u.startswith("CID") and u[3:].isdigit():
            cid = int(u[3:])
            return {"kind": "cid", "cid": cid, "compound": f"compound:CID{cid}"}
        if u.startswith("SID:"):
            sid = int(s.split(":", 1)[1])
            return {"kind": "sid", "sid": sid, "substance": f"substance:SID{sid}"}
        if u2.startswith("SID=") and u2[4:].isdigit():
            sid = int(u2[4:])
            return {"kind": "sid", "sid": sid, "substance": f"substance:SID{sid}"}
        if u.startswith("SID") and u[3:].isdigit():
            sid = int(u[3:])
            return {"kind": "sid", "sid": sid, "substance": f"substance:SID{sid}"}
        # Structure-based IDs (resolved to CID via PUG-REST)
        if u.startswith("INCHIKEY:"):
            ik = s.split(":", 1)[1].strip()
            return {"kind": "inchikey", "inchikey": ik}
        if re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", u):
            return {"kind": "inchikey", "inchikey": u}
        if u.startswith("SMILES:"):
            smi = s.split(":", 1)[1].strip()
            return {"kind": "smiles", "smiles": smi}
        if u.startswith("INCHI:"):
            inc = s.split(":", 1)[1].strip()
            return {"kind": "inchi", "inchi": inc}
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
        # Convenience: treat other short alpha-numeric tokens as gene symbol
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{2,19}", s):
            return {"kind": "symbol", "symbol": s, "symbol_term": f"gene:{s}"}
        return {"kind": "unresolved", "raw": s}

    def _extract_taxid(self, taxonomy_term: str) -> Optional[int]:
        mt = re.search(r"TAXID(\d+)$", taxonomy_term)
        return int(mt.group(1)) if mt else None

    def _mg_matches_taxids(self, mg_term: str, taxids: tuple[int, ...]) -> bool:
        # Only filter out a measuregroup when taxonomy participants are present and none match.
        parts = self.objects_for("measuregroup", mg_term, "obo:RO_0000057", cap=500)
        saw_taxonomy = False
        for t in parts:
            if t.startswith("taxonomy:"):
                saw_taxonomy = True
                tid = self._extract_taxid(t)
                if tid is not None and tid in taxids:
                    return True
        return not saw_taxonomy

    def normalize_chemical_seeds(self, chem_ids: List[str]) -> List[Dict[str, Any]]:
        """Parse and resolve chemical seed IDs into CID/SID/terms.

        Supports CID/SID/IRIs as well as InChIKey/SMILES/InChI via PUG-REST.
        """
        parsed = [self.parse_chemical_seed(x) for x in chem_ids]
        out: List[Dict[str, Any]] = []
        for p in parsed:
            if p.get("compound") or p.get("substance"):
                out.append(p)
                continue
            if p.get("kind") == "inchikey":
                cids = self.pug.cids_from_inchikey(p.get("inchikey"))
                for cid in cids[:20]:
                    out.append({"kind": "cid", "cid": int(cid), "compound": f"compound:CID{int(cid)}"})
            elif p.get("kind") == "smiles":
                cids = self.pug.cids_from_smiles(p.get("smiles"))
                for cid in cids[:20]:
                    out.append({"kind": "cid", "cid": int(cid), "compound": f"compound:CID{int(cid)}"})
            elif p.get("kind") == "inchi":
                cids = self.pug.cids_from_inchi(p.get("inchi"))
                for cid in cids[:20]:
                    out.append({"kind": "cid", "cid": int(cid), "compound": f"compound:CID{int(cid)}"})
            else:
                out.append(p)
        # Deduplicate by compound/substance term
        seen = set()
        uniq: List[Dict[str, Any]] = []
        for p in out:
            key = p.get("compound") or p.get("substance") or (p.get("kind"), p.get("raw"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)
        return uniq

    # ---------- describe helpers ----------
    def describe_subject(self, graph: str, subject: str, *, limit: int = 500) -> List[Tuple[str, str, str]]:
        """Best-effort subject description.

        WARNING: For some entities (notably popular compounds), describing a
        subject without a predicate can be extremely expensive and may time out.
        Use predicate-specific getters whenever possible.
        """
        try:
            rows = self.client.query(graph=graph, subject=subject, limit=limit)
            return self._triples(rows)
        except Exception:
            return []

    def objects_for(self, graph: str, subject: str, predicate: str, *, cap: int = 50, strict: bool = True) -> List[str]:
        """Convenience: list of object terms/literals for (subject, predicate, ?o).

        Use strict=False for optional metadata lookups that should not abort a run
        when PubChem briefly returns a transient error.
        """
        try:
            rows = self.client.query(graph=graph, subject=subject, predicate=predicate, limit=cap)
            return self._objects(rows)
        except Exception:
            if strict:
                raise
            log.warning("Skipping optional RDF lookup graph=%s subject=%s predicate=%s", graph, subject, predicate, exc_info=True)
            return []

    def subjects_for(self, graph: str, predicate: str, object_term: str, *, cap: int = 50) -> List[str]:
        """Convenience: list of subject terms for (?s, predicate, object)."""
        rows = self.client.query(graph=graph, predicate=predicate, object=object_term, limit=cap)
        return self._subjects(rows)

    # ---------- resolvers ----------
    def resolve_symbols_to_genes(self, symbols: List[str], *, taxids: Optional[tuple[int, ...]] = None) -> List[str]:
        """Resolve gene symbols to PubChem gene terms.

        If taxids is provided, keep only genes whose organism matches one of
        the requested TAXIDs.
        """
        genes: List[str] = []
        for sym in symbols:
            rows = self.client.query(graph="gene", predicate="bao:BAO_0002870", object=f"gene:{sym}")
            genes.extend(self._subjects(rows))
        genes = sorted(set(genes))
        if not taxids:
            return genes

        filtered: List[str] = []
        for g in genes:
            orgs = self.objects_for("gene", g, "up:organism", cap=3)
            ok = False
            for o in orgs:
                tid = self._extract_taxid(o)
                if tid is not None and tid in taxids:
                    ok = True
                    break
            if ok:
                filtered.append(g)
        return filtered

    def resolve_genes_to_proteins(self, gene_terms: List[str], *, taxids: Optional[tuple[int, ...]] = None) -> List[str]:
        """Resolve gene terms to PubChem protein terms.

        If taxids is provided, keep only proteins whose organism matches.
        """
        prots: List[str] = []
        for g in gene_terms:
            rows = self.client.query(graph="protein", predicate="up:encodedBy", object=g)
            prots.extend(self._subjects(rows))
        prots = sorted(set(prots))
        if not taxids:
            return prots

        filtered: List[str] = []
        for p in prots:
            orgs = self.objects_for("protein", p, "up:organism", cap=2)
            ok = False
            for o in orgs:
                tid = self._extract_taxid(o)
                if tid is not None and tid in taxids:
                    ok = True
                    break
            if ok:
                filtered.append(p)
        return filtered

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
        # NOTE: PubChem RDF-REST appears to have a fixed prefix map for CURIE parsing.
        # In PubChem's SHACL model, both 'cheminf:' and 'sio:' are bound to
        #   http://semanticscience.org/resource/
        # but the REST endpoint does not reliably accept the 'cheminf:' prefix.
        # Use the equivalent CURIE under the 'sio:' prefix (same namespace).
        rows = self.client.query(graph="substance", subject=substance_term, predicate="sio:CHEMINF_000477", limit=10)
        objs = self._objects(rows)
        return objs[0] if objs else None

    def substances_for_compound(self, compound_term: str, *, cap: Optional[int] = None) -> List[str]:
        """Inverse lookup of (Substance *:CHEMINF_000477 Compound).

        PubChem's SHACL uses 'cheminf:CHEMINF_000477' for "has pubchem normalized counterpart".
        The RDF-REST endpoint is stricter about known prefixes; we use the equivalent
        'sio:CHEMINF_000477' (same namespace) for compatibility.
        """
        rows = self.client.query(
            graph="substance",
            predicate="sio:CHEMINF_000477",
            object=compound_term,
            limit=cap or 10_000,
        )
        subs = self._subjects(rows)
        return subs[:cap] if cap else subs

    def measuregroups_for_substance(self, substance_term: str, *, cap: Optional[int] = None) -> List[str]:
        """Substance participates in MeasureGroup (obo:RO_0000056)."""
        rows = self.client.query(
            graph="substance",
            subject=substance_term,
            predicate="obo:RO_0000056",
            limit=cap or 10_000,
        )
        mgs = self._objects(rows)
        return mgs[:cap] if cap else mgs

    # ---------- main entry points ----------

    def iter_intersection_evidence(self, chem_ids: List[str], target_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Case A — Both inputs provided: keep the intersection evidence only.

        Optimization note:
        The previous implementation expanded from targets (participants -> measuregroups -> endpoints)
        and then filtered by compound. This can over-retrieve massively for broad targets.

        This implementation anchors on the provided compounds first (compound -> substances -> measuregroups),
        then keeps only measuregroups whose protein/gene participants overlap the requested targets, and
        finally keeps only endpoints that map back to the requested compounds.
        """
        if not chem_ids or not target_ids:
            return iter(())

        chem = self.normalize_chemical_seeds(chem_ids)
        tgt = [self.parse_target_seed(x) for x in target_ids]

        compound_terms = {c["compound"] for c in chem if c.get("compound")}
        if not compound_terms:
            return iter(())

        protein_terms = {t["protein"] for t in tgt if t.get("protein")}
        gene_terms = {t["gene"] for t in tgt if t.get("gene")}
        symbols = [t["symbol"] for t in tgt if t.get("symbol")]

        taxids: Optional[tuple[int, ...]] = getattr(flags, "taxids", None)
        if symbols:
            gene_terms.update(self.resolve_symbols_to_genes(symbols, taxids=taxids))
        if gene_terms:
            protein_terms.update(self.resolve_genes_to_proteins(sorted(gene_terms), taxids=taxids))

        target_parts = set(protein_terms) | set(gene_terms)
        if not target_parts:
            return iter(())

        # Conservative caps for compound-first intersection.
        sub_cap = getattr(caps, "max_substances_per_compound", None) or 200
        mg_cap = getattr(caps, "max_measuregroups_per_compound", None) or 200
        max_eps = getattr(caps, "max_endpoints_per_pair", None)

        include_optional = getattr(flags, "include_optional_context", True)
        include_ep_meta = getattr(flags, "include_endpoint_metadata", True)
        include_ep_refs = getattr(flags, "include_endpoint_references", False)

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

        def materialize_protein(term: str) -> Iterator[Dict[str, Any]]:
            acc = re.sub(r"^protein:ACC", "", term)
            if acc in seen["protein"]:
                return
            triples = self.describe_subject("protein", term)
            props: Dict[str, Any] = {"protein_id": acc, "protein_term": term}
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

        def materialize_gene(term: str) -> Iterator[Dict[str, Any]]:
            m = re.search(r"GID(\d+)$", term)
            gid = m.group(1) if m else self._term_id(term)
            if gid in seen["gene"]:
                return
            triples = self.describe_subject("gene", term)
            props: Dict[str, Any] = {"gene_id": gid, "gene_term": term}
            for (_, pred, obj) in triples:
                if pred == "bao:BAO_0002870":
                    props["symbol"] = self.iri_to_term(obj).split(":", 1)[-1]
                elif pred == "skos:prefLabel":
                    props.setdefault("name", obj.strip('"'))
            yield from emit("gene", gid, props)

        # Candidate measuregroups from the compound side.
        mg_candidates: List[str] = []
        for cmp_term in sorted(compound_terms):
            subs = self.substances_for_compound(cmp_term, cap=sub_cap)
            # bounded aggregation: stop once we have enough measuregroups
            for sub in subs:
                for mg in self.measuregroups_for_substance(sub, cap=mg_cap):
                    mg_candidates.append(mg)
                    if len(set(mg_candidates)) >= mg_cap:
                        break
                if len(set(mg_candidates)) >= mg_cap:
                    break
        mg_candidates = sorted(set(mg_candidates))
        if not mg_candidates:
            return iter(())

        # Optional taxonomic restriction (applied early)
        if taxids:
            mg_candidates = [mg for mg in mg_candidates if self._mg_matches_taxids(mg, taxids)]
            if not mg_candidates:
                return iter(())

        for mg in mg_candidates:
            mg_id = self._term_id(mg)
            # Participants are needed to decide if this MG intersects with requested targets.
            mg_parts = self.objects_for("measuregroup", mg, "obo:RO_0000057", cap=500)
            mg_target_terms = [t for t in mg_parts if t.startswith("protein:ACC") or t.startswith("gene:GID")]
            overlap = set(mg_target_terms) & target_parts
            if not overlap:
                continue

            yield from emit("measuregroup", mg_id, {"mg_id": mg_id, "mg_term": mg})

            # Emit only overlapping target participants (avoid hydrating unrelated proteins/genes).
            for term in sorted(overlap):
                if term.startswith("protein:ACC"):
                    yield from materialize_protein(term)
                    acc = re.sub(r"^protein:ACC", "", term)
                    yield {"kind": "mg_protein", "data": {"mg_id": mg_id, "protein_id": acc}}
                elif term.startswith("gene:GID"):
                    yield from materialize_gene(term)
                    mgid = re.search(r"GID(\d+)$", term)
                    if mgid:
                        yield {"kind": "mg_gene", "data": {"mg_id": mg_id, "gene_id": mgid.group(1)}}

            # Optional context: organism/cell/anatomy (only for retained MGs)
            if include_optional:
                for term in mg_parts:
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
                ep_props: Dict[str, Any] = {"endpoint_id": ep_id, "endpoint_term": ep, "mg_id": mg_id}
                sub_term: Optional[str] = None
                refs: List[str] = []

                # Only hydrate expensive endpoint metadata if requested.
                if include_ep_meta or include_ep_refs:
                    ep_triples = self.describe_subject("endpoint", ep)
                    for (_, pred, obj) in ep_triples:
                        if include_ep_meta:
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
                        if pred == "obo:IAO_0000136":
                            sub_term = self.iri_to_term(obj)
                        if include_ep_refs and pred == "cito:citesAsDataSource":
                            refs.append(self.iri_to_term(obj))

                if sub_term is None:
                    sub_term = self.substance_for_endpoint(ep)
                if not sub_term:
                    continue

                cmp_term = self.compound_for_substance(sub_term)
                if not cmp_term or cmp_term not in compound_terms:
                    continue

                # Parse CID
                m2 = re.search(r"CID(\d+)$", cmp_term)
                cid = int(m2.group(1)) if m2 else None

                # Substance node
                m = re.search(r"SID(\d+)$", sub_term)
                sid = int(m.group(1)) if m else None
                if sid is not None:
                    sub_props: Dict[str, Any] = {"sid": sid, "substance_term": sub_term, "cid": cid}
                    sub_triples = self.describe_subject("substance", sub_term)
                    for (_, pred, obj) in sub_triples:
                        if pred == "dcterms:source":
                            sub_props["source_term"] = self.iri_to_term(obj)
                    yield from emit("substance", str(sid), sub_props)
                    ep_props["sid"] = sid

                # Compound node (only when actually supported by an endpoint)
                if cid is not None:
                    if str(cid) not in seen["compound"]:
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

                # Endpoint type from label
                lab = (ep_props.get("label") or "").upper()
                for k in ("IC50", "KI", "KD", "EC50", "AC50"):
                    if k in lab:
                        ep_props.setdefault("type", k)
                        break

                yield from emit("endpoint", ep_id, ep_props)

                # BioAssay provenance
                assays = self.bioassays_for_measuregroup(mg)
                if assays:
                    assay = assays[0]
                    ma = re.search(r"AID(\d+)$", assay)
                    if ma:
                        aid = int(ma.group(1))
                        if str(aid) not in seen["bioassay"]:
                            assay_triples = self.describe_subject("bioassay", assay)
                            aprops: Dict[str, Any] = {"aid": aid, "bioassay_term": assay}
                            for (_, pred, obj) in assay_triples:
                                if pred == "dcterms:title":
                                    aprops["name"] = obj.strip('"')
                                elif pred == "dcterms:source":
                                    aprops["source_term"] = self.iri_to_term(obj)
                            yield from emit("bioassay", str(aid), aprops)
                        yield {"kind": "mg_bioassay", "data": {"mg_id": mg_id, "aid": aid}}

                # References (optional)
                if include_ep_refs:
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
        # Target-only expansion remains target-anchored by definition.
        # We reuse the previous participant-first strategy but without compound filtering.
        return self._iter_target_anchored_evidence(target_ids, caps=caps, flags=flags)

    def _iter_target_anchored_evidence(self, target_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Target-anchored evidence expansion (used for expand-from-targets).

        This mirrors the older intersection strategy but does not filter by a compound set.
        """
        tgt = [self.parse_target_seed(x) for x in target_ids]
        protein_terms = {t["protein"] for t in tgt if t.get("protein")}
        gene_terms = {t["gene"] for t in tgt if t.get("gene")}
        symbols = [t["symbol"] for t in tgt if t.get("symbol")]
        taxids: Optional[tuple[int, ...]] = getattr(flags, "taxids", None)
        if symbols:
            gene_terms.update(self.resolve_symbols_to_genes(symbols, taxids=taxids))
        if gene_terms:
            protein_terms.update(self.resolve_genes_to_proteins(sorted(gene_terms), taxids=taxids))

        participants = sorted(set(protein_terms) | set(gene_terms))
        if not participants:
            return iter(())

        mg_cap = getattr(caps, "max_measuregroups_per_target", None)
        max_eps = getattr(caps, "max_endpoints_per_pair", None)
        include_optional = getattr(flags, "include_optional_context", True)
        include_ep_meta = getattr(flags, "include_endpoint_metadata", True)
        include_ep_refs = getattr(flags, "include_endpoint_references", False)

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

        # Materialize proteins/genes (same as previous behavior)
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

        all_mgs: List[str] = []
        for part in participants:
            all_mgs.extend(self.measuregroups_for_participant(part, cap=mg_cap))
        all_mgs = sorted(set(all_mgs))
        if not all_mgs:
            return iter(())
        if taxids:
            all_mgs = [mg for mg in all_mgs if self._mg_matches_taxids(mg, taxids)]
            if not all_mgs:
                return iter(())

        for mg in all_mgs:
            mg_id = self._term_id(mg)
            yield from emit("measuregroup", mg_id, {"mg_id": mg_id, "mg_term": mg})

            mg_parts = self.objects_for("measuregroup", mg, "obo:RO_0000057", cap=500)
            for term in mg_parts:
                if term.startswith("protein:ACC"):
                    acc = re.sub(r"^protein:ACC", "", term)
                    yield {"kind": "mg_protein", "data": {"mg_id": mg_id, "protein_id": acc}}
                if term.startswith("gene:GID"):
                    mgid = re.search(r"GID(\d+)$", term)
                    if mgid:
                        yield {"kind": "mg_gene", "data": {"mg_id": mg_id, "gene_id": mgid.group(1)}}

            if include_optional:
                for term in mg_parts:
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
                ep_props: Dict[str, Any] = {"endpoint_id": ep_id, "endpoint_term": ep, "mg_id": mg_id}
                sub_term: Optional[str] = None
                refs: List[str] = []

                if include_ep_meta or include_ep_refs:
                    ep_triples = self.describe_subject("endpoint", ep)
                    for (_, pred, obj) in ep_triples:
                        if include_ep_meta:
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
                        if pred == "obo:IAO_0000136":
                            sub_term = self.iri_to_term(obj)
                        if include_ep_refs and pred == "cito:citesAsDataSource":
                            refs.append(self.iri_to_term(obj))

                if sub_term is None:
                    sub_term = self.substance_for_endpoint(ep)
                if not sub_term:
                    continue

                cmp_term = self.compound_for_substance(sub_term)
                m2 = re.search(r"CID(\d+)$", cmp_term or "")
                cid = int(m2.group(1)) if m2 else None

                m = re.search(r"SID(\d+)$", sub_term)
                sid = int(m.group(1)) if m else None
                if sid is not None:
                    sub_props: Dict[str, Any] = {"sid": sid, "substance_term": sub_term, "cid": cid}
                    sub_triples = self.describe_subject("substance", sub_term)
                    for (_, pred, obj) in sub_triples:
                        if pred == "dcterms:source":
                            sub_props["source_term"] = self.iri_to_term(obj)
                    yield from emit("substance", str(sid), sub_props)
                    ep_props["sid"] = sid

                if cid is not None and cmp_term:
                    if str(cid) not in seen["compound"]:
                        cmp_triples = self.describe_subject("compound", cmp_term)
                        cmp_props: Dict[str, Any] = {"cid": cid, "compound_term": cmp_term}
                        for (_, pred, obj) in cmp_triples:
                            if pred == "skos:prefLabel":
                                cmp_props["name"] = obj.strip('"')
                        yield from emit("compound", str(cid), cmp_props)

                lab = (ep_props.get("label") or "").upper()
                for k in ("IC50", "KI", "KD", "EC50", "AC50"):
                    if k in lab:
                        ep_props.setdefault("type", k)
                        break

                yield from emit("endpoint", ep_id, ep_props)

                assays = self.bioassays_for_measuregroup(mg)
                if assays:
                    assay = assays[0]
                    ma = re.search(r"AID(\d+)$", assay)
                    if ma:
                        aid = int(ma.group(1))
                        if str(aid) not in seen["bioassay"]:
                            assay_triples = self.describe_subject("bioassay", assay)
                            aprops: Dict[str, Any] = {"aid": aid, "bioassay_term": assay}
                            for (_, pred, obj) in assay_triples:
                                if pred == "dcterms:title":
                                    aprops["name"] = obj.strip('"')
                                elif pred == "dcterms:source":
                                    aprops["source_term"] = self.iri_to_term(obj)
                            yield from emit("bioassay", str(aid), aprops)
                        yield {"kind": "mg_bioassay", "data": {"mg_id": mg_id, "aid": aid}}

                if include_ep_refs:
                    for ref in sorted(set(refs)):
                        rid = self._term_id(ref)
                        yield from emit("reference", rid, {"ref_id": rid, "ref_term": ref})
                        yield {"kind": "ep_reference", "data": {"endpoint_id": ep_id, "ref_id": rid}}

        return iter(())

    def iter_expand_from_compounds(self, chem_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Case C — Only compounds provided.

        Traversal (PubChemRDF SHACL-aligned):
          (1) Compound -> Substance(s)  via inverse of cheminf:CHEMINF_000477
          (2) Substance -> MeasureGroup via obo:RO_0000056 (participates in)
          (3) MeasureGroup -> Endpoint  via obo:OBI_0000299
          (4) Endpoint -> Substance     via obo:IAO_0000136 (is about)
          (5) MeasureGroup -> Protein/Gene/Taxonomy/Cell via obo:RO_0000057

        This can explode; we honor caps:
          - max_measuregroups_per_compound
          - max_targets_per_compound
          - max_endpoints_per_pair
        """
        if not chem_ids:
            return iter(())

        chem = self.normalize_chemical_seeds(chem_ids)

        compound_terms = [c["compound"] for c in chem if c.get("compound")]
        substance_terms_seeded = [c["substance"] for c in chem if c.get("substance")]

        # For compound-driven expansion, defaults MUST be conservative.
        # Users can override via CLI caps.
        sub_cap = getattr(caps, "max_substances_per_compound", None) or 200
        mg_cap = (
            getattr(caps, "max_measuregroups_per_compound", None)
            or getattr(caps, "max_measuregroups_per_target", None)
            or 200
        )
        max_eps = getattr(caps, "max_endpoints_per_pair", None) or 50
        max_targets = getattr(caps, "max_targets_per_compound", None) or 200

        # Dedup across the whole run
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

        # Expand each compound separately to honor per-compound caps.
        taxids: Optional[tuple[int, ...]] = getattr(flags, "taxids", None)

        for cmp_term in compound_terms:
            m = re.search(r"CID(\d+)$", cmp_term)
            cid = int(m.group(1)) if m else None

            # Substance set for this compound
            subs = set(self.substances_for_compound(cmp_term, cap=sub_cap))
            for s in substance_terms_seeded:
                c2 = self.compound_for_substance(s)
                if c2 == cmp_term:
                    subs.add(s)

            # Emit compound node (even if no evidence found)
            if cid is not None:
                cmp_props: Dict[str, Any] = {"cid": cid, "compound_term": cmp_term}

                # Avoid describing popular compounds (can time out). Fetch only a few key predicates.
                name = self.objects_for("compound", cmp_term, "skos:prefLabel", cap=3)
                if name:
                    cmp_props["name"] = str(name[0]).strip('"')
                smiles = self.objects_for("compound", cmp_term, "vocab:smiles", cap=3)
                if smiles:
                    cmp_props["smiles"] = str(smiles[0]).strip('"')
                inchikey = self.objects_for("compound", cmp_term, "vocab:inchikey", cap=3)
                if inchikey:
                    cmp_props["inchikey"] = str(inchikey[0]).strip('"')
                inchi = self.objects_for("compound", cmp_term, "vocab:iupac_inchi", cap=3)
                if inchi:
                    cmp_props["inchi"] = str(inchi[0]).strip('"')

                # A few physchem props (best-effort)
                mw = self.objects_for("compound", cmp_term, "vocab:molecular_weight", cap=2)
                if mw:
                    try:
                        cmp_props["molecular_weight"] = float(str(mw[0]).strip('"'))
                    except Exception:
                        pass
                formula = self.objects_for("compound", cmp_term, "vocab:molecular_formula", cap=2)
                if formula:
                    cmp_props["formula"] = str(formula[0]).strip('"')

                # Neighbors / parents (use sio:CHEMINF_* to avoid prefix issues)
                neighbors: List[str] = []
                for pred in ("vocab:has_parent", "sio:CHEMINF_000455", "sio:CHEMINF_000461", "sio:CHEMINF_000462", "sio:CHEMINF_000480"):
                    neighbors.extend(self.objects_for("compound", cmp_term, pred, cap=50))
                if neighbors:
                    cmp_props["neighbors"] = sorted(set(neighbors))[:200]
                yield from emit("compound", str(cid), cmp_props)

            # Traverse substances -> measuregroups
            all_mgs: List[str] = []
            for sub in sorted(subs):
                all_mgs.extend(self.measuregroups_for_substance(sub, cap=mg_cap))

                # Emit substance node + provenance
                ms = re.search(r"SID(\d+)$", sub)
                sid = int(ms.group(1)) if ms else None
                if sid is not None:
                    sub_props: Dict[str, Any] = {"sid": sid, "substance_term": sub, "cid": cid}
                    src = self.objects_for("substance", sub, "dcterms:source", cap=2)
                    if src:
                        sub_props["source_term"] = src[0]
                    yield from emit("substance", str(sid), sub_props)

            all_mgs = sorted(set(all_mgs))
            if not all_mgs:
                continue

            proteins_seen_for_compound: set[str] = set()

            for mg in all_mgs[:mg_cap]:
                # Taxonomy restriction: keep only measuregroups with matching TAXID participant.
                mg_parts_for_tax: Optional[List[str]] = None
                if taxids:
                    mg_parts_for_tax = self.objects_for("measuregroup", mg, "obo:RO_0000057", cap=500)
                    mg_tax = [t for t in mg_parts_for_tax if t.startswith("taxonomy:")]
                    if mg_tax and not any((self._extract_taxid(t) in taxids) for t in mg_tax if self._extract_taxid(t) is not None):
                        continue

                mg_id = self._term_id(mg)
                yield from emit("measuregroup", mg_id, {"mg_id": mg_id, "mg_term": mg})

                # BioAssay provenance
                assays = self.bioassays_for_measuregroup(mg)
                if assays:
                    assay = assays[0]
                    ma = re.search(r"AID(\d+)$", assay)
                    if ma:
                        aid = int(ma.group(1))
                        aprops: Dict[str, Any] = {"aid": aid, "bioassay_term": assay}
                        title = self.objects_for("bioassay", assay, "dcterms:title", cap=1)
                        if title:
                            aprops["name"] = str(title[0]).strip('"')
                        src = self.objects_for("bioassay", assay, "dcterms:source", cap=1)
                        if src:
                            aprops["source_term"] = src[0]
                        yield from emit("bioassay", str(aid), aprops)
                        yield {"kind": "mg_bioassay", "data": {"mg_id": mg_id, "aid": aid}}

                # Participants + optional context
                participants = mg_parts_for_tax if mg_parts_for_tax is not None else self.objects_for("measuregroup", mg, "obo:RO_0000057", cap=500)
                for term in participants:

                    if term.startswith("protein:ACC"):
                        acc = re.sub(r"^protein:ACC", "", term)
                        proteins_seen_for_compound.add(acc)
                        yield {"kind": "mg_protein", "data": {"mg_id": mg_id, "protein_id": acc}}

                        if max_targets and len(proteins_seen_for_compound) > max_targets:
                            break

                        # Emit protein node once
                        if acc not in seen["protein"]:
                            props: Dict[str, Any] = {"protein_id": acc, "protein_term": term}
                            nm = self.objects_for("protein", term, "skos:prefLabel", cap=2)
                            if nm:
                                props.setdefault("name", str(nm[0]).strip('"'))
                            seq = self.objects_for("protein", term, "bao:BAO_0002817", cap=1)
                            if seq:
                                props["sequence"] = str(seq[0]).strip('"')
                            enc = self.objects_for("protein", term, "up:encodedBy", cap=5)
                            if enc:
                                g = enc[0]
                                mgid = re.search(r"GID(\d+)$", g)
                                if mgid:
                                    props["gene_id"] = mgid.group(1)
                                    props["gene_term"] = g
                            org = self.objects_for("protein", term, "up:organism", cap=2)
                            if org:
                                tax = org[0]
                                mt = re.search(r"TAXID(\d+)$", tax)
                                if mt:
                                    props["tax_id"] = int(mt.group(1))
                                    props["tax_term"] = tax
                            if taxids and props.get("tax_id") is not None and int(props["tax_id"]) not in taxids:
                                # Safety: if protein has a tax_id and doesn't match, skip it
                                continue
                            yield from emit("protein", acc, props)

                    elif getattr(flags, "include_optional_context", True) and term.startswith("taxonomy:"):
                        mt = re.search(r"TAXID(\d+)$", term)
                        if mt:
                            tax_id = int(mt.group(1))
                            yield from emit("organism", str(tax_id), {"tax_id": tax_id, "tax_term": term})
                            yield {"kind": "mg_organism", "data": {"mg_id": mg_id, "tax_id": tax_id}}

                    elif getattr(flags, "include_optional_context", True) and term.startswith("cell:"):
                        cell_id = self._term_id(term)
                        yield from emit("cellline", cell_id, {"cellline_id": cell_id, "cell_term": term})
                        yield {"kind": "mg_cellline", "data": {"mg_id": mg_id, "cellline_id": cell_id}}
                        anats = self.objects_for("cell", term, "obo:RO_0001000", cap=5, strict=False)
                        for anat in anats:
                            anat_id = self._term_id(anat)
                            yield from emit("anatomy", anat_id, {"anatomy_id": anat_id, "anatomy_term": anat})
                            yield {"kind": "cell_anatomy", "data": {"cellline_id": cell_id, "anatomy_id": anat_id}}

                # Endpoints
                eps = self.endpoints_for_measuregroup(mg, cap=max_eps)
                for ep in eps:
                    ep_id = self._term_id(ep)
                    ep_props: Dict[str, Any] = {"endpoint_id": ep_id, "endpoint_term": ep, "mg_id": mg_id}
                    refs: List[str] = []
                    sub_term: Optional[str] = None

                    include_ep_meta = getattr(flags, "include_endpoint_metadata", True)
                    include_ep_refs = getattr(flags, "include_endpoint_references", False) and getattr(flags, "include_optional_context", True)

                    # Pull only what we need. Endpoint references are opt-in because they
                    # add a large number of optional PubChem requests and are the most
                    # common trigger for throttling on bigger runs.
                    if include_ep_meta:
                        lab = self.objects_for("endpoint", ep, "rdfs:label", cap=1, strict=False)
                        if lab:
                            ep_props["label"] = str(lab[0]).strip('"')
                        val = self.objects_for("endpoint", ep, "sio:SIO_000300", cap=1, strict=False)
                        if val:
                            try:
                                ep_props["value"] = float(str(val[0]).strip('"'))
                            except Exception:
                                pass
                        unit = self.objects_for("endpoint", ep, "sio:SIO_000221", cap=1, strict=False)
                        if unit:
                            ep_props["unit"] = unit[0]
                        qual = self.objects_for("endpoint", ep, "vocab:hasQualifier", cap=1, strict=False)
                        if qual:
                            ep_props["qualifier"] = str(qual[0]).strip('"')
                        outc = self.objects_for("endpoint", ep, "vocab:PubChemAssayOutcome", cap=1, strict=False)
                        if outc:
                            ep_props["outcome"] = outc[0].split(":")[-1]
                    about = self.objects_for("endpoint", ep, "obo:IAO_0000136", cap=1, strict=False)
                    if about:
                        sub_term = about[0]
                    # References
                    if include_ep_refs:
                        refs = self.objects_for("endpoint", ep, "cito:citesAsDataSource", cap=10, strict=False)

                    if not sub_term:
                        sub_term = self.substance_for_endpoint(ep)
                    if not sub_term:
                        continue
                    cmp2 = self.compound_for_substance(sub_term)
                    if cmp2 != cmp_term:
                        continue

                    ms = re.search(r"SID(\d+)$", sub_term)
                    if ms:
                        ep_props["sid"] = int(ms.group(1))

                    lab = (ep_props.get("label") or "").upper()
                    for k in ("IC50", "KI", "KD", "EC50", "AC50"):
                        if k in lab:
                            ep_props.setdefault("type", k)
                            break

                    yield from emit("endpoint", ep_id, ep_props)

                    for ref in sorted(set(refs)):
                        rid = self._term_id(ref)
                        yield from emit("reference", rid, {"ref_id": rid, "ref_term": ref})
                        yield {"kind": "ep_reference", "data": {"endpoint_id": ep_id, "ref_id": rid}}

        return iter(())
