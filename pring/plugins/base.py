from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List


@dataclass(frozen=True)
class GraphDelta:
    nodes: List[Dict]
    rels: List[Dict]


class BasePlugin(ABC):
    name: str = "base"

    def enabled(self, settings: Any) -> bool:
        return True

    @abstractmethod
    def run(self, settings: Any) -> Iterator[GraphDelta]:
        raise NotImplementedError
