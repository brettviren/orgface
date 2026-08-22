# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared fixtures for the store tests.

Everything here is real: real exporter output, a real sqlite database.
See ``tests/data/README.org`` for where the JSON came from.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from org_store.backends import SqliteStore
from org_store.model import OrgDocument
from org_store.service import OrgStore

DATA = Path(__file__).parent / "data"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Keep every test away from the developer's own org files.

    Configuration is layered environment -> file -> command line, so a
    real ``~/.config/org-cli-helper/config.toml`` beats the environment
    variables a test sets and would aim the store at the user's notes.
    Pointing XDG at a scratch tree, and dropping any ambient
    ``ORG_CLI_HELPER_*`` setting, makes a test read only what it says.
    """
    for variable in list(os.environ):
        if variable.startswith("ORG_CLI_HELPER_"):
            monkeypatch.delenv(variable, raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(scope="session")
def fixture_json() -> dict:
    return json.loads((DATA / "fixture.ox-json.json").read_text())


@pytest.fixture(scope="session")
def timestamps_json() -> dict:
    return json.loads((DATA / "fixture-timestamps.ox-json.json").read_text())


@pytest.fixture
def doc(fixture_json: dict) -> OrgDocument:
    return OrgDocument.model_validate(fixture_json)


@pytest.fixture
def backend(tmp_path: Path) -> SqliteStore:
    with SqliteStore(tmp_path / "store.sqlite") as opened:
        yield opened


@pytest.fixture
def store(backend: SqliteStore) -> OrgStore:
    return OrgStore(backend)


@pytest.fixture
def stored(store: OrgStore, fixture_json: dict) -> str:
    """A document already in the store; yields its id."""
    return store.create(fixture_json, path_hint="fixture.org")
