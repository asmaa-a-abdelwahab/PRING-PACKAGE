from __future__ import annotations

"""Input adapters for PRING text-mined co-occurrence rows.

The text-mined layer is intentionally separate from curated PubChem assay
activity. It can be loaded from a local CSV/TSV file, queried from the PubChemRDF
co-occurrence namespace when a mirror exposes it, or built from PubMed
Title/Abstract co-mentions as a network-safe fallback.
"""

import csv
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Set

from pring.extract.pubchem_core import PubChemRow
from pring.io.http import HttpClient
from pring.transform.normalizer import make_stable_id, normalize_id

log = logging.getLogger("pring.textmining")


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
                "association_type": _first(row, "association_type", "type", "cooc_type") or "compound-target cooccurrence",
                "evidence_level": "text_mined",
                "direction": _first(row, "direction") or "unknown",
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


# ---------------------------------------------------------------------------
# PubChemRDF endpoint-backed text-mining import
# ---------------------------------------------------------------------------


def _term_from_node(label: str, identifier: Any, pubchem_uri: Any = None) -> Optional[str]:
    """Build a PubChem CURIE-like term from a PRING node id/property."""
    for value in (pubchem_uri, identifier):
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text.startswith(("compound:", "protein:", "gene:", "disease:", "reference:", "cooccurrence:")):
            return text
        if "/pubchem/" in text:
            tail = text.rstrip("/").rsplit("/", 2)[-2:]
            if len(tail) == 2:
                return f"{tail[0]}:{tail[1]}"
    text = str(identifier or "").strip()
    if not text:
        return None
    if label == "Compound":
        if text.upper().startswith("CID"):
            return f"compound:{text.upper()}"
        if text.isdigit():
            return f"compound:CID{text}"
    if label == "Protein":
        # PubChemRDF protein identifiers are commonly ACC-prefixed. Accept both
        # raw UniProt accessions and already materialized PubChem protein ids.
        if text.startswith("protein:"):
            return text
        if text.upper().startswith("ACC"):
            return f"protein:{text.upper()}"
        return f"protein:ACC{text.upper()}"
    if label == "Gene":
        if text.startswith("gene:"):
            return text
        if text.upper().startswith("GID"):
            return f"gene:{text.upper()}"
        return f"gene:GID{text}"
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
    """Fetch PubChemRDF co-occurrence rows when the mirror exposes them.

    Several public PubChemRDF mirrors differ in how much of the co-occurrence
    layer they expose. The query is therefore intentionally defensive: it binds
    the target first, tries to discover co-occurrence resources around it, and
    records only rows that can be represented as PRING Cooc nodes.
    """
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

    method_id = "textmine:pubchem_rdf_cooccurrence"
    yield PubChemRow("textmine", {
        "textmine_id": method_id,
        "method_id": method_id,
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

        # Query shape 1: direct co-occurrence resource connected to target.
        # Query shape 2: same, with a bounded compound VALUES clause. Some
        # mirrors optimize one shape but not the other.
        compound_values = ""
        compound_filter = ""
        if compounds:
            compound_values = "VALUES ?compound { " + " ".join(compounds[:300]) + " }"
            compound_filter = "?cooc ?compoundPred ?compound . FILTER(STRSTARTS(STR(?compound), STR(compound:)))"
        templates = [
            f"""{SPARQL_PREFIXES}
PREFIX cooccurrence: <http://rdf.ncbi.nlm.nih.gov/pubchem/cooccurrence/>
PREFIX reference: <http://rdf.ncbi.nlm.nih.gov/pubchem/reference/>
SELECT DISTINCT ?cooc ?compound ?protein ?gene ?disease ?reference ?score ?sentenceCount ?context WHERE {{
  VALUES ?target {{ {target} }}
  ?cooc ?targetPred ?target .
  FILTER(STRSTARTS(STR(?cooc), STR(cooccurrence:)))
  OPTIONAL {{ {compound_values} {compound_filter} }}
  OPTIONAL {{ ?cooc ?proteinPred ?protein . FILTER(STRSTARTS(STR(?protein), STR(protein:))) }}
  OPTIONAL {{ ?cooc ?genePred ?gene . FILTER(STRSTARTS(STR(?gene), STR(gene:))) }}
  OPTIONAL {{ ?cooc ?diseasePred ?disease . FILTER(CONTAINS(STR(?disease), "/pubchem/disease/")) }}
  OPTIONAL {{ ?cooc ?referencePred ?reference . FILTER(STRSTARTS(STR(?reference), STR(reference:))) }}
  OPTIONAL {{ ?cooc ?scorePred ?score . FILTER(isNumeric(?score)) }}
  OPTIONAL {{ ?cooc ?sentenceCountPred ?sentenceCount . FILTER(isNumeric(?sentenceCount)) }}
  OPTIONAL {{ ?cooc rdfs:comment ?context }}
}}
LIMIT {int(limit)}""",
            f"""{SPARQL_PREFIXES}
PREFIX cooccurrence: <http://rdf.ncbi.nlm.nih.gov/pubchem/cooccurrence/>
SELECT DISTINCT ?cooc ?compound ?protein ?gene ?reference WHERE {{
  VALUES ?target {{ {target} }}
  {{ ?target ?p1 ?cooc . }} UNION {{ ?cooc ?p2 ?target . }}
  FILTER(STRSTARTS(STR(?cooc), STR(cooccurrence:)))
  OPTIONAL {{ ?cooc ?cp ?compound . FILTER(STRSTARTS(STR(?compound), STR(compound:))) }}
  OPTIONAL {{ ?cooc ?pp ?protein . FILTER(STRSTARTS(STR(?protein), STR(protein:))) }}
  OPTIONAL {{ ?cooc ?gp ?gene . FILTER(STRSTARTS(STR(?gene), STR(gene:))) }}
  OPTIONAL {{ ?cooc ?rp ?reference . FILTER(STRSTARTS(STR(?reference), STR(reference:))) }}
}}
LIMIT {int(limit)}""",
        ]

        rows: list[Dict[str, Any]] = []
        last_exc: Optional[Exception] = None
        for query in templates:
            try:
                try:
                    rows = client.select(query, timeout_s=getattr(getattr(client, "cfg", None), "evidence_timeout_s", None), max_retries=0)
                except TypeError:
                    rows = client.select(query)
                if rows:
                    break
            except Exception as exc:
                last_exc = exc
                rows = []
                continue
        if not rows:
            if last_exc:
                log.warning("PubChem text-mining endpoint query failed/empty for %s: %s", target, last_exc)
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
                "method_id": method_id,
                "method_name": "PubChemRDF co-occurrence endpoint",
                "method_version": "endpoint",
                "method_source": getattr(getattr(client, "cfg", None), "endpoint_url", "PubChemRDF SPARQL"),
            }
            if data.get("cid") is None and compounds:
                continue
            emitted += 1
            yield from _cooc_rows_from_data(data)

    if emitted == 0:
        log.info("PubChem text-mining endpoint returned no co-occurrence rows for the extracted target/compound scope.")


# ---------------------------------------------------------------------------
# PubMed title/abstract fallback
# ---------------------------------------------------------------------------


def iter_pubmed_textmining_rows(
    client: HttpClient,
    *,
    compound_entities: Iterable[Dict[str, Any]],
    target_entities: Iterable[Dict[str, Any]],
    max_records: Optional[int] = None,
    max_records_per_target: Optional[int] = 250,
    max_references_per_pair: Optional[int] = 5,
) -> Iterator[PubChemRow]:
    """Yield weak co-occurrence rows from PubMed title/abstract mentions.

    This fallback fixes the common public-mirror situation where the PubChemRDF
    co-occurrence namespace is absent or too expensive to query. It does not
    assert activity. It only adds separate ``Cooc`` evidence when an abstract or
    title mentions an extracted CYP target and one of the extracted compounds.
    """
    compounds = _prepare_compound_entities(compound_entities)
    targets = _prepare_target_entities(target_entities)
    if not compounds or not targets:
        log.warning("PubMed text-mining fallback requested, but compounds=%d targets=%d; skipping.", len(compounds), len(targets))
        return

    method_id = "textmine:pubmed_title_abstract_cooccurrence"
    yield PubChemRow("textmine", {
        "textmine_id": method_id,
        "method_id": method_id,
        "name": "PubMed title/abstract co-mention fallback",
        "version": "esearch+efetch",
        "source": "NCBI PubMed E-utilities",
    })

    global_limit = None if max_records is None else max(0, int(max_records))
    per_target_limit = None if max_records_per_target is None else max(1, int(max_records_per_target))
    refs_per_pair = None if max_references_per_pair is None else max(1, int(max_references_per_pair))
    emitted = 0
    seen: set[str] = set()
    pair_ref_counts: Dict[tuple[int, str], int] = {}

    def emit_article_mentions(target: Dict[str, Any], article: Dict[str, Any], matched: list[Dict[str, Any]]) -> Iterator[PubChemRow]:
        nonlocal emitted
        text = " ".join([article.get("title") or "", article.get("abstract") or ""]).strip()
        if not text or not matched:
            return
        context = _context_window(text, [target.get("preferred_term") or ""] + [m.get("preferred_term", "") for m in matched])
        pmid = article.get("pmid")
        reference_id = f"PMID:{pmid}" if pmid else None
        for compound in matched:
            if global_limit is not None and emitted >= global_limit:
                break
            pair_key = (int(compound["cid"]), str(target.get("protein_id") or target.get("gene_id") or target.get("display") or "target"))
            if refs_per_pair is not None:
                current = pair_ref_counts.get(pair_key, 0)
                if current >= refs_per_pair:
                    continue
                pair_ref_counts[pair_key] = current + 1
            cooc_seed = "|".join([str(pair_key[0]), pair_key[1], str(reference_id or ""), method_id])
            cooc_id = make_stable_id(cooc_seed, prefix="cooc:")
            uniq = f"{cooc_id}|{reference_id or ''}"
            if uniq in seen:
                continue
            seen.add(uniq)
            data: Dict[str, Any] = {
                "cooc_id": cooc_id,
                "cid": compound["cid"],
                "compound_name": compound.get("preferred_name"),
                "protein_id": target.get("protein_id"),
                "protein_name": target.get("protein_name"),
                "gene_id": target.get("gene_id"),
                "gene_symbol": target.get("gene_symbol"),
                "reference_id": reference_id,
                "pmid": pmid,
                "score": None,
                "sentence_count": 1,
                "mention_context": context[:1000] if context else None,
                "association_type": "compound-target title/abstract cooccurrence",
                "direction": "unknown",
                "evidence_level": "text_mined_weak_context",
                "method_id": method_id,
                "method_name": "PubMed title/abstract co-mention fallback",
                "method_version": "esearch+efetch",
                "method_source": "NCBI PubMed E-utilities",
            }
            emitted += 1
            yield PubChemRow("compound", {"cid": compound["cid"], "preferred_name": compound.get("preferred_name")})
            if data.get("protein_id"):
                yield PubChemRow("protein", {"protein_id": data.get("protein_id"), "name": data.get("protein_name"), "gene_symbol": data.get("gene_symbol")})
            if data.get("gene_id"):
                yield PubChemRow("gene", {"gene_id": data.get("gene_id"), "symbol": data.get("gene_symbol")})
            if reference_id:
                yield PubChemRow("reference", {"reference_id": reference_id, "ref_id": reference_id, "pmid": pmid, "title": article.get("title")})
            yield from _cooc_rows_from_data(data)

    for target in targets:
        if global_limit is not None and emitted >= global_limit:
            break
        start_for_target = emitted
        query = _pubmed_query_for_target(target)
        if not query:
            continue
        remaining = None if global_limit is None else max(0, global_limit - emitted)
        retmax = per_target_limit if remaining is None else min(per_target_limit or remaining, remaining)
        if not retmax:
            break
        try:
            ids = _pubmed_esearch(client, query, retmax=int(retmax))
        except Exception:
            log.warning("PubMed text-mining eSearch failed for %s", target.get("display") or target.get("protein_id") or target.get("gene_id"), exc_info=True)
            ids = []
        articles: list[Dict[str, Any]] = []
        if ids:
            try:
                articles = _pubmed_efetch_articles(client, ids)
            except Exception:
                log.warning("PubMed text-mining eFetch failed for target %s", target.get("display"), exc_info=True)
                articles = []

        for article in articles:
            if global_limit is not None and emitted >= global_limit:
                break
            text = " ".join([article.get("title") or "", article.get("abstract") or ""]).strip()
            if not text:
                continue
            lowered = _normalize_text_for_match(text)
            if not any(_contains_phrase(lowered, term) for term in target.get("match_terms", [])):
                continue
            matched = _find_compound_mentions(lowered, compounds)
            yield from emit_article_mentions(target, article, matched)

        # Target-only PubMed searches often return CYP papers that do not happen
        # to mention an extracted compound in the first retmax records. If broad
        # search produced no Cooc rows for this target, do a bounded pairwise
        # fallback over the best compound names/synonyms. This is slower, so it
        # is activated only when needed and remains governed by max_records caps.
        if emitted == start_for_target:
            pairwise_budget = min(len(compounds), max(1, int(retmax)))
            for compound in compounds[:pairwise_budget]:
                if global_limit is not None and emitted >= global_limit:
                    break
                if refs_per_pair is not None and pair_ref_counts.get((int(compound["cid"]), str(target.get("protein_id") or target.get("gene_id") or target.get("display") or "target")), 0) >= refs_per_pair:
                    continue
                pair_query = _pubmed_pair_query_for_target_compound(target, compound)
                if not pair_query:
                    continue
                try:
                    pair_ids = _pubmed_esearch(client, pair_query, retmax=max(1, refs_per_pair or 1))
                except Exception:
                    log.debug("PubMed pairwise text-mining eSearch failed for target=%s compound=%s", target.get("display"), compound.get("preferred_name"), exc_info=True)
                    continue
                if not pair_ids:
                    continue
                try:
                    pair_articles = _pubmed_efetch_articles(client, pair_ids)
                except Exception:
                    log.debug("PubMed pairwise text-mining eFetch failed for target=%s compound=%s", target.get("display"), compound.get("preferred_name"), exc_info=True)
                    continue
                for article in pair_articles:
                    text = " ".join([article.get("title") or "", article.get("abstract") or ""]).strip()
                    lowered = _normalize_text_for_match(text)
                    if any(_contains_phrase(lowered, t) for t in target.get("match_terms", [])) and any(_contains_phrase(lowered, t) for t in compound.get("match_terms", [])):
                        yield from emit_article_mentions(target, article, [compound])

    if emitted == 0:
        log.warning("PubMed text-mining fallback completed but produced zero compound-target co-occurrences. Check compound names/synonyms and target symbols.")
    else:
        log.info("PubMed text-mining fallback produced %d compound-target co-occurrence records.", emitted)


def _cooc_rows_from_data(data: Dict[str, Any]) -> Iterator[PubChemRow]:
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


def _pubmed_esearch(client: HttpClient, term: str, *, retmax: int) -> list[str]:
    data = client.get_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "retmode": "json", "retmax": max(1, int(retmax)), "sort": "relevance", "term": term},
    )
    ids = (((data or {}).get("esearchresult") or {}).get("idlist") or [])
    return [str(x) for x in ids if str(x).strip()]


