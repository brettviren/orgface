# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading secrets from pass(1), and using one as the embedding API key.

A small shell script stands in for ``pass`` so nothing depends on a real
password store or a GPG agent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from org_store.embed import EmbeddingError
from orgface.config import EmbedService, load
from orgface.passstore import PassError, lookup
from orgface.storage import embed_api_key, embedder


@pytest.fixture
def fake_pass(tmp_path: Path, monkeypatch) -> Path:
    """A ``pass`` on PATH that knows one entry and logs every call."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls"
    script = bindir / "pass"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{log}"\n'
        'case "$1" in\n'
        '  good) printf "sekrit\\nuser: me\\n" ;;\n'
        '  blank) printf "\\nnotes\\n" ;;\n'
        '  *) echo "Error: $1 is not in the password store." >&2; exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def test_lookup_returns_the_first_line(fake_pass) -> None:
    assert lookup("good") == "sekrit"


def test_lookup_reports_a_missing_entry(fake_pass) -> None:
    with pytest.raises(PassError, match="not in the password store"):
        lookup("absent")


def test_lookup_rejects_an_empty_first_line(fake_pass) -> None:
    with pytest.raises(PassError, match="empty"):
        lookup("blank")


def test_lookup_reports_a_missing_program(tmp_path) -> None:
    with pytest.raises(PassError, match="cannot run"):
        lookup("good", program=str(tmp_path / "no-such-pass"))


def test_an_explicit_api_key_wins(fake_pass) -> None:
    service = EmbedService(api_key="direct", api_key_passname="good")
    assert embed_api_key(service) == "direct"
    assert calls(fake_pass) == []


def test_no_passname_means_no_key(fake_pass) -> None:
    assert embed_api_key(EmbedService()) is None


def test_the_passname_is_read_only_when_needed(fake_pass) -> None:
    model = embedder(EmbedService(api_key_passname="good"))
    assert calls(fake_pass) == []
    assert model.api_key == "sekrit"
    assert model.api_key == "sekrit"
    assert calls(fake_pass) == ["good"]


def test_a_failed_lookup_is_an_embedding_error(fake_pass) -> None:
    with pytest.raises(EmbeddingError, match="embedding API key"):
        embedder(EmbedService(api_key_passname="absent")).api_key


def test_the_passname_comes_from_the_config_file(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[embed]\napi_key_passname = "some/entry"\n', encoding="utf-8")
    (service,) = load(path=path, environ={}).embed
    assert service.api_key_passname == "some/entry"
