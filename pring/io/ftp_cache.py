from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import hashlib
import shutil
import time


@dataclass
class FtpCache:
    cache_dir: Path
    ttl_seconds: int = 7 * 24 * 3600

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str, etag: Optional[str] = None) -> str:
        raw = (url + "|" + (etag or "")).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def path_for(self, url: str, etag: Optional[str] = None, suffix: str = "") -> Path:
        name = self._key(url, etag) + (suffix or "")
        return self.cache_dir / name

    def is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        return age <= self.ttl_seconds

    def get(self, url: str, etag: Optional[str] = None, suffix: str = "") -> Optional[Path]:
        p = self.path_for(url, etag, suffix=suffix)
        return p if self.is_fresh(p) else None

    def put_file(self, src: Path, url: str, etag: Optional[str] = None, suffix: str = "") -> Path:
        dst = self.path_for(url, etag, suffix=suffix)
        shutil.copy2(src, dst)
        return dst

    def purge(self) -> int:
        n = 0
        now = time.time()
        for p in self.cache_dir.glob("*"):
            try:
                if now - p.stat().st_mtime > self.ttl_seconds:
                    p.unlink()
                    n += 1
            except FileNotFoundError:
                pass
        return n
