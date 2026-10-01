"""The live-checkpoint characterization, isolated behind the ``llm`` marker.

A test that loads the real Laya checkpoint (a ~0.8 GB download on first use) is not a unit test:
its answer is model output, and it is exactly the surface that made the characterization suite
flaky when the environment armed it by accident. So it is opt-in. CI runs ``-m "not llm"`` and
never pays for it; a developer who wants to characterize the checkpoint runs it deliberately:

    python -m pytest tests/test_laya_live.py -m llm -n0 -s

Everything else exercises the same seam with an injected fake (``tests/test_routing.py``,
``tests/test_laya_model.py``).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.llm


def test_the_real_checkpoint_answers_in_the_engines_vocabulary():
    pytest.importorskip("laya")

    from agents import laya, laya_model

    backend = laya_model.Backend(laya_model._load_router())
    verdict = backend.classify("add a login endpoint")

    assert verdict is not None, "the checkpoint answered off-contract"
    assert verdict.intent in (laya.INTENT_ADMIN, laya.INTENT_CODE)
    assert verdict.domain in {
        laya.DOMAIN_UI, laya.DOMAIN_API, laya.DOMAIN_DB, laya.DOMAIN_TESTS,
        laya.DOMAIN_DOCS, laya.DOMAIN_CORE, laya.DOMAIN_GENERAL,
    }
    assert 0.0 <= verdict.confidence <= 1.0
