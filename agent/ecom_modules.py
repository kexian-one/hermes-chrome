from __future__ import annotations

import sys
from pathlib import Path


def load_scripts() -> None:
    scripts = str(Path(__file__).resolve().parent.parent / "skills" / "ecom-best-source" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
