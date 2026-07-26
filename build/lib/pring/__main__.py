from __future__ import annotations

import logging
import sys

from .cli import main
from .utils.resource_control import ResourceLimitExceeded


if __name__ == "__main__":
    try:
        main()
    except ResourceLimitExceeded as exc:
        logging.getLogger("pring").error("Resource limit reached: %s", exc)
        sys.exit(3)
