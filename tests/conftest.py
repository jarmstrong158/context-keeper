"""Shared pytest fixtures.

Keep the mirror OFF by default for the whole suite. The mirror activates
purely from environment variables, so if the developer running the tests
has CONTEXT_KEEPER_REMOTE_URL exported (they are setting up mirroring!),
record_* would try to hit a real remote and next_id would append the
Option-B collision suffix, breaking tests that assert bare ids. This
autouse fixture clears those vars before every test; test_mirror.py sets
them explicitly via its own fixtures, which run after this one.
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_mirror_env(monkeypatch):
    monkeypatch.delenv("CONTEXT_KEEPER_REMOTE_URL", raising=False)
    monkeypatch.delenv("CONTEXT_KEEPER_REMOTE_TIMEOUT", raising=False)
    yield
