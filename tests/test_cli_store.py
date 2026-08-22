# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org store`` end to end.

The store needs no Emacs, so these run the real command against a real
sqlite file in a temporary directory, pointed there by the same
environment variable a user would set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_cli_helper.cli import main

DATA = Path(__file__).parent / "data"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the store at a scratch database, as configuration would."""
    database = tmp_path / "store.sqlite"
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_DATABASE", str(database))
    return database


@pytest.fixture
def source(tmp_path) -> Path:
    target = tmp_path / "fixture.json"
    target.write_text((DATA / "fixture.ox-json.json").read_text())
    return target


def add(runner, source: Path, *args: str) -> str:
    result = runner.invoke(main, ["store", "add", str(source), *args])
    assert result.exit_code == 0, result.output
    return result.output.strip()


def test_help_without_arguments(runner, env) -> None:
    """No arguments prints help, as every other group in this app does."""
    result = runner.invoke(main, ["store"])
    assert "Create, read, update and delete" in result.output
    assert result.exit_code == runner.invoke(main, ["config"]).exit_code


def test_subcommands_help_without_arguments(runner, env) -> None:
    for name in ("add", "show", "rm", "node", "rm-node"):
        result = runner.invoke(main, ["store", name])
        assert "Usage:" in result.output, name


def test_help_flag_is_accepted_everywhere(runner, env) -> None:
    for args in (["store"], ["store", "add"], ["store", "list"]):
        result = runner.invoke(main, [*args, "-h"])
        assert result.exit_code == 0, args
        assert "Usage:" in result.output, args


def test_add_creates_the_database(runner, env, source) -> None:
    doc_id = add(runner, source, "--id", "fixture.org")
    assert doc_id == "fixture.org"
    assert env.exists()


def test_add_reads_an_org_file(runner, env, tmp_path) -> None:
    """The whole point of the org-file path: no Emacs, no JSON."""
    source = tmp_path / "notes.org"
    source.write_text((DATA / "fixture.org").read_text())
    result = runner.invoke(main, ["store", "add", str(source)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "notes.org"

    shown = runner.invoke(main, ["store", "show", "notes.org"])
    assert "Canonical model fixture" in shown.output
    assert "54 nodes" in shown.output


def test_org_and_json_sources_agree(runner, env, tmp_path) -> None:
    """The two ingestion paths must land on the same document."""
    org = tmp_path / "a.org"
    org.write_text((DATA / "fixture.org").read_text())
    js = tmp_path / "b.json"
    js.write_text((DATA / "fixture.ox-json.json").read_text())
    runner.invoke(main, ["store", "add", str(org), "--id", "from-org"])
    runner.invoke(main, ["store", "add", str(js), "--id", "from-json"])

    def summary(doc_id):
        out = runner.invoke(main, ["store", "show", doc_id]).output
        return [line for line in out.splitlines() if "nodes," in line]

    assert summary("from-org") == summary("from-json")


def test_add_rejects_json_that_is_not_a_document(runner, env, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"contents": [{"$$data_type": "org-node"}]}')
    result = runner.invoke(main, ["store", "add", str(bad)])
    assert result.exit_code != 0
    assert "not a valid org document" in result.output


def test_add_rejects_malformed_json(runner, env, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    result = runner.invoke(main, ["store", "add", str(bad)])
    assert result.exit_code != 0


def test_list_and_show(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")

    listed = runner.invoke(main, ["store", "list"])
    assert listed.exit_code == 0
    assert "fixture.org" in listed.output
    assert "Canonical model fixture" in listed.output

    shown = runner.invoke(main, ["store", "show", "fixture.org"])
    assert shown.exit_code == 0
    assert "Canonical model fixture" in shown.output
    assert "54 nodes" in shown.output


def test_list_when_empty(runner, env) -> None:
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "no documents" in result.output


def test_show_json_is_the_canonical_document(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    result = runner.invoke(main, ["store", "show", "fixture.org", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["$$data_type"] == "org-document"
    assert payload["schema-version"] == 1


def test_show_unknown_document(runner, env) -> None:
    result = runner.invoke(main, ["store", "show", "nope"])
    assert result.exit_code != 0
    assert "no such document" in result.output


def test_node_is_addressable(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    shown = runner.invoke(main, ["store", "show", "fixture.org", "--json"])
    ref = json.loads(shown.output)["contents"][0]["ref"]

    result = runner.invoke(main, ["store", "node", "fixture.org", ref])
    assert result.exit_code == 0
    assert ref in result.output


def test_node_json(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    shown = runner.invoke(main, ["store", "show", "fixture.org", "--json"])
    ref = json.loads(shown.output)["contents"][0]["ref"]

    result = runner.invoke(main, ["store", "node", "fixture.org", ref, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["ref"] == ref


def test_unknown_node(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    result = runner.invoke(main, ["store", "node", "fixture.org", "orgmissing"])
    assert result.exit_code != 0
    assert "no node" in result.output


def test_rm_node_warns_that_refs_move(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    shown = runner.invoke(main, ["store", "show", "fixture.org", "--json"])
    ref = json.loads(shown.output)["contents"][0]["ref"]

    result = runner.invoke(main, ["store", "rm-node", "fixture.org", ref])
    assert result.exit_code == 0
    assert "refs after it have changed" in result.output


def test_rm(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    result = runner.invoke(main, ["store", "rm", "fixture.org"])
    assert result.exit_code == 0

    listed = runner.invoke(main, ["store", "list"])
    assert "no documents" in listed.output


def test_reindex(runner, env, source) -> None:
    add(runner, source, "--id", "fixture.org")
    result = runner.invoke(main, ["store", "reindex"])
    assert result.exit_code == 0
    assert "reindexed 1 document" in result.output


def test_unknown_backend_is_reported(runner, env, monkeypatch, source) -> None:
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_BACKEND", "postgres")
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code != 0
    assert "unknown store backend" in result.output


def test_store_settings_are_configurable(runner, tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    database = tmp_path / "configured.sqlite"
    config.write_text(f'[store]\ndatabase = "{database}"\n')
    result = runner.invoke(main, ["-c", str(config), "store", "list"])
    assert result.exit_code == 0
    assert "no documents" in result.output


def test_the_org_file_backend_is_selectable(runner, tmp_path, monkeypatch) -> None:
    """Same commands, files on disk instead of a database."""
    root = tmp_path / "notes"
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_DIRECTORY", str(root))

    source = tmp_path / "fixture.org"
    source.write_text((DATA / "fixture.org").read_text())
    added = runner.invoke(main, ["store", "add", str(source), "--id", "kept.org"])
    assert added.exit_code == 0, added.output

    assert (root / "kept.org").is_file()
    assert "#+title: Canonical model fixture" in (root / "kept.org").read_text()

    listed = runner.invoke(main, ["store", "list"])
    assert "kept.org" in listed.output


def test_the_org_file_backend_has_no_search(runner, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_DIRECTORY", str(tmp_path / "notes"))
    result = runner.invoke(main, ["search", "anything"])
    assert result.exit_code != 0
    assert "no search index" in result.output


def test_a_hand_written_file_is_already_in_the_store(
    runner, tmp_path, monkeypatch
) -> None:
    """Nothing has to be imported: the files are the storage."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "mine.org").write_text("#+title: Written by hand\n\n* A heading\n")
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_DIRECTORY", str(root))

    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "Written by hand" in result.output


