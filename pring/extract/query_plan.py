from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from pring.config import BuildCaps, BuildFlags


class Mode(str, Enum):
    rdf_rest = "rdf-rest"
    ftp = "ftp"
    sparql = "sparql"   # alias for rdf-rest (PubChem RDF REST returns SPARQL JSON)


class Scope(str, Enum):
    intersection = "intersection"
    expand_from_targets = "expand-from-targets"
    expand_from_compounds = "expand-from-compounds"


@dataclass(frozen=True)
class InputFiles:
    chem_ids_path: Optional[Path] = None
    target_ids_path: Optional[Path] = None


@dataclass(frozen=True)
class BuildPlan:
    mode: Mode
    scope: Scope
    chem_ids: List[str]
    target_ids: List[str]
    caps: BuildCaps
    flags: BuildFlags

    def describe(self) -> str:
        return (
            f"mode={self.mode.value} scope={self.scope.value} "
            f"chem_ids={len(self.chem_ids)} target_ids={len(self.target_ids)} "
            f"caps={self.caps} flags={self.flags}"
        )


def load_id_file(path: Path) -> List[str]:
    ids: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        ids.append(s)
    return ids


def decide_scope(user_scope: Optional[str], chem_ids: List[str], target_ids: List[str]) -> Scope:
    if user_scope:
        scope = Scope(user_scope)
        if scope == Scope.intersection and (not chem_ids or not target_ids):
            raise ValueError("scope=intersection requires BOTH --chem-ids and --target-ids.")
        if scope == Scope.expand_from_targets and not target_ids:
            raise ValueError("scope=expand-from-targets requires --target-ids.")
        if scope == Scope.expand_from_compounds and not chem_ids:
            raise ValueError("scope=expand-from-compounds requires --chem-ids.")
        return scope

    # Defaulting behavior (your Case A/B/C)
    if chem_ids and target_ids:
        return Scope.intersection
    if target_ids:
        return Scope.expand_from_targets
    if chem_ids:
        return Scope.expand_from_compounds
    raise ValueError("At least one of --chem-ids or --target-ids must be provided.")


def decide_mode(user_mode: Optional[str]) -> Mode:
    if not user_mode:
        return Mode.rdf_rest
    m = Mode(user_mode)
    if m == Mode.sparql:
        return Mode.rdf_rest
    return m
