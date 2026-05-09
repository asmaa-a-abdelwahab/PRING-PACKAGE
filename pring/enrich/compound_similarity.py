from __future__ import annotations

"""Compound similarity enrichment for PRING.

The graph transformer already knows how to materialize Compound->Compound
SIMILAR_TO edges from `compound` rows containing a `similar_compounds` payload.
This module only populates that payload from PubChem PUG-REST.
"""

import logging
from typing import Iterable, Iterator, Optional

from pring.extract.pubchem_core import PubChemRow
from pring.extract.pubchem_rdf_rest import PubChemPugClient

log = logging.getLogger("pring")


def iter_compound_similarity_rows(
    cids: Iterable[int],
    *,
    pug: PubChemPugClient,
    method: str = "2d",
    threshold: int = 90,
    max_similar_per_compound: Optional[int] = 10,
    relation_source: str = "PubChem PUG-REST",
) -> Iterator[PubChemRow]:
    seen_sources: set[int] = set()
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
        payload = []
        for target_cid in similar:
            if int(target_cid) == cid:
                continue
            payload.append({
                "cid": int(target_cid),
                "method": f"fastsimilarity_{method.lower()}",
                "relation_source": relation_source,
                "threshold": int(threshold),
            })
            if limit is not None and len(payload) >= limit:
                break
        if payload:
            yield PubChemRow("compound", {"cid": cid, "similar_compounds": payload})
