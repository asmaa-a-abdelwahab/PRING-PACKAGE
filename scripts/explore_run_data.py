#!/usr/bin/env python3
"""Backward-compatible wrapper for PRING run-data EDA.

Prefer:
    python -m pring eda --run-path <run-dir-or-zip> --output-dir <eda-dir>
"""

from pring.analysis.run_eda import main


if __name__ == "__main__":
    main()
