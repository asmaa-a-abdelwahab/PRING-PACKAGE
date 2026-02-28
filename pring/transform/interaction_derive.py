from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Tuple


@dataclass(frozen=True)
class PredictedInteraction:
    cid: int
    protein_id: str
    score: float
    model: str
    evidence: Optional[List[str]] = None


def derive_predicted_interactions(
    endpoints: Iterable[Dict],
    *,
    min_support: int = 1,
    model_name: str = "baseline_v0",
) -> Iterator[PredictedInteraction]:
    """Starter derivation: build Compound->Protein 'prediction' edges from endpoints.

    Replace this with your proper model when you integrate MolGraph/ProtEmbed features.
    """
    buckets: Dict[Tuple[int, str], List[Dict]] = {}
    for ep in endpoints:
        cid = ep.get("cid")
        pid = ep.get("protein_id")
        if cid is None or pid is None:
            continue
        buckets.setdefault((int(cid), str(pid)), []).append(ep)

    for (cid, pid), rows in buckets.items():
        if len(rows) < min_support:
            continue
        num = 0.0
        den = 0.0
        evid: List[str] = []
        for r in rows:
            a = r.get("activity", 0)
            w = float(r.get("w", 1.0))
            num += (1.0 if a else 0.0) * w
            den += w
            if r.get("endpoint_id"):
                evid.append(str(r["endpoint_id"]))
        score = (num / den) if den else 0.0
        yield PredictedInteraction(cid=cid, protein_id=pid, score=score, model=model_name, evidence=evid or None)
