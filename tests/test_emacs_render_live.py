# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""``org render --method emacs``, against a real daemon.

The exporter is org's own, so there is nothing here to check about *how*
it renders -- only that the right file reaches it, that what comes back
is the export, and that the CLI path works end to end.  A missing Emacs
skips, as everywhere else under this marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from org_cli_helper.cli import main
from org_cli_helper.config import Config
from org_cli_helper.emacs import DaemonError, Emacs
from org_store.adapters.orgparser import parse
from org_store.export import render
from org_store.export.emacs import OrgExportEngine

pytestmark = pytest.mark.emacs

DATA = Path(__file__).parent / "data"

SERVER = "org-cli-helper-render-test"

PAGE = "#+title: Rendered by Emacs\n\n* A heading\n\nSome *text* here.\n"


@pytest.fixture(scope="module")
def emacs() -> Emacs:
    """A daemon that can export HTML, or a skip saying why not."""
    client = Emacs(Config(server_name=SERVER, lifetime=120))
    try:
        client.eval("(emacs-version)")
    except DaemonError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no usable Emacs daemon: {exc}")
    yield client
    client.stop()


@pytest.fixture
def page(tmp_path) -> Path:
    path = tmp_path / "notes.org"
    path.write_text(PAGE, encoding="utf-8")
    return path


def test_emacs_satisfies_the_export_port(emacs: Emacs) -> None:
    assert isinstance(emacs, OrgExportEngine)


def test_exporting_a_file_gives_org_s_own_html(emacs: Emacs, page: Path) -> None:
    html = emacs.export(page, "html")
    assert "<html" in html
    assert "Rendered by Emacs" in html
    assert "<b>text</b>" in html or "<strong>text</strong>" in html


def test_the_method_exports_the_file_it_is_given(emacs: Emacs, page: Path) -> None:
    html = render(
        parse(page.read_text()),
        fmt="html",
        method="emacs",
        emacs=emacs,
        doc_id="notes.org",
        source=page,
    )
    assert "Rendered by Emacs" in html


def test_a_document_with_no_file_is_exported_too(emacs: Emacs) -> None:
    """A store that keeps no files still renders through Emacs."""
    html = render(
        parse(PAGE), fmt="html", method="emacs", emacs=emacs, doc_id="kept-in-sqlite"
    )
    assert "<html" in html
    assert "A heading" in html


def test_the_command_renders_through_emacs(tmp_path, monkeypatch) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "notes.org").write_text(PAGE, encoding="utf-8")
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_BACKEND", "orgfs")
    monkeypatch.setenv("ORG_CLI_HELPER_STORE_DIRECTORY", str(root))
    monkeypatch.setenv("ORG_CLI_HELPER_SERVER_NAME", SERVER)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["render", "notes.org", "-m", "emacs"])
    assert result.exit_code == 0, result.output
    assert "<html" in (tmp_path / "notes.html").read_text()


def test_an_unknown_exporter_is_reported_not_swallowed(
    emacs: Emacs, page: Path
) -> None:
    """A backend Emacs does not have must say so, not return nothing."""
    with pytest.raises(DaemonError):
        emacs.export(page, "nosuchbackend")