@pytest.fixture
def paired(runner, tmp_path, monkeypatch, source):
    """A sqlite store holding one document, and somewhere to sync it."""
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_DATABASE", str(tmp_path / "s.sqlite"))
    result = runner.invoke(main, ["store", "add", str(source), "--id", "kept.org"])
    assert result.exit_code == 0, result.output
    return tmp_path / "elsewhere"


def test_sync_help_without_arguments(runner, env) -> None:
    result = runner.invoke(main, ["store", "sync"])
    assert "Copy documents between" in result.output


def test_sync_creates_at_the_destination(runner, paired) -> None:
    result = runner.invoke(main, ["store", "sync", f"orgfs:{paired}"])
    assert result.exit_code == 0, result.output
    assert "+ kept.org" in result.output
    assert (paired / "kept.org").is_file()


def test_sync_is_idempotent(runner, paired) -> None:
    runner.invoke(main, ["store", "sync", f"orgfs:{paired}"])
    again = runner.invoke(main, ["store", "sync", f"orgfs:{paired}"])
    assert again.exit_code == 0
    assert "1 unchanged" in again.output


def test_sync_dry_run_writes_nothing(runner, paired) -> None:
    result = runner.invoke(
        main, ["store", "sync", f"orgfs:{paired}", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "dry run" in result.output
    assert not paired.exists() or not list(paired.glob("*.org"))


def test_sync_pull_brings_changes_back(runner, paired) -> None:
    runner.invoke(main, ["store", "sync", f"orgfs:{paired}"])
    path = paired / "kept.org"
    path.write_text(path.read_text() + "\n* Edited in the file\n")

    pulled = runner.invoke(main, ["store", "sync", f"orgfs:{paired}", "--pull"])
    assert pulled.exit_code == 0
    assert "~ kept.org" in pulled.output

    found = runner.invoke(main, ["search", "Edited in the file"])
    assert found.exit_code == 0, found.output


def test_sync_reports_the_direction(runner, paired) -> None:
    pushed = runner.invoke(main, ["store", "sync", f"orgfs:{paired}"])
    assert pushed.output.splitlines()[0].startswith("sqlite:")
    pulled = runner.invoke(main, ["store", "sync", f"orgfs:{paired}", "--pull"])
    assert pulled.output.splitlines()[0].startswith("orgfs:")


def test_sync_prune(runner, paired) -> None:
    runner.invoke(main, ["store", "sync", f"orgfs:{paired}"])
    (paired / "extra.org").write_text("#+title: Extra\n")
    result = runner.invoke(main, ["store", "sync", f"orgfs:{paired}", "--prune"])
    assert "- extra.org" in result.output
    assert not (paired / "extra.org").exists()


def test_sync_rejects_an_unknown_backend(runner, paired) -> None:
    result = runner.invoke(main, ["store", "sync", "postgres:/tmp/x"])
    assert result.exit_code != 0
    assert "unknown store backend" in result.output
