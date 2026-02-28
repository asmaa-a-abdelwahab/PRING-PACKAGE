from __future__ import annotations

from pathlib import Path


def export_placeholder(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "README.txt").write_text(
        "PyG export stub. Implement export logic based on your node/edge feature schema.\n",
        encoding="utf-8",
    )
