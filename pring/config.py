from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


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
class RdfRestConfig:
    """PubChem RDF REST query endpoint config."""
    base_url: str = "https://pubchem.ncbi.nlm.nih.gov/rest/rdf"
    timeout_s: float = 60.0
    max_retries: int = 3
    user_agent: str = "pring/0.1 (+https://example.org)"


@dataclass(frozen=True)
class BuildCaps:
    """Caps to keep graphs thesis-friendly."""
    max_compounds_per_target: Optional[int] = None
    max_targets_per_compound: Optional[int] = None
    # When expanding from compounds, a single CID can map to thousands of SIDs.
    # Cap how many substances we will traverse per compound.
    max_substances_per_compound: Optional[int] = None
    max_measuregroups_per_target: Optional[int] = None
    max_measuregroups_per_compound: Optional[int] = None
    max_endpoints_per_pair: Optional[int] = None


@dataclass(frozen=True)
class BuildFlags:
    include_textmining: bool = False
    include_optional_context: bool = True
    # Optional taxonomic restriction. When provided, PRING will keep only
    # evidence (measuregroups/endpoints) whose participants include a matching
    # PubChem Taxonomy entity (taxonomy:TAXIDxxxx).
    taxids: Optional[tuple[int, ...]] = None


@dataclass(frozen=True)
class Settings:
    neo4j: Neo4jConfig
    rdf_rest: RdfRestConfig = field(default_factory=RdfRestConfig)

    # Path to your Graphviz schema (DOT). Used for validation.
    schema_dot_path: Optional[Path] = None

    # Batch size for UNWIND loads
    batch_size: int = 5_000

    # Cache directory (for HTTP or FTP mode)
    cache_dir: Path = field(default_factory=lambda: Path(_env("PRING_CACHE_DIR", str(Path.home() / ".cache" / "pring"))))

    # Default behavior controls
    caps: BuildCaps = field(default_factory=BuildCaps)
    flags: BuildFlags = field(default_factory=BuildFlags)

    # Labels -> key fields (MERGE keys). Adjust these to match your canonical IDs.
    node_keys: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: {
        "Compound": ("cid",),
        "Substance": ("sid",),
        "Protein": ("protein_id",),
        "Gene": ("gene_id",),
        "Organism": ("tax_id",),
        "Structure": ("cid",),
        "Properties": ("cid",),
        "Synonyms": ("cid",),
        "Neighbors": ("cid",),
        "BioAssay": ("aid",),
        "MeasureGrp": ("mg_id",),
        "Endpoint": ("endpoint_id",),
        "Source": ("source_id",),
        "Reference": ("ref_id",),
        "Pathway": ("pathway_id",),
        "CellLine": ("cellline_id",),
        "Anatomy": ("anatomy_id",),
        "Disease": ("disease_id",),
        "Cooc": ("cooc_id",),
        "TextMine": ("method_id",),
        # Plugins
        "UniProt": ("uniprot_id",),
        "GO": ("go_id",),
        "Reactome": ("reactome_id",),
        "InterPro": ("interpro_id",),
        "ChEMBL": ("chembl_id",),
        "BindingDB": ("bindingdb_id",),
        "DrugBank": ("drugbank_id",),
        "PDB": ("pdb_id",),
        "AlphaFold": ("alphafold_id",),
        "ProtEmbed": ("embed_id",),
        "MolGraph": ("cid",),
    })

    # Relationship-type overrides (optional):
    # (start_label, end_label, schema_edge_label) -> REL_TYPE
    rel_type_overrides: Dict[Tuple[str, str, str], str] = field(default_factory=dict)

    # Enable/disable plugins by import path (e.g., "pring.plugins.uniprot:get_plugin")
    enabled_plugins: List[str] = field(default_factory=list)

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)

    @staticmethod
    def from_env() -> "Settings":
        neo4j_uri = _env("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = _env("NEO4J_USER", "neo4j")
        neo4j_password = _env("NEO4J_PASSWORD", "neo4j")

        schema_dot = _env("PRING_SCHEMA_DOT", None)
        schema_dot_path = Path(schema_dot) if schema_dot else None

        batch_size = int(_env("PRING_BATCH_SIZE", "5000") or "5000")
        cache_dir = Path(_env("PRING_CACHE_DIR", str(Path.home() / ".cache" / "pring")))

        flags = BuildFlags(
            include_textmining=(_env("PRING_INCLUDE_TEXTMINING", "false").lower() == "true"),
            include_optional_context=(_env("PRING_INCLUDE_OPTIONAL_CONTEXT", "true").lower() == "true"),
            taxids=_parse_taxids(_env("PRING_TAXID", None)),
        )

        caps = BuildCaps(
            max_compounds_per_target=_int_or_none(_env("PRING_MAX_COMPOUNDS_PER_TARGET")),
            max_targets_per_compound=_int_or_none(_env("PRING_MAX_TARGETS_PER_COMPOUND")),
            max_substances_per_compound=_int_or_none(_env("PRING_MAX_SUBS_PER_COMPOUND")),
            max_measuregroups_per_target=_int_or_none(_env("PRING_MAX_MG_PER_TARGET")),
            max_measuregroups_per_compound=_int_or_none(_env("PRING_MAX_MG_PER_COMPOUND")),
            max_endpoints_per_pair=_int_or_none(_env("PRING_MAX_ENDPOINTS_PER_PAIR")),
        )

        plugins = _env("PRING_PLUGINS", "")
        enabled_plugins = [p.strip() for p in plugins.split(",") if p.strip()]

        return Settings(
            neo4j=Neo4jConfig(uri=neo4j_uri, user=neo4j_user, password=neo4j_password),
            schema_dot_path=schema_dot_path,
            batch_size=batch_size,
            cache_dir=cache_dir,
            flags=flags,
            caps=caps,
            enabled_plugins=enabled_plugins,
        )


def _int_or_none(v: Optional[str]) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return None
    return int(v)


def _parse_taxids(v: Optional[str]) -> Optional[tuple[int, ...]]:
    """Parse a taxid list from env/CLI-style input.

    Accepts:
      - "9606"
      - "9606,10090"
      - "TAXID9606"
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    parts = [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]
    out: list[int] = []
    for p in parts:
        up = p.upper()
        if up.startswith("TAXID"):
            p = p[5:]
        if not p.isdigit():
            continue
        out.append(int(p))
    return tuple(sorted(set(out))) if out else None
