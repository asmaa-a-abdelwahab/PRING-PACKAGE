from __future__ import annotations

"""Runtime resource controls for PRING builds.

The guard is deliberately conservative. It cannot make the operating system's
CPU scheduler or every third-party library obey a perfect hard cap, but it does
three important things for local/laptop runs:

* limits native library thread pools before heavy imports use them;
* checks process + child RSS and stops before the configured memory ceiling;
* checks total system available memory and stops before the machine becomes
  unstable or starts aggressive swapping.

Use smaller ``--resource-check-interval`` values for stricter enforcement.
"""

from dataclasses import dataclass
import gc
import logging
import os
import time
from typing import Optional

log = logging.getLogger("pring")

try:  # psutil is optional but strongly recommended, especially on Windows.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on optional environment
    psutil = None  # type: ignore


class ResourceLimitExceeded(RuntimeError):
    """Raised when the configured resource budget is exceeded."""


@dataclass(frozen=True)
class RuntimeResourceLimits:
    max_memory_mb: Optional[int] = None
    max_cpu_percent: Optional[float] = None
    resource_check_interval_s: float = 5.0
    max_workers: int = 1
    memory_safety_margin_mb: int = 1024
    reserve_system_memory_mb: int = 1024

    @property
    def max_memory_bytes(self) -> Optional[int]:
        if self.max_memory_mb is None:
            return None
        return max(0, int(self.max_memory_mb)) * 1024 * 1024

    @property
    def effective_process_memory_bytes(self) -> Optional[int]:
        """Process RSS ceiling after reserving a safety margin.

        Stopping below the user-provided limit is intentional. Python and CSV
        generation can allocate temporary objects between checks. The margin
        prevents PRING from reaching the exact limit and freezing the machine.
        """
        hard = self.max_memory_bytes
        if hard is None:
            return None
        requested_margin = max(0, int(self.memory_safety_margin_mb or 0)) * 1024 * 1024
        # Do not let the default margin consume tiny test budgets. For normal
        # laptop/server budgets, the full requested margin is used; for small
        # budgets it is capped to roughly 20% of the configured limit.
        margin_cap = max(128 * 1024 * 1024, int(hard * 0.20))
        margin = min(requested_margin, margin_cap)
        return max(128 * 1024 * 1024, hard - margin)

    @property
    def reserve_system_memory_bytes(self) -> int:
        return max(0, int(self.reserve_system_memory_mb or 0)) * 1024 * 1024


def apply_thread_env(max_workers: Optional[int]) -> None:
    """Limit common numeric-library thread pools for predictable CPU use."""
    if max_workers is None:
        return
    workers = max(1, int(max_workers))
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "POLARS_MAX_THREADS",
        "RAYON_NUM_THREADS",
        "TOKENIZERS_PARALLELISM",
    ):
        # TOKENIZERS_PARALLELISM expects true/false, not a count.
        if name == "TOKENIZERS_PARALLELISM":
            os.environ.setdefault(name, "false")
        else:
            os.environ.setdefault(name, str(workers))


