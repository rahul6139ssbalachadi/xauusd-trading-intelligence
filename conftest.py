from __future__ import annotations

import sys
from pathlib import Path

# Ensures `import market_data` resolves to this project regardless of how
# pytest is invoked (bare `pytest`, `python -m pytest`, from a subdirectory,
# etc). The project has no installed package/build-system yet (Phase 1).
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
