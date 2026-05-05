from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import os
from typing import Any, List, Optional, Tuple


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str
    database: str = "neo4j"
    encrypted: bool = False
    max_connection_lifetime: int = 3600
    max_connection_pool_size: int = 50
    connection_timeout: int = 30


@dataclass(frozen=True)
class SparqlConfig:
    endpoint_url: str = "https://idsm.elixir-czech.cz/sparql/endpoint/idsm"
    timeout_s: float = 120.0
    max_retries: int = 3
    user_agent: str = "pring/0.1"


@dataclass(frozen=True)
class RdfRestConfig:
    base_url: str = "https://pubchem.ncbi.nlm.nih.gov/rdf"
    timeout_s: float = 120.0
    max_retries: int = 3
    user_agent: str = "pring/0.1"
    honor_throttling_headers: bool = True
    min_delay_s: float = 0.2
    max_delay_s: float = 2.0


@dataclass(frozen=True)
class ResourceProfile:
    profile: str = "balanced"
    write_csv_mirrors: bool = True
    max_http_cache_mb: Optional[int] = None
    max_graph_artifact_mb: Optional[int] = None


@dataclass(frozen=True)
class BuildFlags:
    include_textmining: bool = False
    include_optional_context: bool = True
    include_endpoint_metadata: bool = True
    include_endpoint_references: bool = True
    taxids: Tuple[int, ...] = (9606,)


@dataclass(frozen=True)
class BuildCaps:
    max_compounds_per_target: Optional[int] = 200
    max_targets_per_compound: Optional[int] = None
    max_substances_per_compound: Optional[int] = None
    max_measuregroups_per_target: Optional[int] = 500
    max_measuregroups_per_compound: Optional[int] = None
    max_endpoints_per_pair: Optional[int] = 50


