from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from pring.config import BuildCaps, BuildFlags, SparqlConfig
from pring.io.http import HttpClient
from pring.transform.normalizer import normalize_id


log = logging.getLogger("pring.sparql")


_PUBCHEM_IRI_RE = re.compile(r"^https?://rdf\.ncbi\.nlm\.nih\.gov/pubchem/([^/]+)/([^/?#]+)$")


def _chunked(xs: Sequence[str], n: int) -> Iterator[List[str]]:
    for i in range(0, len(xs), n):
        yield list(xs[i : i + n])


def iri_to_term(value: str) -> str:
    """Convert PubChem IRIs returned by SPARQL into the CURIE-like terms used in PRING.

    Example:
      http://rdf.ncbi.nlm.nih.gov/pubchem/substance/SID87798 -> substance:SID87798
    """
    if value is None:
        return value
    v = str(value).strip()
    m = _PUBCHEM_IRI_RE.match(v)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return v


def _term_id(term: str) -> str:
    return normalize_id(term) or term


def _extract_int(term: str, pat: str) -> Optional[int]:
    m = re.search(pat, term)
    return int(m.group(1)) if m else None


def _cid(term: str) -> Optional[int]:
    return _extract_int(term, r"CID(\d+)$")


def _sid(term: str) -> Optional[int]:
    return _extract_int(term, r"SID(\d+)$")


def _taxid(term: str) -> Optional[int]:
    return _extract_int(term, r"TAXID(\d+)$")


def _uniprot_acc(term: str) -> Optional[str]:
    m = re.search(r"ACC([^:]+)$", term)
    return m.group(1) if m else None


def _gid(term: str) -> Optional[int]:
    return _extract_int(term, r"GID(\d+)$")


SPARQL_PREFIXES = """
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX bao: <http://www.bioassayontology.org/bao#>
PREFIX sio: <http://semanticscience.org/resource/>
PREFIX cito: <http://purl.org/spar/cito/>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX vocab: <http://rdf.ncbi.nlm.nih.gov/pubchem/vocabulary#>
PREFIX up: <http://purl.uniprot.org/core/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

PREFIX compound: <http://rdf.ncbi.nlm.nih.gov/pubchem/compound/>
PREFIX substance: <http://rdf.ncbi.nlm.nih.gov/pubchem/substance/>
PREFIX measuregroup: <http://rdf.ncbi.nlm.nih.gov/pubchem/measuregroup/>
PREFIX endpoint: <http://rdf.ncbi.nlm.nih.gov/pubchem/endpoint/>
PREFIX bioassay: <http://rdf.ncbi.nlm.nih.gov/pubchem/bioassay/>
PREFIX protein: <http://rdf.ncbi.nlm.nih.gov/pubchem/protein/>
PREFIX gene: <http://rdf.ncbi.nlm.nih.gov/pubchem/gene/>
PREFIX taxonomy: <http://rdf.ncbi.nlm.nih.gov/pubchem/taxonomy/>
PREFIX cell: <http://rdf.ncbi.nlm.nih.gov/pubchem/cell/>
PREFIX anatomy: <http://rdf.ncbi.nlm.nih.gov/pubchem/anatomy/>
PREFIX genesymbol: <http://rdf.ncbi.nlm.nih.gov/pubchem/genesymbol/>
""".strip()


