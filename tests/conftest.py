"""Shared pytest fixtures.

Keep the mirror OFF by default for the whole suite. The mirror activates
purely from environment variables, so if the developer running the tests
has CONTEXT_KEEPER_REMOTE_URL exported (they are setting up mirroring!),
record_* would try to hit a real remote and next_id would append the
Option-B collision suffix, breaking tests that assert bare ids. This
autouse fixture clears those vars before every test; test_mirror.py sets
them explicitly via its own fixtures, which run after this one.

Same idea for the Xylem session pointer -- see _hermetic_xylem_pointer.
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_mirror_env(monkeypatch):
    monkeypatch.delenv("CONTEXT_KEEPER_REMOTE_URL", raising=False)
    monkeypatch.delenv("CONTEXT_KEEPER_REMOTE_TIMEOUT", raising=False)
    yield


@pytest.fixture(autouse=True)
def _hermetic_xylem_pointer(tmp_path_factory, monkeypatch):
    """Point the Xylem session pointer at a path that does not exist.

    _resolve_project_dir consults ~/.xylem/active_project.json at step 1.5,
    ABOVE the cwd/ancestor checks. On a developer machine that actually runs
    the Xylem suite, that file exists and names whatever project the last
    SessionStart hook saw -- so TestProjectResolution's cwd/walk-up tests
    resolved to that real project instead of tmp_path and failed, even
    though CI (where the file is absent) was green. A test that passes only
    on machines without the product installed is not a test.

    Redirecting the override env var to a nonexistent file makes the pointer
    reliably absent for every test, matching CI. Tests that WANT the pointer
    (TestXylemSessionProject) monkeypatch the same var in the test body,
    which runs after this fixture and therefore wins.
    """
    absent = tmp_path_factory.mktemp("xylem_absent") / "no_active_project.json"
    monkeypatch.setenv("XYLEM_ACTIVE_PROJECT_FILE", str(absent))
    yield
