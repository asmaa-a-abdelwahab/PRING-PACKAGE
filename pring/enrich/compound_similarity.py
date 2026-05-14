from __future__ import annotations

"""Compound similarity enrichment for PRING.

This layer now materializes complete graph nodes for similar compounds, not only
SIMILAR_TO edges. For every returned similar CID, PRING emits a normal
``compound`` row containing PubChem PUG-REST properties/synonyms where available.
The standard graph transformer then creates Compound, Structure, Properties,
Synonyms, Neighbors, and the derived MolGraph feature node later in the run.
"""

import logging
from typing import Any, Iterable, Iterator, Optional

from pring.extract.pubchem_core import PubChemRow
from pring.extract.pubchem_rdf_rest import PubChemPugClient

log = logging.getLogger("pring")
_SIMILARITY_SMILES_CACHE: dict[int, Optional[str]] = {}


def iter_compound_similarity_rows(
    cids: Iterable[int],
    *,
    pug: PubChemPugClient,
    method: str = "2d",
    threshold: int = 90,
    max_similar_per_compound: Optional[int] = 10,
    relation_source: str = "PubChem PUG-REST",
    fetch_similar_compound_nodes: bool = True,
    synonym_limit: int = 25,
) -> Iterator[PubChemRow]:
    """Yield compound rows for similarity enrichment.

    The previous implementation emitted only a source ``compound`` row with a
    ``similar_compounds`` payload. That produced many SIMILAR_TO relationships
    whose target Compound nodes did not exist. This implementation fetches and
    yields one complete ``compound`` row for every similar CID as well.

    Args:
        cids: Source compound CIDs already present in the extracted scope.
        pug: PubChem PUG-REST client.
        method: PubChem fast similarity method, ``2d`` or ``3d``.
        threshold: Similarity threshold.
        max_similar_per_compound: Max similar CIDs per source compound.
        relation_source: Relationship provenance label.
        fetch_similar_compound_nodes: If true, retrieve full rows for similar
            compounds before emitting the SIMILAR_TO relationship row.
        synonym_limit: Max synonyms to store per similar compound.

    Yields:
        PubChemRow objects of kind ``compound``.
    """
    seen_sources: set[int] = set()
    emitted_compound_nodes: set[int] = set()
    limit = None if max_similar_per_compound is None else max(0, int(max_similar_per_compound))
    if limit == 0:
        return

    for raw_cid in cids:
        try:
            cid = int(raw_cid)
        except Exception:
            continue
        if cid in seen_sources:
            continue
        seen_sources.add(cid)
        try:
            similar = pug.similar_cids(cid, method=method, threshold=threshold, max_records=limit or 100)
        except Exception:
            log.warning("Skipping compound similarity for CID%s", cid, exc_info=True)
            continue

        target_cids: list[int] = []
        for target_cid in similar:
            try:
                target_int = int(target_cid)
            except Exception:
                continue
            if target_int == cid:
                continue
            target_cids.append(target_int)
            if limit is not None and len(target_cids) >= limit:
                break

        if not target_cids:
            continue

        if fetch_similar_compound_nodes:
            for target_record in _safe_compound_records(pug, target_cids, synonym_limit=synonym_limit):
                target_id = _as_int(target_record.get("cid"))
                if target_id is None or target_id in emitted_compound_nodes:
                    continue
                emitted_compound_nodes.add(target_id)
                target_record.setdefault("neighbor_source", "compound_similarity")
                target_record.setdefault("similarity_expansion", True)
                yield PubChemRow("compound", target_record)

        # PubChem fastsimilarity returns CIDs above the requested threshold.
        # When RDKit and SMILES are available, compute the exact local Morgan
        # Tanimoto value for modeling. Otherwise keep the threshold lower-bound
        # as a safe numeric edge weight.
        threshold_score = float(int(threshold)) / 100.0
        source_smiles = _smiles_for_cid(pug, cid)
        payload = []
        for target_cid in target_cids:
            target_smiles = _smiles_for_cid(pug, int(target_cid))
            exact = _rdkit_morgan_tanimoto(source_smiles, target_smiles)
            score = exact if exact is not None else threshold_score
            payload.append({
                "cid": int(target_cid),
                "method": f"fastsimilarity_{method.lower()}",
                "relation_source": relation_source,
                "threshold": int(threshold),
                "score": score,
                "edge_weight": score,
                "tanimoto": exact if exact is not None else "",
                "score_type": "rdkit_morgan_tanimoto" if exact is not None else "threshold_lower_bound",
            })
        yield PubChemRow("compound", {"cid": cid, "similar_compounds": payload})



def _smiles_for_cid(pug: PubChemPugClient, cid: int) -> Optional[str]:
    cid_int = _as_int(cid)
    if cid_int is None:
        return None
    if cid_int in _SIMILARITY_SMILES_CACHE:
        return _SIMILARITY_SMILES_CACHE[cid_int]
    smiles = None
    try:
        recs = list(pug.compound_records([cid_int], synonym_limit=0))
        if recs:
            rec = recs[0]
            for key in ("canonical_smiles", "CanonicalSMILES", "smiles", "SMILES", "isomeric_smiles", "IsomericSMILES"):
                value = rec.get(key)
                if value not in (None, ""):
                    smiles = str(value).strip()
                    break
    except Exception:
        smiles = None
    _SIMILARITY_SMILES_CACHE[cid_int] = smiles
    return smiles


def _rdkit_morgan_tanimoto(smiles_a: Optional[str], smiles_b: Optional[str]) -> Optional[float]:
    if not smiles_a or not smiles_b:
        return None
    try:
        from rdkit import Chem, DataStructs  # type: ignore
        from rdkit.Chem import rdFingerprintGenerator  # type: ignore
        mol_a = Chem.MolFromSmiles(str(smiles_a))
        mol_b = Chem.MolFromSmiles(str(smiles_b))
        if mol_a is None or mol_b is None:
            return None
        generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
        fp_a = generator.GetFingerprint(mol_a)
        fp_b = generator.GetFingerprint(mol_b)
        return round(float(DataStructs.TanimotoSimilarity(fp_a, fp_b)), 6)
    except Exception:
        return None

def _safe_compound_records(
    pug: PubChemPugClient,
    cids: Iterable[int],
    *,
    synonym_limit: int = 25,
) -> Iterator[dict[str, Any]]:
    """Retrieve compound rows, falling back to minimal CID-only rows on errors."""
    try:
        yield from pug.compound_records(cids, synonym_limit=synonym_limit)
        return
    except Exception:
        log.warning("Batch compound property retrieval failed for similar CIDs; falling back to per-CID retrieval.", exc_info=True)

    for raw_cid in cids:
        cid = _as_int(raw_cid)
        if cid is None:
            continue
        try:
            records = list(pug.compound_records([cid], synonym_limit=synonym_limit))
            if records:
                yield records[0]
                continue
        except Exception:
            log.warning("Could not retrieve full similar compound node for CID%s; writing minimal node.", cid, exc_info=True)
        yield {
            "cid": cid,
            "compound_term": f"compound:CID{cid}",
            "pubchem_uri": f"compound:CID{cid}",
            "preferred_name": f"CID {cid}",
            "similarity_expansion": True,
            "retrieval_status": "minimal_fallback",
        }


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None