def _pubmed_efetch_articles(client: HttpClient, ids: Iterable[str]) -> list[Dict[str, Any]]:
    ids_text = ",".join(str(x) for x in ids if str(x).strip())
    if not ids_text:
        return []
    xml = client.get_text(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "retmode": "xml", "id": ids_text},
    )
    if not xml.strip():
        return []
    root = ET.fromstring(xml)
    out: list[Dict[str, Any]] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = _xml_text(article.find(".//PMID"))
        title = " ".join(article.itertext()) if False else _xml_text(article.find(".//ArticleTitle"))
        abstract_parts = []
        for node in article.findall(".//Abstract/AbstractText"):
            label = node.attrib.get("Label")
            text = " ".join(t.strip() for t in node.itertext() if t and t.strip())
            if label and text:
                abstract_parts.append(f"{label}: {text}")
            elif text:
                abstract_parts.append(text)
        out.append({"pmid": pmid, "title": title, "abstract": " ".join(abstract_parts)})
    return out


def _xml_text(node: Optional[ET.Element]) -> Optional[str]:
    if node is None:
        return None
    text = " ".join(t.strip() for t in node.itertext() if t and t.strip())
    return text or None


def _pubmed_query_for_target(target: Dict[str, Any]) -> str:
    terms = []
    for term in target.get("query_terms", []):
        term = str(term or "").strip()
        if len(term) >= 3:
            terms.append(f'"{term}"[Title/Abstract]')
    terms = list(dict.fromkeys(terms))[:8]
    if not terms:
        return ""
    biology = "(inhibitor[Title/Abstract] OR inhibition[Title/Abstract] OR substrate[Title/Abstract] OR metabolism[Title/Abstract] OR metabolized[Title/Abstract] OR cytochrome[Title/Abstract] OR CYP[Title/Abstract] OR pharmacokinetic[Title/Abstract])"
    return "(" + " OR ".join(terms) + ") AND " + biology


