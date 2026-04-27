"""Shared test fixtures."""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    """Temporary sqlite DB path that auto-cleans."""
    return str(tmp_path / "test_state.db")


@pytest.fixture
def fixed_now_ts():
    """Fixed timestamp for deterministic tests: 2026-04-27 12:00:00 UTC."""
    return 1777680000


@pytest.fixture
def fake_openrouter_url():
    """Llmstub URL set as env var for tests that need it."""
    url = "http://localhost:9999/v1"
    os.environ["OPENROUTER_BASE_URL"] = url
    yield url
    os.environ.pop("OPENROUTER_BASE_URL", None)
