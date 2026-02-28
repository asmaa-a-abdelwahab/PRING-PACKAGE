from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from pring.config import RdfRestConfig
from pring.io.http import HttpClient
from pring.transform.normalizer import normalize_id


@dataclass
class SparqlBinding:
    type: str
    value: str


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

    def __post_init__(self) -> None:
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        self.http = HttpClient(timeout_s=self.cfg.timeout_s, max_retries=self.cfg.max_retries, headers=headers)

    def close(self) -> None:
        self.http.close()

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

        Common params (observed):
          graph=... (required)
          subject=..., predicate/pred=..., object/obj=...  (triple-pattern)
          name=..., contain=true|false, return=...         (synonym graph convenience)
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

        data = self.http.get_json(url, params=params)
        return parse_sparql_results_json(data)


# ---------------------------
# Extractor skeleton (RDF-REST)
# ---------------------------

@dataclass
class PubChemRdfRestExtractor:
    """Extract minimal evidence graph from PubChemRDF using RDF REST queries.

    IMPORTANT:
    The *exact* predicates you need for Endpoint/MeasureGroup/Assay linking
    are defined in the PubChem RDF schema table. This class is structured so you
    can plug those predicates in once you decide the exact triple patterns.

    Strategy (high level):
      - If (compounds & targets): retrieve ONLY measuregroups/endpoints connecting them (intersection)
      - If only targets: expand cautiously (caps)
      - If only compounds: expand cautiously (caps)
    """
    client: PubChemRdfRestClient

    # URI builders (stable and easy)
    def compound_uri(self, cid: int) -> str:
        return f"http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID{cid}"

    # TODO: confirm the correct internal PubChem protein/gene URI scheme for your target IDs.
    def target_uri_guess(self, target_id: str) -> str:
        t = target_id.strip()
        if t.startswith("http://") or t.startswith("https://"):
            return t
        # Heuristic: treat as UniProt accession
        return f"http://purl.uniprot.org/uniprot/{t}"

    def iter_intersection_evidence(self, cids: List[int], target_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Yield extracted evidence rows (dicts) for intersection.

        This is the *entry point* for Case A.
        """
        # TODO (you will implement with correct triple patterns):
        #   1) map compounds -> substances (SID)
        #   2) map targets -> measuregroups (MG)
        #   3) intersect on measuregroups and pull endpoints
        # For now, this yields nothing but preserves the orchestration points.
        if not cids or not target_ids:
            return iter(())
        return iter(())

    def iter_expand_from_targets(self, target_ids: List[str], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Yield evidence rows when only targets are given (Case B)."""
        if not target_ids:
            return iter(())
        return iter(())

    def iter_expand_from_compounds(self, cids: List[int], *, caps: Any, flags: Any) -> Iterator[Dict[str, Any]]:
        """Yield evidence rows when only compounds are given (Case C)."""
        if not cids:
            return iter(())
        return iter(())