def _pubmed_pair_query_for_target_compound(target: Dict[str, Any], compound: Dict[str, Any]) -> str:
    target_terms = []
    for term in target.get("query_terms", []):
        term = str(term or "").strip()
        if len(term) >= 3:
            target_terms.append(f'"{term}"[Title/Abstract]')
    compound_terms = []
    for term in compound.get("match_terms", []):
        # Use original-ish preferred names where possible. Normalized terms are
        # sufficient for PubMed phrase queries after whitespace normalization.
        term = str(term or "").strip()
        if len(term) >= 4 and not term.lower().startswith("cid "):
            compound_terms.append(f'"{term}"[Title/Abstract]')
    target_terms = list(dict.fromkeys(target_terms))[:5]
    compound_terms = list(dict.fromkeys(compound_terms))[:5]
    if not target_terms or not compound_terms:
        return ""
    biology = "(inhibition[Title/Abstract] OR inhibitor[Title/Abstract] OR substrate[Title/Abstract] OR metabolism[Title/Abstract] OR metabolized[Title/Abstract])"
    return "(" + " OR ".join(target_terms) + ") AND (" + " OR ".join(compound_terms) + ") AND " + biology


def _prepare_compound_entities(items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    seen: set[int] = set()
    for item in items or []:
        cid = _as_int(item.get("cid") or item.get("key_cid"))
        if cid is None or cid in seen:
            continue
        raw_terms = [
            item.get("preferred_name"), item.get("name"), item.get("title"),
            item.get("props_preferred_name"), item.get("props_name"),
        ]
        for key in ["synonyms", "props_synonyms", "props_synonyms_1", "props_synonyms_2", "props_synonyms_3"]:
            value = item.get(key)
            if isinstance(value, str):
                raw_terms.extend([x.strip() for x in re.split(r"\s*\|\s*", value) if x.strip()])
            elif isinstance(value, (list, tuple, set)):
                raw_terms.extend(list(value))
        terms = []
        for term in raw_terms:
            text = str(term or "").strip()
            if not text or text.lower().startswith("cid "):
                continue
            if len(text) < 4 or len(text) > 80:
                continue
            if re.fullmatch(r"[A-Za-z0-9\-]+", text) and len(text) < 4:
                continue
            # Avoid extremely generic tokens that create noisy PubMed matches.
            if text.lower() in {"compound", "chemical", "unknown", "untitled"}:
                continue
            terms.append(text)
        terms = list(dict.fromkeys(terms))[:12]
        if not terms:
            continue
        seen.add(cid)
        out.append({"cid": cid, "preferred_name": terms[0], "preferred_term": terms[0], "match_terms": [_normalize_text_for_match(t) for t in terms]})
    return out


def _prepare_target_entities(items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        protein_id = _clean_protein(str(item.get("protein_id") or item.get("key_protein_id") or "")) or None
        gene_id = _clean_gene(str(item.get("gene_id") or item.get("key_gene_id") or "")) or None
        gene_symbol = str(item.get("gene_symbol") or item.get("symbol") or item.get("props_symbol") or "").strip() or None
        protein_name = str(item.get("protein_name") or item.get("name") or item.get("props_name") or item.get("props_protein_name") or "").strip() or None
        key = protein_id or gene_id or gene_symbol or protein_name
        if not key or key in seen:
            continue
        terms = []
        # CYP symbols are the most PubMed-useful names; put them before raw
        # accessions/gene IDs so query construction is not dominated by IDs.
        if gene_symbol and gene_symbol.upper().startswith("CYP"):
            terms.append(gene_symbol.upper())
            compact = gene_symbol.upper().replace("CYP", "")
            if compact:
                terms.append(f"cytochrome P450 {compact}")
                terms.append(f"cytochrome P450 {gene_symbol.upper()}")
        for term in [gene_symbol, protein_name, protein_id, gene_id]:
            text = str(term or "").strip()
            if text and len(text) >= 3:
                terms.append(text)
        terms = list(dict.fromkeys(terms))[:12]
        if not terms:
            continue
        seen.add(key)
        out.append({
            "protein_id": protein_id,
            "gene_id": gene_id,
            "gene_symbol": gene_symbol,
            "protein_name": protein_name,
            "display": gene_symbol or protein_name or protein_id or gene_id,
            "preferred_term": terms[0],
            "query_terms": terms,
            "match_terms": [_normalize_text_for_match(t) for t in terms],
        })
    return out


def _find_compound_mentions(lowered_text: str, compounds: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    matched: list[Dict[str, Any]] = []
    for compound in compounds:
        for term in compound.get("match_terms", []):
            if _contains_phrase(lowered_text, term):
                matched.append(compound)
                break
    return matched[:25]


def _contains_phrase(lowered_text: str, lowered_phrase: str) -> bool:
    phrase = str(lowered_phrase or "").strip().lower()
    if not phrase:
        return False
    if len(phrase) <= 5 and re.fullmatch(r"[a-z0-9]+", phrase):
        return re.search(rf"\b{re.escape(phrase)}\b", lowered_text) is not None
    return phrase in lowered_text


def _normalize_text_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _context_window(text: str, terms: list[str], width: int = 500) -> str:
    if not text:
        return ""
    low = text.lower()
    positions = [low.find(str(t or "").lower()) for t in terms if t]
    positions = [p for p in positions if p >= 0]
    if not positions:
        return text[:width]
    pos = min(positions)
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width // 2)
    return text[start:end].strip()


# ---------------------------------------------------------------------------
# Small normalizer helpers
# ---------------------------------------------------------------------------


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
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith("protein:acc"):
        return text.split("ACC", 1)[1]
    if low.startswith("protein:"):
        return text.split(":", 1)[1].removeprefix("ACC")
    if low.startswith("uniprot:"):
        return text.split(":", 1)[1]
    if text.upper().startswith("ACC"):
        return text[3:]
    return text


def _clean_gene(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith("gene:gid"):
        return text.split("GID", 1)[1]
    if low.startswith("gene:"):
        return text.split(":", 1)[1].removeprefix("GID")
    if low.startswith("geneid:"):
        return text.split(":", 1)[1]
    if text.upper().startswith("GID"):
        return text[3:]
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
    value = str(term).rsplit(":", 1)[-1]
    return value[3:] if value.upper().startswith("ACC") else value


def _gene_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    value = str(term).rsplit(":", 1)[-1]
    return value[3:] if value.upper().startswith("GID") else value


def _disease_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    return str(term).rsplit(":", 1)[-1]


def _reference_id_from_term(term: Optional[str]) -> Optional[str]:
    if not term:
        return None
    value = str(term).rsplit(":", 1)[-1]
    return f"PMID:{value}" if value.isdigit() else value
