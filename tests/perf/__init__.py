# Performance tests.
# All tests are marked @pytest.mark.perf and @pytest.mark.slow.
# Run gate: pytest tests/perf/ -m perf --timeout=300
# Requires a live Postgres container; skip in unit-only CI runs via -m "not perf".
