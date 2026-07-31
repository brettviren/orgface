# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""End to end tests against a real Emacs daemon.

These exercise the parts that cannot be reasoned about safely in isolation:
how many rounds of expansion an org-roam capture body actually goes
through, and whether the daemon really starts, answers, and expires.

Everything runs against a throwaway org-roam directory and an init file
built here, so the user's own notes and Emacs configuration are untouched.
Tests skip when Emacs or org-roam cannot be found.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from org_cli_helper import elisp
from org_cli_helper.config import Config
from org_cli_helper.emacs import Emacs
from org_cli_helper.templates import Template, fill

pytestmark = pytest.mark.emacs

# Directories that hold byte-compiled packages for the common installers.
_PACKAGE_GLOBS = (
    ".local/state/emacs/straight/build",
    ".config/emacs/straight/build",
    ".emacs.d/straight/build",
    ".config/emacs/elpa",
    ".emacs.d/elpa",
)

TEMPLATE_BODY = "* %^{Title}\n%T\n%?"


def _find_org_roam() -> list[Path] | None:
    """Locate org-roam and put its whole package directory on the path."""
    home = Path.home()
    for relative in _PACKAGE_GLOBS:
        base = home / relative
        if not base.is_dir():
            continue
        for candidate in base.glob("org-roam*"):
            if (candidate / "org-roam-dailies.el").exists() or (
                candidate / "org-roam.el"
            ).exists():
                return sorted(p for p in base.iterdir() if p.is_dir())
    return None


@pytest.fixture(scope="module")
def load_path() -> list[Path]:
    if shutil.which("emacs") is None or shutil.which("emacsclient") is None:
        pytest.skip("emacs and emacsclient are required")
    found = _find_org_roam()
    if not found:
        pytest.skip("org-roam is not installed anywhere this test looks")
    return found


@pytest.fixture
def workspace(load_path, tmp_path):
    """An isolated org-roam tree plus an `emacs` wrapper that boots it."""
    roam = tmp_path / "roam"
    (roam / "daily").mkdir(parents=True)
    emacsd = tmp_path / "emacsd"
    emacsd.mkdir()

    quote = elisp.quote_string
    paths = "\n".join(f"  (add-to-list 'load-path {quote(str(p))})" for p in load_path)
    head = quote("#+title: %<%Y-%m-%d>\n")
    init = tmp_path / "init.el"
    init.write_text(
        f"""\
(progn
{paths}
)
(setq user-emacs-directory {quote(str(emacsd) + "/")})
(require 'org-roam)
(require 'org-roam-dailies)
(setq org-roam-directory {quote(str(roam) + "/")}
      org-roam-db-location {quote(str(emacsd / "org-roam.db"))}
      org-id-locations-file {quote(str(emacsd / ".org-id-locations"))}
      org-roam-dailies-directory "daily/")
(setq org-roam-dailies-capture-templates
      (list (list "d" "default" 'entry {quote(TEMPLATE_BODY)}
                  :target (list 'file+head "%<%Y-%m-%d>.org" {head}))
            (list "p" "plain" 'entry {quote("* %?")}
                  :target (list 'file+head "%<%Y-%m-%d>.org" {head}))))
(org-roam-db-autosync-mode)
""",
        encoding="utf-8",
    )

    wrapper = tmp_path / "emacs-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\nexec {shutil.which("emacs")} -Q -l "{init}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    # AF_UNIX paths cap out near 108 bytes, so keep the socket directory short.
    runtime = Path(tempfile.mkdtemp(prefix="ochl", dir="/tmp"))
    previous = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)

    server = f"org-cli-test-{os.getpid()}"
    config = Config(
        server_name=server,
        lifetime=600,
        emacs=str(wrapper),
        emacsclient="emacsclient",
        start_timeout=180,
    )
    emacs = Emacs(config)
    try:
        yield emacs, roam / "daily"
    finally:
        try:
            emacs.stop()
        except Exception:
            pass
        if previous is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = previous
        shutil.rmtree(runtime, ignore_errors=True)


def _only_note(daily: Path) -> str:
    notes = sorted(daily.glob("*.org"))
    assert len(notes) == 1, f"expected one daily note, found {notes}"
    return notes[0].read_text(encoding="utf-8")


class TestDaemon:
    def test_starts_on_demand_and_reports_itself(self, workspace):
        emacs, _ = workspace
        assert emacs.is_running() is False
        info = emacs.info()
        assert emacs.is_running() is True
        assert [t["key"] for t in info["templates"]] == ["d", "p"]
        assert info["dailies_directory"] == "daily/"

    def test_reports_the_template_body_from_org_roam_syntax(self, workspace):
        """org-roam keeps the body at index 3, not org-capture's index 4."""
        emacs, _ = workspace
        info = emacs.info()
        default = info["templates"][0]
        assert default["body_kind"] == "string"
        assert default["body"] == TEMPLATE_BODY

    def test_stop_is_idempotent(self, workspace):
        emacs, _ = workspace
        emacs.info()
        assert emacs.stop() is True
        assert emacs.stop() is False


