from __future__ import annotations

"""Input adapter for PRING text-mined co-occurrence rows.

This keeps text-mined/weak evidence separate from curated PubChem assay evidence.
The importer is intentionally source-neutral: it can receive PubChem co-occurrence
exports, a manually curated CSV, or another text-mining pipeline output, provided
that the columns map to the fields below.
"""

import csv
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from pring.extract.pubchem_core import PubChemRow
from pring.transform.normalizer import make_stable_id, normalize_id


_TRUE = {"1", "true", "yes", "y", "on"}


def iter_textmining_csv_rows(path: Path, *, max_records: Optional[int] = None) -> Iterator[PubChemRow]:
    """Yield PubChemRow records from a text-mining CSV/TSV file.

    Accepted columns are forgiving and optional where possible:
      cooc_id, cid, compound_cid, protein_id, uniprot, gene_id, gene_symbol,
      disease_id, disease_label, reference_id, pmid, doi, score,
      sentence_count, mention_context, association_type,
      method_id, method_name, method_version, method_source.
    """
    path = Path(path)
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".tab"} else ","
    emitted = 0

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for raw in reader:
            row = {_norm_key(k): (v.strip() if isinstance(v, str) else v) for k, v in (raw or {}).items() if k is not None}
            if not any(row.values()):
                continue
            if max_records is not None and emitted >= max_records:
                break
            emitted += 1

            cid = _as_int(_first(row, "cid", "compound_cid", "compound", "compound_id"))
            protein_id = _clean_protein(_first(row, "protein_id", "uniprot", "target_id", "target"))
            gene_id = _clean_gene(_first(row, "gene_id", "gene"))
            gene_symbol = _first(row, "gene_symbol", "symbol")
            disease_id = _first(row, "disease_id", "mesh_id", "disease")
            reference_id = _reference_id(row)

            method_id = _first(row, "method_id", "textmine_id") or "textmine:imported"
            method_name = _first(row, "method_name", "method", "name") or "Imported text-mining co-occurrence"
            method_source = _first(row, "method_source", "source") or "external-file"
            method_version = _first(row, "method_version", "version")

            seed = "|".join(str(x) for x in [cid, protein_id, gene_id, gene_symbol, disease_id, reference_id, method_id] if x)
            cooc_id = _first(row, "cooc_id") or make_stable_id(seed or str(row), prefix="cooc:")

            yield PubChemRow("textmine", {
                "textmine_id": method_id,
                "method_id": method_id,
                "name": method_name,
                "version": method_version,
                "source": method_source,
            })
            yield PubChemRow("cooc", {
                "cooc_id": cooc_id,
                "score": _as_float(_first(row, "score", "cooc_score", "sco")),
                "sentence_count": _as_int(_first(row, "sentence_count", "sentences", "n_sentences")),
                "mention_context": _first(row, "mention_context", "context", "sentence", "snippet"),
                "association_type": _first(row, "association_type", "type", "cooc_type"),
                "evidence_level": "text_mined",
                "direction": _first(row, "direction"),
            })
            yield PubChemRow("cooc_textmine", {"cooc_id": cooc_id, "textmine_id": method_id})

            if cid is not None:
                yield PubChemRow("compound", {"cid": cid, "preferred_name": _first(row, "compound_name", "chemical_name")})
                yield PubChemRow("cooc_compound", {"cooc_id": cooc_id, "cid": cid})

            if protein_id:
                yield PubChemRow("protein", {"protein_id": protein_id, "name": _first(row, "protein_name", "target_name")})
                yield PubChemRow("cooc_protein", {"cooc_id": cooc_id, "protein_id": protein_id})

            if gene_id or gene_symbol:
                gid = gene_id or normalize_id(str(gene_symbol)) or str(gene_symbol)
                yield PubChemRow("gene", {"gene_id": gid, "symbol": gene_symbol})
                yield PubChemRow("cooc_gene", {"cooc_id": cooc_id, "gene_id": gid})

            if disease_id:
                did = normalize_id(str(disease_id)) or str(disease_id)
                yield PubChemRow("disease", {"disease_id": did, "label": _first(row, "disease_label", "disease_name")})
                yield PubChemRow("cooc_disease", {"cooc_id": cooc_id, "disease_id": did})

            if reference_id:
                yield PubChemRow("reference", {"reference_id": reference_id, "ref_id": reference_id, "doi": _first(row, "doi"), "pmid": _first(row, "pmid")})
                yield PubChemRow("cooc_reference", {"cooc_id": cooc_id, "reference_id": reference_id})


def _norm_key(key: str) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(" ", "_")


def _first(row: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = row.get(_norm_key(key))
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null", "nan"}:
            return text
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    import re
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _clean_protein(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("protein:acc"):
        return text.split("ACC", 1)[1]
    if text.lower().startswith("uniprot:"):
        return text.split(":", 1)[1]
    return text


def _clean_gene(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("gene:gid"):
        return text.split("GID", 1)[1]
    if text.lower().startswith("geneid:"):
        return text.split(":", 1)[1]
    return text


def _reference_id(row: Dict[str, Any]) -> Optional[str]:
    explicit = _first(row, "reference_id", "ref_id", "reference")
    if explicit:
        return explicit
    pmid = _first(row, "pmid")
    if pmid:
        return f"PMID:{pmid}"
    doi = _first(row, "doi")
    if doi:
        return f"DOI:{doi}"
    return None
