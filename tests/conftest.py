"""Make the src/ layout importable when running pytest without installing.

`pip install -e ".[dev]"` is the documented path; this just means a bare
`python -m pytest` works from a fresh checkout too.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