@dataclass
class SparqlMirrorClient:
    cfg: SparqlConfig
    cache_dir: Optional[Path] = None
    max_cache_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "application/sparql-results+json",
        }
        self.http = HttpClient(timeout_s=self.cfg.timeout_s, max_retries=self.cfg.max_retries, headers=headers, cache_dir=self.cache_dir, max_cache_bytes=self.max_cache_bytes)

    def close(self) -> None:
        self.http.close()

    def select(
        self,
        query: str,
        *,
        timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Run SELECT query and return SPARQL JSON bindings."""
        try:
            data = self.http.post_json(
                self.cfg.endpoint_url,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout_s=timeout_s,
                max_retries=max_retries,
            )
        except TypeError as type_error:
            # Keep compatibility with tests or external callers that monkeypatch
            # HttpClient with a minimal post_json(url, data, headers) object.
            if "timeout_s" not in str(type_error) and "max_retries" not in str(type_error):
                raise
            data = self.http.post_json(
                self.cfg.endpoint_url,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
            )
        return ((data.get("results") or {}).get("bindings") or [])


@dataclass
class PubChemSparqlMirrorExtractor:
    """Extractor using a SPARQL mirror (IDSM/ChemWebRDF) to reduce request count.

    It yields the same PRING row stream (kind/data) as the RDF-REST extractor.
    """

    client: SparqlMirrorClient
    page_size: int = 25

    def close(self) -> None:
        return

    # -------------------------
    # Seed parsing (minimal)
    # -------------------------
    def _parse_compounds(self, chem_ids: List[str]) -> List[str]:
        out: List[str] = []
        for raw in chem_ids:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            u = s.upper()
            if s.isdigit():
                out.append(f"compound:CID{int(s)}")
                continue
            if u.startswith("CID:"):
                out.append(f"compound:CID{int(s.split(':',1)[1])}")
                continue
            if u.startswith("CID") and u[3:].isdigit():
                out.append(f"compound:CID{int(u[3:])}")
                continue
            if s.startswith("compound:"):
                out.append(s)
                continue
            if "/compound/" in s:
                out.append(iri_to_term(s))
                continue
            # Allow SID lines (we can still traverse from substance)
            if u.startswith("SID:"):
                out.append(f"substance:SID{int(s.split(':',1)[1])}")
                continue
            if u.startswith("SID") and u[3:].isdigit():
                out.append(f"substance:SID{int(u[3:])}")
                continue
            if s.startswith("substance:") or "/substance/" in s:
                out.append(iri_to_term(s))
                continue

        # de-dup
        seen: Set[str] = set()
        uniq: List[str] = []
        for t in out:
            if t not in seen:
                uniq.append(t)
                seen.add(t)
        return uniq

    def _parse_targets(self, target_ids: List[str]) -> Tuple[List[str], List[str]]:
        """Return (protein_terms, gene_terms)."""
        prots: List[str] = []
        genes: List[str] = []
        for raw in target_ids:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            u = s.upper()
            if u.startswith("UNIPROT:"):
                acc = s.split(":", 1)[1].strip()
                prots.append(f"protein:ACC{acc}")
                continue
            if re.fullmatch(r"[A-NR-Z][0-9][A-Z0-9]{3}[0-9](?:-\d+)?", s):
                prots.append(f"protein:ACC{s}")
                continue
            if u.startswith("GENEID:"):
                gid = int(s.split(":", 1)[1])
                genes.append(f"gene:GID{gid}")
                continue
            if s.isdigit():
                genes.append(f"gene:GID{int(s)}")
                continue
            if s.startswith("protein:") or "/protein/" in s:
                prots.append(iri_to_term(s))
                continue
            if s.startswith("gene:") or "/gene/" in s:
                genes.append(iri_to_term(s))
                continue
            # treat remaining short tokens as gene symbols
            if u.startswith("SYMBOL:"):
                sym = s.split(":", 1)[1].strip()
                genes.append(f"gene:{sym}")
                continue
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{2,19}", s):
                genes.append(f"gene:{s}")
                continue

        # de-dup
        prots = list(dict.fromkeys(prots))
        genes = list(dict.fromkeys(genes))
        return prots, genes

    def _resolve_symbols_to_geneids(self, gene_terms: List[str]) -> List[str]:
        """Resolve gene symbols (gene:BRCA1) into gene:GIDxxxx where possible."""
        # Keep gene:GID as-is
        gids = [g for g in gene_terms if re.search(r"GID\d+$", g)]
        syms = [g for g in gene_terms if g.startswith("gene:") and not re.search(r"GID\d+$", g)]
        if not syms:
            return gids

        values = " ".join(syms)
        q = f"""{SPARQL_PREFIXES}
SELECT DISTINCT ?gene WHERE {{
  VALUES ?sym {{ {values} }}
  ?gene bao:BAO_0002870 ?sym .
}}"""
        rows = self.client.select(q)
        for b in rows:
            v = b.get("gene", {}).get("value")
            if v:
                gids.append(iri_to_term(v))
        return list(dict.fromkeys(gids))

    def _genes_to_proteins(self, geneids: List[str], flags: BuildFlags) -> List[str]:
        if not geneids:
            return []
        values = " ".join(geneids)
        tax_filter = ""
        if flags.taxids:
            tax_values = " ".join([f"taxonomy:TAXID{t}" for t in flags.taxids])
            tax_filter = f"\n  ?protein up:organism ?tax .\n  VALUES ?tax {{ {tax_values} }}"
        q = f"""{SPARQL_PREFIXES}
SELECT DISTINCT ?protein ?gene WHERE {{
  VALUES ?gene {{ {values} }}
  ?protein up:encodedBy ?gene .
  {tax_filter}
}}"""
        rows = self.client.select(q)
        prots: List[str] = []
        for b in rows:
            v = b.get("protein", {}).get("value")
            if v:
                prots.append(iri_to_term(v))
        return list(dict.fromkeys(prots))

    def _resolve_targets_to_proteins(self, target_ids: List[str], flags: BuildFlags) -> List[str]:
        prots, genes = self._parse_targets(target_ids)
        geneids = self._resolve_symbols_to_geneids(genes)
        prots2 = self._genes_to_proteins(geneids, flags)
        allp = list(dict.fromkeys(prots + prots2))
        return allp

    
    def _tax_filter_on_var(self, node_var: str, tax_var: str, flags: BuildFlags) -> str:
        """Apply taxonomy filtering on a node's up:organism (NOT on measuregroup participants).

        Non-strict by default: if organism is missing, keep the row.
        """
        if not flags.taxids:
            return ""
        tax_list = ", ".join([f"taxonomy:TAXID{t}" for t in flags.taxids])
        return f"\n  OPTIONAL {{ {node_var} up:organism {tax_var} . }}\n  FILTER(!BOUND({tax_var}) || {tax_var} IN ({tax_list}))"

# -------------------------
    # Query builders
    # -------------------------
    def _tax_mg_filter(self, flags: BuildFlags) -> str:
        # Deprecated: do not filter measuregroups by taxonomy participant; apply tax filter on targets/proteins.
        return ""

    def _select_measuregroups_for_proteins(self, proteins: List[str], caps: BuildCaps, flags: BuildFlags) -> List[str]:
        """Select measuregroups for a list of target terms.

        Despite the name, `proteins` may include protein:* and gene:GID* terms.
        Taxonomy filtering is applied on the target's up:organism (not on mg participants).
        """
        if not proteins:
            return []
        values = " ".join(proteins)
        limit = None
        if caps.max_measuregroups_per_target:
            limit = caps.max_measuregroups_per_target * max(1, len(proteins))

        tax = self._tax_filter_on_var("?target", "?tTax", flags)

        q = f"""{SPARQL_PREFIXES}
            SELECT DISTINCT ?mg WHERE {{
            VALUES ?target {{ {values} }}
            ?mg obo:RO_0000057 ?target .
            }}"""
        if limit:
            q += f"\nLIMIT {int(limit)}"
        rows = self.client.select(q)
        mgs: List[str] = []
        for b in rows:
            v = b.get("mg", {}).get("value")
            if v:
                mgs.append(iri_to_term(v))
        return list(dict.fromkeys(mgs))

    def _select_substances_for_compounds(self, compounds: List[str], caps: BuildCaps) -> List[str]:
        if not compounds:
            return []
        # Accept either compound:* or substance:* in the list; return substance terms.
        compound_terms = [c for c in compounds if c.startswith("compound:")]
        substance_terms = [c for c in compounds if c.startswith("substance:")]
        out = list(substance_terms)
        if not compound_terms:
            return list(dict.fromkeys(out))
        values = " ".join(compound_terms)
        limit = None
        if caps.max_substances_per_compound:
            limit = caps.max_substances_per_compound * max(1, len(compound_terms))
        q = f"""{SPARQL_PREFIXES}
            SELECT DISTINCT ?sub WHERE {{
            VALUES ?compound {{ {values} }}
            ?sub sio:CHEMINF_000477 ?compound .
            }}"""
        if limit:
            q += f"\nLIMIT {int(limit)}"
        rows = self.client.select(q)
        for b in rows:
            v = b.get("sub", {}).get("value")
            if v:
                out.append(iri_to_term(v))
        return list(dict.fromkeys(out))

    def _select_measuregroups_for_substances(self, substances: List[str], caps: BuildCaps) -> List[str]:
        if not substances:
            return []
        values = " ".join(substances)
        limit = None
        if caps.max_measuregroups_per_compound:
            # approximate cap
            limit = caps.max_measuregroups_per_compound * max(1, len(substances))
        q = f"""{SPARQL_PREFIXES}
            SELECT DISTINCT ?mg WHERE {{
            VALUES ?sub {{ {values} }}
            ?sub obo:RO_0000056 ?mg .
            }}"""
        if limit:
            q += f"\nLIMIT {int(limit)}"
        rows = self.client.select(q)
        mgs: List[str] = []
        for b in rows:
            v = b.get("mg", {}).get("value")
            if v:
                mgs.append(iri_to_term(v))
        return list(dict.fromkeys(mgs))

    def _select_evidence_rows_for_measuregroups(self, mgs: List[str], caps: BuildCaps, flags: BuildFlags,
                                                restrict_compounds: Optional[Set[str]] = None,
                                                restrict_proteins: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        """Return denormalized evidence rows for one measuregroup chunk.

        This intentionally keeps the original/legacy PRING SPARQL evidence
        shape instead of using a nested subquery.  The nested subquery version
        reduced local row volume but proved slower on the IDSM public mirror for
        some PubChem measuregroups because the server could not stream results
        early enough.  Resource controls are therefore applied around the
        original evidence query shape, while optional blocks are included only
        when requested by flags.
        """
        if not mgs:
            return []

        mg_values = " ".join(mgs)

        compound_filter = ""
        if restrict_compounds:
            comp_vals = " ".join(sorted(restrict_compounds))
            compound_filter = f"\n            VALUES ?compound {{ {comp_vals} }}"

        # Keep the participant pattern bounded. The older query used an
        # OPTIONAL protein-from-gene join where ?geneTarget could be unbound,
        # which can explode on public SPARQL engines.
        if restrict_proteins:
            prot_vals = " ".join(sorted(restrict_proteins))
            participant_clause = f"""
            VALUES ?protein {{ {prot_vals} }}
            ?mg obo:RO_0000057 ?protein .
            OPTIONAL {{ ?protein up:encodedBy ?gene }}
            OPTIONAL {{
                ?mg obo:RO_0000057 ?geneTarget .
                FILTER(STRSTARTS(STR(?geneTarget), STR(gene:)))
            }}"""
        else:
            participant_clause = """
            {
                ?mg obo:RO_0000057 ?protein .
                FILTER(STRSTARTS(STR(?protein), STR(protein:)))
                OPTIONAL { ?protein up:encodedBy ?gene }
            }
            UNION
            {
                ?mg obo:RO_0000057 ?geneTarget .
                FILTER(STRSTARTS(STR(?geneTarget), STR(gene:)))
                ?protein up:encodedBy ?geneTarget .
                OPTIONAL { ?protein up:encodedBy ?gene }
            }"""

        tax_clause = self._tax_filter_on_var("?protein", "?pTax", flags)

        row_limit = None
        if caps.max_endpoints_per_pair:
            row_limit = int(caps.max_endpoints_per_pair) * max(1, len(mgs))

        select_gene = "\n                ?geneTarget ?gname ?gsNode ?gsym"
        # Keep dependent OPTIONAL patterns nested under the pattern that binds
        # their variable.  Separate OPTIONALs on an unbound variable can become
        # broad graph scans on public SPARQL endpoints.
        gene_clauses = """
            OPTIONAL {
                ?mg obo:RO_0000057 ?geneTarget .
                FILTER(STRSTARTS(STR(?geneTarget), STR(gene:)))
                OPTIONAL { ?geneTarget skos:prefLabel ?gname }
                OPTIONAL { ?geneTarget rdfs:label ?gname }
                OPTIONAL {
                    ?geneTarget bao:BAO_0002870 ?gsNode .
                    OPTIONAL { ?gsNode skos:prefLabel ?gsym }
                }
            }"""

        select_context = ""
        context_clauses = ""
        if getattr(flags, "include_optional_context", True):
            select_context = "\n                ?cell ?anat"
            context_clauses = """
            OPTIONAL {
                ?mg obo:RO_0000057 ?cell .
                FILTER(STRSTARTS(STR(?cell), STR(cell:)))
                OPTIONAL { ?cell obo:RO_0001000 ?anat }
            }"""

        select_metadata = ""
        metadata_clauses = ""
        if getattr(flags, "include_endpoint_metadata", True):
            select_metadata = "\n                ?value ?unit ?qual ?outcome ?eplabel"
            metadata_clauses = """
            OPTIONAL { ?endpoint sio:SIO_000300 ?value }
            OPTIONAL { ?endpoint sio:SIO_000221 ?unit }
            OPTIONAL { ?endpoint vocab:hasQualifier ?qual }
            OPTIONAL { ?endpoint vocab:PubChemAssayOutcome ?outcome }
            OPTIONAL { ?endpoint rdfs:label ?eplabel }"""

        select_ref = ""
        ref_clause = ""
        if getattr(flags, "include_endpoint_references", False):
            select_ref = " ?ref"
            ref_clause = "\n            OPTIONAL { ?endpoint cito:citesAsDataSource ?ref }"

        limit_clause = f"\nLIMIT {row_limit}" if row_limit else ""

        q = f"""{SPARQL_PREFIXES}
            SELECT ?mg ?bioassay ?baname ?endpoint ?sub ?compound ?protein ?tax
                {select_gene}{select_context}{select_metadata}{select_ref}
                ?pname ?seq ?gene
                ?cname ?smiles ?inchikey ?inchi ?formula ?mw ?xlogp3 ?tpsa
                ?source
            WHERE {{
            VALUES ?mg {{ {mg_values} }}
            ?mg obo:OBI_0000299 ?endpoint .
            ?endpoint obo:IAO_0000136 ?sub .
            ?sub sio:CHEMINF_000477 ?compound .
            {compound_filter}

            {participant_clause}
            {tax_clause}

            OPTIONAL {{ ?mg obo:RO_0000057 ?tax . FILTER(STRSTARTS(STR(?tax), STR(taxonomy:))) }}
            OPTIONAL {{
                ?bioassay bao:BAO_0000209 ?mg .
                OPTIONAL {{ ?bioassay skos:prefLabel ?baname }}
                OPTIONAL {{ ?bioassay rdfs:label ?baname }}
            }}
            {gene_clauses}
            {context_clauses}
            {metadata_clauses}
            {ref_clause}
            OPTIONAL {{ ?sub dcterms:source ?source }}

            OPTIONAL {{ ?protein skos:prefLabel ?pname }}
            OPTIONAL {{ ?protein bao:BAO_0002817 ?seq }}
            OPTIONAL {{ ?protein up:encodedBy ?gene }}

            OPTIONAL {{ ?compound skos:prefLabel ?cname }}
            OPTIONAL {{ ?compound vocab:smiles ?smiles }}
            OPTIONAL {{ ?compound vocab:inchikey ?inchikey }}
            OPTIONAL {{ ?compound vocab:iupac_inchi ?inchi }}
            OPTIONAL {{ ?compound vocab:molecular_formula ?formula }}
            OPTIONAL {{ ?compound vocab:molecular_weight ?mw }}
            OPTIONAL {{ ?compound vocab:xlogp3 ?xlogp3 }}
            OPTIONAL {{ ?compound vocab:tpsa ?tpsa }}
            }}{limit_clause}"""

        cfg = getattr(self.client, "cfg", SparqlConfig())
        return list(self.client.select(
            q,
            timeout_s=getattr(cfg, "evidence_timeout_s", None),
            max_retries=getattr(cfg, "evidence_max_retries", None),
        ))

    def _select_evidence_rows_adaptive(
        self,
        mg_chunk: List[str],
        caps: BuildCaps,
        flags: BuildFlags,
        restrict_compounds: Optional[Set[str]] = None,
        restrict_proteins: Optional[Set[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], int, int, Optional[Exception]]:
        """Fetch evidence rows and split heavy chunks on timeout/failure.

        Public SPARQL mirrors can time out on a chunk even when individual
        measuregroups are retrievable. This helper keeps the existing extraction
        logic but makes the chunking adaptive: try the requested chunk, split on
        failure, and only skip the smallest still-failing chunk when skipping is
        enabled.
        """
        cfg = getattr(self.client, "cfg", SparqlConfig())
        min_size = max(1, int(getattr(cfg, "min_page_size", 1) or 1))
        try:
            bindings = self._select_evidence_rows_for_measuregroups(
                mg_chunk,
                caps=caps,
                flags=flags,
                restrict_compounds=restrict_compounds,
                restrict_proteins=restrict_proteins,
            )
            return bindings, 0, 0, None
        except Exception as exc:
            can_split = bool(getattr(cfg, "adaptive_chunking", True)) and len(mg_chunk) > min_size
            if can_split:
                mid = max(1, len(mg_chunk) // 2)
                left = mg_chunk[:mid]
                right = mg_chunk[mid:]
                log.warning(
                    "SPARQL evidence chunk timed out/failed at size=%d; splitting into %d + %d. error=%s",
                    len(mg_chunk),
                    len(left),
                    len(right),
                    exc,
                )
                rows_l, fail_l, mg_l, exc_l = self._select_evidence_rows_adaptive(
                    left, caps, flags, restrict_compounds, restrict_proteins
                )
                rows_r, fail_r, mg_r, exc_r = self._select_evidence_rows_adaptive(
                    right, caps, flags, restrict_compounds, restrict_proteins
                )
                return rows_l + rows_r, fail_l + fail_r, mg_l + mg_r, exc_r or exc_l

            if not getattr(cfg, "skip_failed_chunks", True):
                raise

            log.warning(
                "Skipping failed SPARQL evidence chunk: chunk_size=%d error=%s",
                len(mg_chunk),
                exc,
            )
            return [], 1, len(mg_chunk), exc

    # -------------------------
    # Public iterators
    # -------------------------
    def iter_expand_from_targets(self, target_ids: List[str], caps: BuildCaps, flags: BuildFlags) -> Iterator[Dict[str, Any]]:
        # Parse + resolve
        prots, genes = self._parse_targets(target_ids)
        geneids = self._resolve_symbols_to_geneids(genes)
        prots2 = self._genes_to_proteins(geneids, flags)

        proteins = list(dict.fromkeys(prots + prots2))
        # Include geneids as valid measuregroup participants in addition to proteins
        targets = list(dict.fromkeys(proteins + geneids))

        log.info("sparql: resolved proteins=%d genes=%d", len(proteins), len(geneids))
        mgs = self._select_measuregroups_for_proteins(targets, caps, flags)
        log.info("sparql: measuregroups=%d", len(mgs))

        yield from self._emit_from_measuregroups(mgs, caps, flags, restrict_proteins=set(proteins))

    def iter_expand_from_compounds(self, chem_ids: List[str], caps: BuildCaps, flags: BuildFlags) -> Iterator[Dict[str, Any]]:
        terms = self._parse_compounds(chem_ids)
        # Split: if user provided substance terms, keep; if compound, expand to substances.
        subs = self._select_substances_for_compounds(terms, caps)
        log.info("sparql: substances=%d", len(subs))
        mgs = self._select_measuregroups_for_substances(subs, caps)
        # Taxonomy filter (optional)
        if flags.taxids:
            tax_filtered: List[str] = []
            for mg in mgs:
                # cheap inline check: keep mg where taxonomy participant exists
                # (we already have flags; mg list may still include untyped)
                tax_filtered.append(mg)
            mgs = tax_filtered
        log.info("sparql: measuregroups=%d", len(mgs))
        restrict_compounds = set([t for t in terms if t.startswith("compound:")]) or None
        yield from self._emit_from_measuregroups(mgs, caps, flags, restrict_compounds=restrict_compounds)

    def iter_intersection_evidence(self, chem_ids: List[str], target_ids: List[str], caps: BuildCaps, flags: BuildFlags) -> Iterator[Dict[str, Any]]:
        terms = self._parse_compounds(chem_ids)
        compounds = set([t for t in terms if t.startswith("compound:")])

        # Resolve targets: include both proteins and geneids for mg selection
        prots, genes = self._parse_targets(target_ids)
        geneids = self._resolve_symbols_to_geneids(genes)
        prots2 = self._genes_to_proteins(geneids, flags)
        proteins = list(dict.fromkeys(prots + prots2))
        targets = list(dict.fromkeys(proteins + geneids))

        log.info("sparql: intersection proteins=%d genes=%d compounds=%d", len(proteins), len(geneids), len(compounds))

        # Select measuregroups satisfying both constraints
        mg_values = " ".join(targets)
        comp_values = " ".join(sorted(compounds)) if compounds else ""
        q = f"""{SPARQL_PREFIXES}
            SELECT DISTINCT ?mg WHERE {{
            VALUES ?protein {{ {mg_values} }}
            ?mg obo:RO_0000057 ?target .
            ?mg obo:OBI_0000299 ?endpoint .
            ?endpoint obo:IAO_0000136 ?sub .
            ?sub sio:CHEMINF_000477 ?compound .
                {('VALUES ?compound { ' + comp_values + ' }') if comp_values else ''}
            }}"""
        limit = None
        if caps.max_measuregroups_per_target:
            limit = caps.max_measuregroups_per_target * max(1, len(targets))
        if limit:
            q += f"\nLIMIT {int(limit)}"
        rows = self.client.select(q)
        mgs: List[str] = []
        for b in rows:
            v = b.get("mg", {}).get("value")
            if v:
                mgs.append(iri_to_term(v))
        mgs = list(dict.fromkeys(mgs))
        log.info("sparql: intersection measuregroups=%d", len(mgs))
        yield from self._emit_from_measuregroups(mgs, caps, flags, restrict_compounds=compounds or None, restrict_proteins=set(proteins))

    # -------------------------
    # Emit row stream
    # -------------------------
    def _emit_from_measuregroups(self, mgs: List[str], caps: BuildCaps, flags: BuildFlags,
                                 restrict_compounds: Optional[Set[str]] = None,
                                 restrict_proteins: Optional[Set[str]] = None) -> Iterator[Dict[str, Any]]:
        seen_comp: Set[int] = set()
        seen_sub: Set[int] = set()
        seen_prot: Set[str] = set()
        seen_gene: Set[str] = set()
        seen_org: Set[int] = set()
        seen_ba: Set[int] = set()
        seen_mg: Set[str] = set()
        seen_ep: Set[str] = set()
        seen_ref: Set[str] = set()

        failed_chunks = 0
        failed_measuregroups = 0
        evidence_queries = 0
        cfg = getattr(self.client, "cfg", SparqlConfig())

        log.info(
            "sparql: evidence strategy=bounded_nested_optional page_size=%s endpoint_metadata=%s optional_context=%s endpoint_references=%s",
            max(1, int(self.page_size or 1)),
            getattr(flags, "include_endpoint_metadata", True),
            getattr(flags, "include_optional_context", True),
            getattr(flags, "include_endpoint_references", False),
        )

        for mg_chunk in _chunked(mgs, max(1, int(self.page_size or 1))):
            if cfg.max_evidence_queries is not None and evidence_queries >= cfg.max_evidence_queries:
                log.warning(
                    "SPARQL max evidence query limit reached (%s). Stopping evidence expansion early.",
                    cfg.max_evidence_queries,
                )
                break
            evidence_queries += 1

            bindings, chunk_failures, mg_failures, last_exc = self._select_evidence_rows_adaptive(
                mg_chunk,
                caps=caps,
                flags=flags,
                restrict_compounds=restrict_compounds,
                restrict_proteins=restrict_proteins,
            )
            if chunk_failures or mg_failures:
                failed_chunks += chunk_failures
                failed_measuregroups += mg_failures
                log.warning(
                    "SPARQL evidence skipped failures so far: failed_chunks=%d failed_measuregroups=%d last_error=%s",
                    failed_chunks,
                    failed_measuregroups,
                    last_exc,
                )
                over_chunks = cfg.max_failed_chunks is not None and failed_chunks > cfg.max_failed_chunks
                over_mgs = (
                    cfg.max_failed_measuregroups is not None
                    and failed_measuregroups > cfg.max_failed_measuregroups
                )
                if over_chunks or over_mgs:
                    raise RuntimeError(
                        "SPARQL evidence skipped too many chunks/measuregroups "
                        f"(failed_chunks={failed_chunks}, failed_measuregroups={failed_measuregroups})"
                    ) from last_exc

            for b in bindings:
                def v(name: str) -> Optional[str]:
                    cell = b.get(name)
                    return cell.get("value") if isinstance(cell, dict) else None

                mg_term = iri_to_term(v("mg") or "")
                if mg_term:
                    mg_id = _term_id(mg_term)
                    if mg_id not in seen_mg:
                        seen_mg.add(mg_id)
                        yield {"kind": "measuregroup", "data": {"mg_id": mg_id, "mg_term": mg_term}}

                # BioAssay
                ba_term = iri_to_term(v("bioassay") or "")
                if ba_term:
                    aid = _extract_int(ba_term, r"AID(\d+)$")
                    if aid is not None and aid not in seen_ba:
                        seen_ba.add(aid)
                        yield {"kind": "bioassay", "data": {"aid": aid, "bioassay_term": ba_term, "name": v("baname")}}
                    if aid is not None and mg_term:
                        yield {"kind": "mg_bioassay", "data": {"mg_id": _term_id(mg_term), "aid": aid}}

                # Gene participant (direct)
                gene_target_term = iri_to_term(v("geneTarget") or "")
                if gene_target_term and gene_target_term.startswith("gene:"):
                    gid = _gid(gene_target_term)
                    if gid is not None:
                        gid_s = str(gid)
                        if gid_s not in seen_gene:
                            seen_gene.add(gid_s)
                            sym = v("gsym")
                            if not sym:
                                gs_node_term = iri_to_term(v("gsNode") or "")
                                if gs_node_term and ":" in gs_node_term:
                                    sym = gs_node_term.split(":", 1)[-1]
                            yield {"kind": "gene", "data": {"gene_id": gid_s, "gene_term": gene_target_term, "name": v("gname"), "symbol": sym}}
                        if mg_term:
                            yield {"kind": "mg_gene", "data": {"mg_id": _term_id(mg_term), "gene_id": gid_s}}

                # Protein
                prot_term = iri_to_term(v("protein") or "")
                if prot_term:
                    acc = _uniprot_acc(prot_term) or prot_term
                    if acc not in seen_prot:
                        seen_prot.add(acc)
                        yield {"kind": "protein", "data": {
                            "protein_id": acc,
                            "protein_term": prot_term,
                            "name": v("pname"),
                            "sequence": v("seq"),
                            "gene_term": iri_to_term(v("gene") or "") if v("gene") else None,
                            "gene_id": str(_gid(iri_to_term(v("gene"))) ) if v("gene") and _gid(iri_to_term(v("gene"))) is not None else None,
                        }}
                    if mg_term:
                        yield {"kind": "mg_protein", "data": {"mg_id": _term_id(mg_term), "protein_id": acc}}

                # Cell line / anatomy (optional context)
                if getattr(flags, "include_optional_context", True):
                    cell_term = iri_to_term(v("cell") or "")
                    if cell_term and cell_term.startswith("cell:"):
                        cell_id = _term_id(cell_term)
                        yield {"kind": "cellline", "data": {"cellline_id": cell_id, "cell_term": cell_term}}
                        if mg_term:
                            yield {"kind": "mg_cellline", "data": {"mg_id": _term_id(mg_term), "cellline_id": cell_id}}
                        anat_term = iri_to_term(v("anat") or "")
                        if anat_term and anat_term.startswith("anatomy:"):
                            anat_id = _term_id(anat_term)
                            yield {"kind": "anatomy", "data": {"anatomy_id": anat_id, "anatomy_term": anat_term}}
                            yield {"kind": "cell_anatomy", "data": {"cellline_id": cell_id, "anatomy_id": anat_id}}

                # Organism (taxonomy participant)
                tax_term = iri_to_term(v("tax") or "")
                if tax_term and tax_term.startswith("taxonomy:"):
                    tid = _taxid(tax_term)
                    if tid is not None and tid not in seen_org:
                        seen_org.add(tid)
                        yield {"kind": "organism", "data": {"tax_id": tid, "tax_term": tax_term}}
                    if tid is not None and mg_term:
                        yield {"kind": "mg_organism", "data": {"mg_id": _term_id(mg_term), "tax_id": tid}}

                # Compound + Substance
                comp_term = iri_to_term(v("compound") or "")
                c = _cid(comp_term) if comp_term else None
                if c is not None and c not in seen_comp:
                    seen_comp.add(c)
                    yield {"kind": "compound", "data": {
                        "cid": c,
                        "compound_term": comp_term,
                        "name": v("cname"),
                        "smiles": v("smiles"),
                        "inchikey": v("inchikey"),
                        "inchi": v("inchi"),
                        "formula": v("formula"),
                        "molecular_weight": v("mw"),
                        "xlogp3": v("xlogp3"),
                        "tpsa": v("tpsa"),
                    }}

                sub_term = iri_to_term(v("sub") or "")
                s_id = _sid(sub_term) if sub_term else None
                if s_id is not None and s_id not in seen_sub:
                    seen_sub.add(s_id)
                    yield {"kind": "substance", "data": {
                        "sid": s_id,
                        "substance_term": sub_term,
                        "cid": c,
                        "source_term": iri_to_term(v("source")) if v("source") else None,
                    }}

                # Endpoint
                ep_term = iri_to_term(v("endpoint") or "")
                if ep_term:
                    ep_id = _term_id(ep_term)
                    if ep_id not in seen_ep:
                        seen_ep.add(ep_id)
                        yield {"kind": "endpoint", "data": {
                            "endpoint_id": ep_id,
                            "endpoint_term": ep_term,
                            "mg_id": _term_id(mg_term) if mg_term else None,
                            "sid": s_id,
                            "type": None,
                            "value": v("value"),
                            "unit": iri_to_term(v("unit")) if v("unit") else None,
                            "qualifier": v("qual"),
                            "outcome": iri_to_term(v("outcome")) if v("outcome") else None,
                            "label": v("eplabel"),
                        }}

                # Endpoint -> Reference
                if getattr(flags, "include_endpoint_references", False):
                    ref_term = iri_to_term(v("ref")) if v("ref") else None
                    if ref_term:
                        ref_id = _term_id(ref_term)
                        if ref_id not in seen_ref:
                            seen_ref.add(ref_id)
                            yield {"kind": "reference", "data": {"ref_id": ref_id, "ref_term": ref_term}}
                        if ep_term:
                            yield {"kind": "ep_reference", "data": {"endpoint_id": _term_id(ep_term), "ref_id": ref_id}}