class ResourceGuard:
    """Periodic memory guard + soft CPU throttle.

    Memory is treated as a stop condition. CPU is a soft target: when process
    CPU is above the target, the guard sleeps briefly. A perfectly hard CPU cap
    is not portable in pure Python, but this prevents sustained overuse during
    PRING-controlled loops.
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
        if (limits.max_memory_mb is not None or limits.reserve_system_memory_mb) and psutil is None:
            log.warning("psutil is not installed; memory checks use a limited process-only fallback where available.")

    @classmethod
    def from_settings(cls, settings) -> "ResourceGuard":
        resources = getattr(settings, "resources", None)
        limits = RuntimeResourceLimits(
            max_memory_mb=getattr(resources, "max_memory_mb", None),
            max_cpu_percent=getattr(resources, "max_cpu_percent", None),
            resource_check_interval_s=float(getattr(resources, "resource_check_interval_s", 5.0) or 5.0),
            max_workers=int(getattr(resources, "max_workers", 1) or 1),
            memory_safety_margin_mb=int(getattr(resources, "memory_safety_margin_mb", 1024) or 0),
            reserve_system_memory_mb=int(getattr(resources, "reserve_system_memory_mb", 1024) or 0),
        )
        apply_thread_env(limits.max_workers)
        return cls(limits)

    def checkpoint(self, label: str = "", *, force: bool = False) -> None:
        now = time.monotonic()
        interval = max(0.05, float(self.limits.resource_check_interval_s or 5.0))
        if not force and (now - self._last_check) < interval:
            return
        self._last_check = now
        self._check_system_memory(label)
        self._check_process_memory(label)
        self._throttle_cpu(label)

    def describe(self) -> str:
        hard = self.limits.max_memory_mb
        effective = self.limits.effective_process_memory_bytes
        effective_text = "none" if effective is None else f"{effective / (1024 * 1024):.1f}"
        return (
            f"max_memory_mb={hard}, effective_stop_mb={effective_text}, "
            f"reserve_system_memory_mb={self.limits.reserve_system_memory_mb}, "
            f"max_cpu_percent={self.limits.max_cpu_percent}, max_workers={self.limits.max_workers}, "
            f"check_interval_s={self.limits.resource_check_interval_s}"
        )

    def _process_tree_rss_bytes(self) -> Optional[int]:
        if self._proc is not None:
            total = 0
            try:
                procs = [self._proc] + self._proc.children(recursive=True)
            except Exception:
                procs = [self._proc]
            for proc in procs:
                try:
                    total += int(proc.memory_info().rss)
                except Exception:
                    continue
            return total
        # Unix fallback; unavailable or not RSS-equivalent on Windows.
        try:  # pragma: no cover - platform dependent
            import resource
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(rss_kb) * 1024
        except Exception:
            return None

    def _check_system_memory(self, label: str) -> None:
        if psutil is None:
            return
        reserve = self.limits.reserve_system_memory_bytes
        if reserve <= 0:
            return
        try:
            available = int(psutil.virtual_memory().available)
        except Exception:
            return
        if available < reserve:
            raise ResourceLimitExceeded(
                f"System memory reserve reached at {label or 'checkpoint'}: "
                f"available={available / (1024 * 1024):.1f} MB < "
                f"reserve={reserve / (1024 * 1024):.1f} MB. "
                "PRING stopped before the OS became unstable. Reduce caps, disable optional layers/CSV mirrors, "
                "or lower --max-memory-mb and keep a larger reserve."
            )

    def _check_process_memory(self, label: str) -> None:
        effective = self.limits.effective_process_memory_bytes
        hard = self.limits.max_memory_bytes
        if effective is None:
            return
        rss = self._process_tree_rss_bytes()
        if rss is None or rss <= effective:
            return
        gc.collect()
        rss_after = self._process_tree_rss_bytes() or rss
        if rss_after > effective:
            raise ResourceLimitExceeded(
                f"Memory safety limit exceeded at {label or 'checkpoint'}: "
                f"process_tree_RSS={rss_after / (1024 * 1024):.1f} MB > "
                f"effective_stop={effective / (1024 * 1024):.1f} MB "
                f"configured_limit={(hard or effective) / (1024 * 1024):.1f} MB, "
                f"safety_margin={self.limits.memory_safety_margin_mb} MB. "
                "PRING stopped before crossing the configured hard budget. Reduce caps/batch size, "
                "disable optional layers/CSV mirrors, or increase --max-memory-mb."
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
        sleep_s = min(max(self.limits.resource_check_interval_s, 0.5), max(0.25, (current - target) / max(target, 1.0)))
        log.debug("CPU throttle at %s: current=%.1f%% target=%.1f%% sleeping %.2fs", label, current, target, sleep_s)
        time.sleep(sleep_s)