@dataclass(frozen=True)
class Settings:
    neo4j: Neo4jConfig
    rdf_rest: RdfRestConfig = field(default_factory=RdfRestConfig)
    sparql: SparqlConfig = field(default_factory=SparqlConfig)
    cache_dir: Path = Path(".cache/pring")
    batch_size: int = 1000
    node_keys: dict = field(default_factory=lambda: {
        # Core entities / evidence backbone
        "Compound": ("cid",),
        "Structure": ("cid",),
        "Properties": ("cid",),
        "Synonyms": ("cid",),
        "Neighbors": ("cid",),
        "Substance": ("sid",),
        "Source": ("source_id",),
        "Organism": ("taxid",),
        "Protein": ("protein_id",),
        "Gene": ("gene_id",),
        "BioAssay": ("aid",),
        "MeasureGrp": ("mg_id",),
        "Endpoint": ("endpoint_id",),
        "Reference": ("reference_id",),

        # Optional biological context
        "Pathway": ("pathway_id",),
        "CellLine": ("cellline_id",),
        "Anatomy": ("anatomy_id",),
        "Disease": ("disease_id",),

        # Text-mined / weak evidence layer
        "Cooc": ("cooc_id",),
        "TextMine": ("textmine_id",),

        # External enrichment / add-ons
        "UniProt": ("uniprot_acc",),
        "GO": ("go_id",),
        "Reactome": ("reactome_id",),
        "InterPro": ("interpro_id",),
        "ChEMBL": ("chembl_id",),
        "BindingDB": ("bindingdb_id",),
        "DrugBank": ("drugbank_id",),
        "PDB": ("pdb_id",),
        "AlphaFold": ("alphafold_id",),
        "ProtEmbed": ("embedding_id",),
        "MolGraph": ("repr_id",),

        # Derived modeling layer
        "Interaction": ("interaction_id",),
    })
    rel_type_overrides: dict = field(default_factory=lambda: {
        ("CellLine", "Anatomy", "DERIVED_FROM"): "DERIVED_FROM",
    })
    schema_dot_path: Optional[Path] = None
    flags: BuildFlags = field(default_factory=BuildFlags)
    caps: BuildCaps = field(default_factory=BuildCaps)
    enabled_plugins: List[str] = field(default_factory=list)
    connect_timeout_seconds: float = 10.0
    endpoint_retries: int = 3
    endpoint_backoff_seconds: float = 1.5
    save_raw_http_cache: bool = True
    save_extracted_artifacts: bool = True
    resources: ResourceProfile = field(default_factory=ResourceProfile)

    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)

    @staticmethod
    def from_env() -> "Settings":
        neo = Neo4jConfig(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "test"),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
            encrypted=_parse_bool(os.getenv("NEO4J_ENCRYPTED"), False),
            max_connection_lifetime=int(os.getenv("NEO4J_MAX_CONNECTION_LIFETIME", "3600")),
            max_connection_pool_size=int(os.getenv("NEO4J_MAX_CONNECTION_POOL_SIZE", "50")),
            connection_timeout=int(os.getenv("NEO4J_CONNECTION_TIMEOUT", "30")),
        )
        rdf_rest = RdfRestConfig(
            base_url=os.getenv("PRING_RDF_REST_BASE_URL", "https://pubchem.ncbi.nlm.nih.gov/rdf"),
            timeout_s=float(os.getenv("PRING_RDF_REST_TIMEOUT_S", "120.0")),
            max_retries=int(os.getenv("PRING_RDF_REST_MAX_RETRIES", "3")),
            user_agent=os.getenv("PRING_RDF_REST_USER_AGENT", "pring/0.1"),
            honor_throttling_headers=_parse_bool(os.getenv("PRING_RDF_REST_HONOR_THROTTLING_HEADERS"), True),
            min_delay_s=float(os.getenv("PRING_RDF_REST_MIN_DELAY_S", "0.2")),
            max_delay_s=float(os.getenv("PRING_RDF_REST_MAX_DELAY_S", "2.0")),
        )
        sparql = SparqlConfig(
            endpoint_url=os.getenv("PRING_SPARQL_ENDPOINT_URL", "https://idsm.elixir-czech.cz/sparql/endpoint/idsm"),
            timeout_s=float(os.getenv("PRING_SPARQL_TIMEOUT_S", "120.0")),
            max_retries=int(os.getenv("PRING_SPARQL_MAX_RETRIES", "3")),
            user_agent=os.getenv("PRING_SPARQL_USER_AGENT", "pring/0.1"),
        )
        cache_dir = Path(os.getenv("PRING_CACHE_DIR", ".cache/pring"))
        batch_size = int(os.getenv("PRING_BATCH_SIZE", "1000"))
        schema_dot = os.getenv("PRING_SCHEMA_DOT_PATH")
        flags = BuildFlags(
            include_textmining=_parse_bool(os.getenv("PRING_INCLUDE_TEXTMINING"), False),
            include_optional_context=_parse_bool(os.getenv("PRING_INCLUDE_OPTIONAL_CONTEXT"), True),
            include_endpoint_metadata=_parse_bool(os.getenv("PRING_INCLUDE_ENDPOINT_METADATA"), True),
            include_endpoint_references=_parse_bool(os.getenv("PRING_INCLUDE_ENDPOINT_REFERENCES"), True),
            taxids=_parse_taxids(os.getenv("PRING_TAXID", "9606")) or (9606,),
        )
        caps = BuildCaps(
            max_compounds_per_target=_int_or_none(os.getenv("PRING_MAX_COMPOUNDS_PER_TARGET"), 200),
            max_targets_per_compound=_int_or_none(os.getenv("PRING_MAX_TARGETS_PER_COMPOUND"), None),
            max_substances_per_compound=_int_or_none(os.getenv("PRING_MAX_SUBSTANCES_PER_COMPOUND"), None),
            max_measuregroups_per_target=_int_or_none(os.getenv("PRING_MAX_MEASUREGROUPS_PER_TARGET"), 500),
            max_measuregroups_per_compound=_int_or_none(os.getenv("PRING_MAX_MEASUREGROUPS_PER_COMPOUND"), None),
            max_endpoints_per_pair=_int_or_none(os.getenv("PRING_MAX_ENDPOINTS_PER_PAIR"), 50),
        )
        plugins_raw = os.getenv("PRING_PLUGINS", "")
        enabled_plugins = [p.strip() for p in plugins_raw.replace(";", ",").split(",") if p.strip()]
        resources = ResourceProfile(
            profile=os.getenv("PRING_RESOURCE_PROFILE", "balanced").strip().lower() or "balanced",
            write_csv_mirrors=_parse_bool(os.getenv("PRING_WRITE_CSV_MIRRORS"), True),
            max_http_cache_mb=_int_or_none(os.getenv("PRING_MAX_HTTP_CACHE_MB"), None),
            max_graph_artifact_mb=_int_or_none(os.getenv("PRING_MAX_GRAPH_ARTIFACT_MB"), None),
        )

        return Settings(
            neo4j=neo,
            rdf_rest=rdf_rest,
            sparql=sparql,
            cache_dir=cache_dir,
            batch_size=batch_size,
            flags=flags,
            caps=caps,
            enabled_plugins=enabled_plugins,
            schema_dot_path=Path(schema_dot) if schema_dot else None,
            connect_timeout_seconds=float(os.getenv("PRING_CONNECT_TIMEOUT_SECONDS", "10.0")),
            endpoint_retries=int(os.getenv("PRING_ENDPOINT_RETRIES", "3")),
            endpoint_backoff_seconds=float(os.getenv("PRING_ENDPOINT_BACKOFF_SECONDS", "1.5")),
            save_raw_http_cache=_parse_bool(os.getenv("PRING_SAVE_RAW_HTTP_CACHE"), True),
            save_extracted_artifacts=_parse_bool(os.getenv("PRING_SAVE_EXTRACTED_ARTIFACTS"), True),
            resources=resources,
        )


def _parse_bool(v: Optional[str], default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_taxids(v: str) -> Optional[Tuple[int, ...]]:
    vals: list[int] = []
    seen: set[int] = set()
    for part in (v or "").replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        if p.upper().startswith("TAXID"):
            p = p[5:]
        try:
            tid = int(p)
        except ValueError:
            continue
        if tid not in seen:
            seen.add(tid)
            vals.append(tid)
    return tuple(vals) or None


def _int_or_none(v: Optional[str], default: Optional[int] = None) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default
