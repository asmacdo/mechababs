"""Make the `mechababs` package importable in unit tests without an editable
install, so `pytest tests/test_*.py` runs from a bare checkout.

Scoped to tests/, which is now the unit suite alone. The e2e scenario ships inside
the package instead (`mechababs/testing/e2e/`), so it travels with an install and
`mechababs test-cluster` can run it from a campaign; it drives the campaign venv's
`mechababs` binary via subprocess rather than importing package logic, but it does
import shared constants (e.g. the ledger filename) to assert against.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
