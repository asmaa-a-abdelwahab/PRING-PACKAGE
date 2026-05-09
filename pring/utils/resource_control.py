from __future__ import annotations

"""Lightweight runtime resource controls for PRING builds.

The guard is intentionally conservative: it keeps the existing extraction logic
streaming/sequential, adds periodic memory checks, and applies soft CPU throttling
when psutil is available. This avoids freezing small laptops while still allowing
larger machines to run with higher caps.
"""

from dataclasses import dataclass
import gc
import logging
import os
import time
from typing import Optional

log = logging.getLogger("pring")

try:  # psutil is optional but recommended, especially on Windows.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on optional environment
    psutil = None  # type: ignore


class ResourceLimitExceeded(RuntimeError):
    """Raised when the configured hard resource budget is exceeded."""


@dataclass(frozen=True)
class RuntimeResourceLimits:
    max_memory_mb: Optional[int] = None
    max_cpu_percent: Optional[float] = None
    resource_check_interval_s: float = 5.0
    max_workers: int = 1

    @property
    def max_memory_bytes(self) -> Optional[int]:
        if self.max_memory_mb is None:
            return None
        return max(0, int(self.max_memory_mb)) * 1024 * 1024


def apply_thread_env(max_workers: Optional[int]) -> None:
    """Limit common numeric-library thread pools for predictable CPU use.

    PRING is currently mostly I/O-bound and sequential, but optional plugins or
    future similarity/modeling layers may import libraries that spawn native
    worker pools. Setting these variables helps keep CPU use bounded.
    """
    if max_workers is None:
        return
    workers = max(1, int(max_workers))
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(name, str(workers))


class ResourceGuard:
    """Periodic memory guard + soft CPU throttle.

    Memory is a hard limit: if RSS remains above the configured budget after a
    garbage collection pass, the build stops with a clear error. CPU is a soft
    limit: when process CPU is above the target, the guard sleeps briefly.
    """

    def __init__(self, limits: RuntimeResourceLimits) -> None:
        self.limits = limits
        self._last_check = 0.0
        self._proc = psutil.Process(os.getpid()) if psutil is not None else None
        self._cpu_supported = self._proc is not None and limits.max_cpu_percent is not None
        if self._proc is not None:
            try:
                self._proc.cpu_percent(interval=None)  # prime psutil counter
            except Exception:
                self._cpu_supported = False
        if limits.max_cpu_percent is not None and psutil is None:
            log.warning("--max-cpu-percent requires psutil; CPU throttling is disabled.")
        if limits.max_memory_mb is not None and psutil is None:
            log.warning("psutil is not installed; memory checks use a limited fallback where available.")

    @classmethod
    def from_settings(cls, settings) -> "ResourceGuard":
        resources = getattr(settings, "resources", None)
        limits = RuntimeResourceLimits(
            max_memory_mb=getattr(resources, "max_memory_mb", None),
            max_cpu_percent=getattr(resources, "max_cpu_percent", None),
            resource_check_interval_s=float(getattr(resources, "resource_check_interval_s", 5.0) or 5.0),
            max_workers=int(getattr(resources, "max_workers", 1) or 1),
        )
        apply_thread_env(limits.max_workers)
        return cls(limits)

    def checkpoint(self, label: str = "") -> None:
        now = time.monotonic()
        interval = max(0.25, float(self.limits.resource_check_interval_s or 5.0))
        if (now - self._last_check) < interval:
            return
        self._last_check = now
        self._check_memory(label)
        self._throttle_cpu(label)

    def _rss_bytes(self) -> Optional[int]:
        if self._proc is not None:
            try:
                return int(self._proc.memory_info().rss)
            except Exception:
                return None
        # Unix fallback; unavailable or not RSS-equivalent on some platforms.
        try:  # pragma: no cover - platform dependent
            import resource
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(rss_kb) * 1024
        except Exception:
            return None

    def _check_memory(self, label: str) -> None:
        limit = self.limits.max_memory_bytes
        if limit is None:
            return
        rss = self._rss_bytes()
        if rss is None:
            return
        if rss <= limit:
            return
        gc.collect()
        rss_after = self._rss_bytes() or rss
        if rss_after > limit:
            raise ResourceLimitExceeded(
                f"Memory limit exceeded at {label or 'checkpoint'}: "
                f"RSS={rss_after / (1024 * 1024):.1f} MB > "
                f"limit={limit / (1024 * 1024):.1f} MB. "
                "Reduce caps/batch size, disable optional layers, or increase --max-memory-mb."
            )

    def _throttle_cpu(self, label: str) -> None:
        if not self._cpu_supported or self._proc is None:
            return
        target = float(self.limits.max_cpu_percent or 0)
        if target <= 0:
            return
        try:
            current = float(self._proc.cpu_percent(interval=None))
        except Exception:
            return
        if current <= target:
            return
        # Sleep proportionally but keep it short so network-bound extraction stays responsive.
        sleep_s = min(max(self.limits.resource_check_interval_s, 0.5), max(0.25, (current - target) / max(target, 1.0)))
        log.debug("CPU throttle at %s: current=%.1f%% target=%.1f%% sleeping %.2fs", label, current, target, sleep_s)
        time.sleep(sleep_s)
