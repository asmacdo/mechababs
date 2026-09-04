"""Make the `mechababs` package importable in unit tests without an editable
install, so `pytest tests/test_*.py` runs from a bare checkout.

Scoped to tests/, the unit suite. The e2e scenario ships inside the package
(`mechababs/testing/e2e/`), so it travels with an install and
`mechababs test-cluster` can run it from a campaign; it drives the campaign venv's
`mechababs` binary via subprocess rather than importing package logic, but it does
import shared constants (e.g. the ledger filename) to assert against.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def stamp_dataset_id(path, dataset_id="11111111-2222-3333-4444-555555555555"):
    """Give ``path`` a datalad-id without building a real datalad dataset.

    The superstudy marker records the super's id, so a fixture super needs one.
    Writing the committed config directly keeps the unit suite as plain directories
    — `datalad create` per fixture would dominate its runtime — and is the same file
    `campaign.dataset_id` reads in production.
    """
    config = Path(path) / ".datalad" / "config"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[datalad "dataset"]\n\tid = {dataset_id}\n')
    return dataset_id


def pretend_uv_check(monkeypatch, ok=True, detail=""):
    """Answer the delegated ``uv sync --check`` without a real uv or a real venv.

    The freshness half of the guard is a subprocess now, so a unit test that is
    *about* something else (add-dataset's commit shape, a scaffold transition) has to
    say what uv would have said. Stubbed at ``venv_matches_lock`` rather than at
    ``subprocess.run``, so a test that does not care about the invocation is not
    coupled to its argv; the tests that DO care stub the subprocess instead.
    """
    from mechababs import campaign as campaign_mod

    monkeypatch.setattr(
        campaign_mod, "venv_matches_lock", lambda campaign: (ok, detail)
    )
