"""Pin the cooldown-interval literal in ClosureRefreshWorker's claim SQL.

Static, import-time assertion — no runtime spy is sufficient because
``str.replace`` produces the same final literal as embedding ``'60 seconds'``
directly, and Python's str is immutable so a runtime check after .replace()
would also pass on the buggy code. The only check that distinguishes the
buggy state from the fixed state is to assert the *template* constant
defined at module scope.
"""

from __future__ import annotations

from registry.workers import closure_refresh


def test_claim_batch_template_uses_literal_interval() -> None:
    """_CLAIM_BATCH_SQL must embed the cooldown as a SQL literal, not a placeholder."""
    template = closure_refresh._CLAIM_BATCH_SQL

    assert "interval '60 seconds'" in template, (
        f"expected literal interval '60 seconds' in template; got:\n{template}"
    )
    assert ":cooldown" not in template, (
        "placeholder :cooldown must not appear — it was a side-effect of the "
        f".replace() indirection that has been removed. Got:\n{template}"
    )
