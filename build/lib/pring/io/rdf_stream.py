from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union
import gzip
import re


_NT_LINE_RE = re.compile(r'^\s*(<[^>]+>|_:[^\s]+)\s+(<[^>]+>)\s+(.+?)\s*\.\s*$')


def _open_any(path: Union[str, Path]):
    p = Path(path)
    if p.suffix == ".gz":
        return gzip.open(p, "rt", encoding="utf-8", errors="ignore")
    return open(p, "rt", encoding="utf-8", errors="ignore")


@dataclass(frozen=True)
class Triple:
    s: str
    p: str
    o: str


def stream_ntriples(path: Union[str, Path]) -> Iterator[Triple]:
    with _open_any(path) as f:
        for line in f:
            m = _NT_LINE_RE.match(line)
            if not m:
                continue
            s, p, o = m.group(1), m.group(2), m.group(3)
            yield Triple(s=s, p=p, o=o)
