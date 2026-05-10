from __future__ import annotations

"""Input adapter for PRING text-mined co-occurrence rows.

This keeps text-mined/weak evidence separate from curated PubChem assay evidence.
The importer is intentionally source-neutral: it can receive PubChem co-occurrence
exports, a manually curated CSV, or another text-mining pipeline output, provided
that the columns map to the fields below.
"""

import csv
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

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

# ---------------------------------------------------------------------------
# PubChem endpoint-backed text-mining import
# ---------------------------------------------------------------------------

import logging
import re
from typing import Set

log = logging.getLogger("pring.textmining")


def _term_from_node(label: str, identifier: Any, pubchem_uri: Any = None) -> Optional[str]:
    """Build a PubChem CURIE-like term from a PRING node id/property."""
    for value in (pubchem_uri, identifier):
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text.startswith(("compound:", "protein:", "gene:", "disease:", "reference:")):
            return text
        if "/pubchem/" in text:
            tail = text.rstrip("/").rsplit("/", 2)[-2:]
            if len(tail) == 2:
                return f"{tail[0]}:{tail[1]}"
    text = str(identifier or "").strip()
    if not text:
        return None
    if label == "Compound":
        if text.startswith("CID"):
            return f"compound:{text}"
        if text.isdigit():
            return f"compound:CID{text}"
    if label == "Protein":
        return text if text.startswith("protein:") else f"protein:{text}"
    if label == "Gene":
        return text if text.startswith("gene:") else f"gene:{text}"
    return None