class TestCommit:
    def test_honours_the_configured_template(self, workspace):
        emacs, daily = workspace
        template = Template("d", "default", "entry", TEMPLATE_BODY)
        filled = fill(template, "Fixed the thing", "Took all day.")
        emacs.commit("d", filled.text)

        note = _only_note(daily)
        assert "#+title:" in note
        assert ":ID:" in note  # org-roam registered the node
        assert "* Fixed the thing" in note
        assert "Took all day." in note

    def test_appends_a_second_entry_to_the_same_note(self, workspace):
        emacs, daily = workspace
        template = Template("d", "default", "entry", TEMPLATE_BODY)
        emacs.commit("d", fill(template, "First", "one").text)
        emacs.commit("d", fill(template, "Second", "two").text)

        note = _only_note(daily)
        assert note.count("\n* ") + note.startswith("* ") >= 2
        assert "* First" in note and "* Second" in note

    def test_template_timestamp_is_expanded(self, workspace):
        emacs, daily = workspace
        template = Template("d", "default", "entry", TEMPLATE_BODY)
        emacs.commit("d", fill(template, "Stamped", "").text)
        note = _only_note(daily)
        assert "%T" not in note
        assert time.strftime("<%Y-%m-%d") in note

    def test_plain_template_puts_the_title_in_the_heading(self, workspace):
        emacs, daily = workspace
        template = Template("p", "plain", "entry", "* %?")
        emacs.commit("p", fill(template, "Just a title", "And a body.").text)
        note = _only_note(daily)
        assert "* Just a title" in note
        assert "And a body." in note


class TestEscaping:
    """The reason the escape arithmetic is what it is."""

    @pytest.mark.parametrize(
        "text",
        [
            "100% done, 50% left",
            "Literal %t stays",
            "Time %<%Y-%m-%d> stays",
            "Elisp %(shell) stays",
            "Prompt %^{Mood} stays",
            "Link %:annotation stays",
            "Numbered %\\1 stays",
            "File %[/etc/passwd] stays",
            r"Windows C:\path stays",
            r"Escaped \%t stays",
            "Trailing percent 100%",
            "Mixed %t and 50% and %(x) together",
        ],
    )
    def test_user_text_survives_verbatim(self, workspace, text):
        emacs, daily = workspace
        template = Template("d", "default", "entry", TEMPLATE_BODY)
        emacs.commit("d", fill(template, "Title", text).text)
        assert text in _only_note(daily)

    def test_embedded_elisp_is_not_evaluated(self, workspace):
        """%(...) in a message must never reach org-eval."""
        emacs, daily = workspace
        template = Template("d", "default", "entry", TEMPLATE_BODY)
        emacs.commit(
            "d", fill(template, "Title", '%(shell) and %(find-file "/etc/passwd")').text
        )

        assert '%(find-file "/etc/passwd")' in _only_note(daily)
        buffers = emacs.eval("(mapcar #'buffer-name (buffer-list))")
        assert "*shell*" not in buffers
        assert "passwd" not in buffers

    def test_title_is_escaped_too(self, workspace):
        emacs, daily = workspace
        template = Template("d", "default", "entry", TEMPLATE_BODY)
        emacs.commit("d", fill(template, "Title with %t and 100%", "body").text)
        assert "* Title with %t and 100%" in _only_note(daily)


class TestExpiry:
    def test_the_daemon_shuts_itself_down_when_idle(self, workspace):
        """The ssh-style master control has to let go on its own."""
        emacs, _ = workspace
        emacs.config = Config(
            server_name=emacs.config.server_name,
            lifetime=2,
            emacs=emacs.config.emacs,
            emacsclient=emacs.config.emacsclient,
            start_timeout=emacs.config.start_timeout,
        )
        emacs.info()
        assert emacs.is_running() is True

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not emacs.is_running():
                break
        assert emacs.is_running() is False

    def test_each_request_resets_the_countdown(self, workspace):
        emacs, _ = workspace
        emacs.config = Config(
            server_name=emacs.config.server_name,
            lifetime=4,
            emacs=emacs.config.emacs,
            emacsclient=emacs.config.emacsclient,
            start_timeout=emacs.config.start_timeout,
        )
        emacs.info()
        for _ in range(6):
            time.sleep(1)
            assert emacs.eval("t").strip() == "t"
        assert emacs.is_running() is True
