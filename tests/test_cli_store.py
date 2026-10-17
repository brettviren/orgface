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

from orgface.cli import main

DATA = Path(__file__).parent / "data"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the store at a scratch database, as configuration would."""
    database = tmp_path / "store.sqlite"
    monkeypatch.setenv("ORGFACE_STORE_DATABASE", str(database))
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
    doc_id = str(source.resolve())
    assert result.output.strip() == doc_id

    shown = runner.invoke(main, ["store", "show", doc_id])
    assert "Canonical model fixture" in shown.output
    assert "54 nodes" in shown.output


def test_add_takes_several_files_in_turn(runner, env, tmp_path) -> None:
    names = ("a.org", "b.org", "c.org")
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text((DATA / "fixture.org").read_text())
        paths.append(str(path.resolve()))
    result = runner.invoke(main, ["store", "add", *paths])
    assert result.exit_code == 0, result.output
    assert result.output.split() == paths
    listed = runner.invoke(main, ["store", "list"]).output
    assert all(path in listed for path in paths)


def test_same_name_in_different_directories_are_different(
    runner, env, tmp_path
) -> None:
    """The full path names a file, not its basename."""
    for sub in ("one", "two"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "notes.org").write_text(f"* {sub}\n")
    first = tmp_path / "one" / "notes.org"
    second = tmp_path / "two" / "notes.org"
    result = runner.invoke(
        main, ["store", "add", str(first), str(second)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output
    assert result.output.split() == [str(first.resolve()), str(second.resolve())]


def test_add_stops_at_the_first_bad_file(runner, env, tmp_path) -> None:
    good = tmp_path / "good.org"
    good.write_text((DATA / "fixture.org").read_text())
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    never = tmp_path / "never.org"
    never.write_text("* x\n")
    result = runner.invoke(main, ["store", "add", str(good), str(bad), str(never)])
    assert result.exit_code != 0
    listed = runner.invoke(main, ["store", "list"]).output
    assert "good.org" in listed
    assert "never.org" not in listed


def test_add_existing_is_a_warning_and_the_rest_carry_on(
    runner, env, tmp_path
) -> None:
    first, second = tmp_path / "first.org", tmp_path / "second.org"
    first.write_text("* x\n")
    second.write_text("* y\n")
    add(runner, first)
    result = runner.invoke(
        main, ["store", "add", str(first), str(second)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "warning:" in result.output and "already exists" in result.output
    assert "second.org" in result.output.splitlines()[-1]
    assert "second.org" in runner.invoke(main, ["store", "list"]).output


def test_add_existing_alone_is_still_just_a_warning(runner, env, tmp_path) -> None:
    only = tmp_path / "only.org"
    only.write_text("* x\n")
    add(runner, only)
    result = runner.invoke(main, ["store", "add", str(only)])
    assert result.exit_code == 0
    assert "warning:" in result.output


def test_add_id_needs_a_single_source(runner, env, tmp_path) -> None:
    one, two = tmp_path / "one.org", tmp_path / "two.org"
    one.write_text("* x\n")
    two.write_text("* y\n")
    result = runner.invoke(main, ["store", "add", str(one), str(two), "--id", "k"])
    assert result.exit_code != 0
    assert "--id" in result.output


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
    monkeypatch.setenv("ORGFACE_STORE_BACKEND", "postgres")
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
    monkeypatch.setenv("ORGFACE_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORGFACE_STORE_DIRECTORY", str(root))

    source = tmp_path / "fixture.org"
    source.write_text((DATA / "fixture.org").read_text())
    added = runner.invoke(main, ["store", "add", str(source), "--id", "kept.org"])
    assert added.exit_code == 0, added.output

    assert (root / "kept.org").is_file()
    assert "#+title: Canonical model fixture" in (root / "kept.org").read_text()

    listed = runner.invoke(main, ["store", "list"])
    assert "kept.org" in listed.output


def test_the_org_file_backend_has_no_search(runner, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORGFACE_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORGFACE_STORE_DIRECTORY", str(tmp_path / "notes"))
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
    monkeypatch.setenv("ORGFACE_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORGFACE_STORE_DIRECTORY", str(root))

    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "Written by hand" in result.output


@pytest.fixture
def paired(runner, tmp_path, monkeypatch, source):
    """A sqlite store holding one document, and somewhere to sync it."""
    monkeypatch.setenv("ORGFACE_STORE_DATABASE", str(tmp_path / "s.sqlite"))
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


# -- embedding --------------------------------------------------------------


@pytest.fixture
def services(tmp_path, service) -> Path:
    """A config naming an unreachable service then a working one, sharing
    an identity, and a second identity only the working server answers."""
    pytest.importorskip("sqlite_vec", reason="the semantic extra is not installed")
    url, _ = service
    path = tmp_path / "config.toml"
    path.write_text(
        f'[store]\ndatabase = "{tmp_path / "store.sqlite"}"\n'
        '[[embed]]\nurl = "http://127.0.0.1:9"\nmodel = "far"\n'
        'identity = "nomic"\ndimensions = 4\n'
        f'[[embed]]\nurl = "{url}"\nmodel = "near"\n'
        'identity = "nomic"\ndimensions = 4\n'
        f'[[embed]]\nurl = "{url}"\nmodel = "other"\ndimensions = 4\n',
        encoding="utf-8",
    )
    return path


def test_embed_uses_the_first_service_that_answers(
    runner, services, source, service
) -> None:
    _, seen = service
    runner.invoke(main, ["-c", str(services), "store", "add", str(source)])
    result = runner.invoke(main, ["-c", str(services), "store", "embed"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("nomic: embedded ")
    assert {request["model"] for request in seen} == {"near"}


def test_embed_by_identity(runner, services, source, service) -> None:
    _, seen = service
    runner.invoke(main, ["-c", str(services), "store", "add", str(source)])
    result = runner.invoke(
        main, ["-c", str(services), "store", "embed", "-i", "other"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("other: embedded ")
    assert {request["model"] for request in seen} == {"other"}


def test_embed_status_lists_vectors_and_services(
    runner, services, source
) -> None:
    config = ["-c", str(services)]
    runner.invoke(main, [*config, "store", "add", str(source)])
    runner.invoke(main, [*config, "store", "embed"])
    result = runner.invoke(main, [*config, "store", "embed", "status"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    nomic = next(line for line in lines if line.strip().startswith("nomic "))
    assert int(nomic.split()[-1]) > 0
    assert any(line.split() == ["other", "0"] for line in lines)
    assert "url:    http://127.0.0.1:9" in result.output
    assert "model:  far" in result.output
    assert result.output.count("status: ok") == 2
    assert "status: unavailable: no embedding model at http://127.0.0.1:9" in (
        result.output
    )


def test_search_needs_vectors_for_the_identity(runner, services, source) -> None:
    config = ["-c", str(services)]
    runner.invoke(main, [*config, "store", "add", str(source)])
    runner.invoke(main, [*config, "store", "embed"])
    found = runner.invoke(main, [*config, "search", "-s", "hello"])
    assert found.exit_code == 0, found.output
    missing = runner.invoke(main, [*config, "search", "-s", "-i", "other", "hello"])
    assert missing.exit_code != 0
    assert "no vectors for other" in missing.output