def iter_pubchem_textmining_sparql_rows(
    client: Any,
    *,
    compound_terms: Optional[Iterable[str]] = None,
    protein_terms: Optional[Iterable[str]] = None,
    gene_terms: Optional[Iterable[str]] = None,
    max_records: Optional[int] = None,
    max_records_per_target: Optional[int] = 250,
    max_references_per_pair: Optional[int] = 5,
) -> Iterator[PubChemRow]:
    """Fetch a separate PubChemRDF text-mined co-occurrence layer.

    The layer is intentionally weak/context evidence and is never converted into
    curated positive labels. The query is target-bounded and optionally
    compound-bounded so it remains safe for public SPARQL mirrors.
    """
    # Imported lazily to avoid coupling the CSV importer to the SPARQL extractor.
    from pring.extract.pubchem_sparql_mirror import SPARQL_PREFIXES, iri_to_term

    compounds = sorted({str(x).strip() for x in (compound_terms or []) if str(x).strip()})
    targets = sorted({str(x).strip() for x in list(protein_terms or []) + list(gene_terms or []) if str(x).strip()})
    if not targets:
        log.warning("Text-mining endpoint requested, but no protein/gene target terms were available from the extracted graph.")
        return

    global_limit = None if max_records is None else max(0, int(max_records))
    per_target_limit = None if max_records_per_target is None else max(1, int(max_records_per_target))
    refs_per_pair = None if max_references_per_pair is None else max(1, int(max_references_per_pair))
    emitted = 0
    seen: Set[str] = set()

    yield PubChemRow("textmine", {
        "textmine_id": "textmine:pubchem_rdf_cooccurrence",
        "method_id": "textmine:pubchem_rdf_cooccurrence",
        "name": "PubChemRDF co-occurrence endpoint",
        "version": "endpoint",
        "source": getattr(getattr(client, "cfg", None), "endpoint_url", "PubChemRDF SPARQL"),
    })

    for target in targets:
        if global_limit is not None and emitted >= global_limit:
            break
        remaining = None if global_limit is None else max(0, global_limit - emitted)
        limit = per_target_limit if remaining is None else min(per_target_limit or remaining, remaining)
        if not limit:
            break
        compound_values = ""
        # Always try to bind compounds. If the user provided extracted compounds,
        # use them as a VALUES filter; otherwise let the endpoint return any
        # compound co-mentioned with the target.
        compound_bound_clause = "?cooc ?compoundPred ?compound ."
        if compounds:
            # Keep the VALUES list modest; the curated graph already bounds the
            # candidate compounds. Large target-only text mining should be done
            # with a higher max_textmine_records_per_target instead of huge VALUES.
            comp_vals = " ".join(compounds[:500])
            compound_values = f"VALUES ?compound {{ {comp_vals} }}"
        query = f"""{SPARQL_PREFIXES}
PREFIX cooccurrence: <http://rdf.ncbi.nlm.nih.gov/pubchem/cooccurrence/>
PREFIX disease: <http://rdf.ncbi.nlm.nih.gov/pubchem/disease/>
PREFIX reference: <http://rdf.ncbi.nlm.nih.gov/pubchem/reference/>

SELECT DISTINCT ?cooc ?compound ?protein ?gene ?disease ?reference ?score ?sentenceCount ?context WHERE {{
  VALUES ?target {{ {target} }}
  ?cooc ?targetPred ?target .
  FILTER(STRSTARTS(STR(?cooc), STR(cooccurrence:)))

  OPTIONAL {{
    {compound_values}
    {compound_bound_clause}
    FILTER(STRSTARTS(STR(?compound), STR(compound:)))
  }}
  OPTIONAL {{ ?cooc ?proteinPred ?protein . FILTER(STRSTARTS(STR(?protein), STR(protein:))) }}
  OPTIONAL {{ ?cooc ?genePred ?gene . FILTER(STRSTARTS(STR(?gene), STR(gene:))) }}
  OPTIONAL {{ ?cooc ?diseasePred ?disease . FILTER(STRSTARTS(STR(?disease), STR(disease:))) }}
  OPTIONAL {{ ?cooc ?referencePred ?reference . FILTER(STRSTARTS(STR(?reference), STR(reference:))) }}
  OPTIONAL {{ ?cooc ?scorePred ?score . FILTER(isNumeric(?score)) }}
  OPTIONAL {{ ?cooc ?sentenceCountPred ?sentenceCount . FILTER(isNumeric(?sentenceCount)) }}
  OPTIONAL {{ ?cooc rdfs:comment ?context }}
}}
LIMIT {int(limit)}"""
        try:
            rows = client.select(query, timeout_s=getattr(getattr(client, "cfg", None), "evidence_timeout_s", None), max_retries=0)
        except TypeError:
            rows = client.select(query)
        except Exception as exc:
            log.warning("PubChem text-mining endpoint query failed for %s: %s", target, exc)
            continue

        pair_ref_counts: Dict[tuple[str, str], int] = {}
        for b in rows:
            if global_limit is not None and emitted >= global_limit:
                break
            cooc_term = _binding_term(b, "cooc", iri_to_term)
            if not cooc_term:
                continue
            compound = _binding_term(b, "compound", iri_to_term)
            protein = _binding_term(b, "protein", iri_to_term)
            gene = _binding_term(b, "gene", iri_to_term)
            disease = _binding_term(b, "disease", iri_to_term)
            ref = _binding_term(b, "reference", iri_to_term)

            # Some endpoint rows only bind the requested target. Preserve it.
            if target.startswith("protein:") and not protein:
                protein = target
            if target.startswith("gene:") and not gene:
                gene = target

            pair_key = (compound or "", protein or gene or target)
            if refs_per_pair is not None and ref:
                current = pair_ref_counts.get(pair_key, 0)
                if current >= refs_per_pair:
                    continue
                pair_ref_counts[pair_key] = current + 1

            cooc_id = cooc_term.replace("cooccurrence:", "cooc:")
            uniq = "|".join([cooc_id, compound or "", protein or "", gene or "", disease or "", ref or ""])
            if uniq in seen:
                continue
            seen.add(uniq)

            data: Dict[str, Any] = {
                "cooc_id": cooc_id,
                "cid": _cid_from_term(compound),
                "protein_id": _protein_id_from_term(protein),
                "gene_id": _gene_id_from_term(gene),
                "disease_id": _disease_id_from_term(disease),
                "reference_id": _reference_id_from_term(ref),
                "score": _binding_value(b, "score"),
                "sentence_count": _binding_value(b, "sentenceCount"),
                "mention_context": _binding_value(b, "context"),
                "association_type": "compound-target cooccurrence" if compound and (protein or gene) else "entity cooccurrence",
                "direction": "unknown",
                "evidence_level": "text_mined_weak_context",
                "method_id": "textmine:pubchem_rdf_cooccurrence",
                "method_name": "PubChemRDF co-occurrence endpoint",
                "method_version": "endpoint",
                "method_source": getattr(getattr(client, "cfg", None), "endpoint_url", "PubChemRDF SPARQL"),
            }
            if data.get("cid") is None and compounds:
                # Do not create target-only Cooc records when the user explicitly
                # bounded text mining to extracted compounds and the endpoint did
                # not return a compound binding.
                continue
            emitted += 1
            yield PubChemRow("cooc", data)
            yield PubChemRow("cooc_textmine", data)
            if data.get("cid") is not None:
                yield PubChemRow("cooc_compound", data)
            if data.get("protein_id"):
                yield PubChemRow("cooc_protein", data)
            if data.get("gene_id"):
                yield PubChemRow("cooc_gene", data)
            if data.get("disease_id"):
                yield PubChemRow("cooc_disease", data)
            if data.get("reference_id"):
                yield PubChemRow("cooc_reference", data)

    if emitted == 0:
        log.info("PubChem text-mining endpoint returned no co-occurrence rows for the extracted target/compound scope.")


def _binding_term(binding: Dict[str, Any], key: str, converter: Any) -> Optional[str]:
    value = ((binding.get(key) or {}).get("value") if isinstance(binding.get(key), dict) else None)
    return converter(value) if value else None


def _binding_value(binding: Dict[str, Any], key: str) -> Optional[Any]:
    return ((binding.get(key) or {}).get("value") if isinstance(binding.get(key), dict) else None)


def _cid_from_term(term: Optional[str]) -> Optional[int]:
    if not term:
        return None
    m = re.search(r"CID(\d+)$", str(term))
    return int(m.group(1)) if m else None


def _protein_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    return str(term).rsplit(":", 1)[-1]


def _gene_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    return str(term).rsplit(":", 1)[-1]


def _disease_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    return str(term).rsplit(":", 1)[-1]


def _reference_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    return str(term).rsplit(":", 1)[-1]
