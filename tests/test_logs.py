# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Choosing where log records go, and how many of them."""

from __future__ import annotations

import logging

import pytest

from orgface import logs
from orgface.cli import main


@pytest.fixture(autouse=True)
def restore_logging():
    """Leave the root logger as the test found it."""
    root = logging.getLogger()
    level = root.level
    yield
    if logs._installed is not None:
        root.removeHandler(logs._installed)
        logs._installed.close()
        logs._installed = None
    root.setLevel(level)


def test_a_file_sink_gets_records_at_the_level(tmp_path) -> None:
    path = tmp_path / "deep" / "orgface.log"
    logs.setup(str(path), "debug")
    logging.getLogger("orgface.test").debug("hello %s", "there")
    logs._installed.flush()
    assert "DEBUG   orgface.test: hello there" in path.read_text()


def test_records_below_the_level_are_dropped(tmp_path) -> None:
    path = tmp_path / "orgface.log"
    logs.setup(str(path), "warning")
    logging.getLogger("orgface.test").info("quiet")
    logging.getLogger("orgface.test").warning("loud")
    logs._installed.flush()
    text = path.read_text()
    assert "loud" in text and "quiet" not in text


def test_streams_are_named(capsys) -> None:
    logs.setup("stdout", "info")
    logging.getLogger("orgface.test").info("to stdout")
    logs.setup("stderr", "info")
    logging.getLogger("orgface.test").info("to stderr")
    captured = capsys.readouterr()
    assert "to stdout" in captured.out and "to stdout" not in captured.err
    assert "to stderr" in captured.err


def test_setting_up_again_replaces_only_its_own_handler(tmp_path) -> None:
    other = logging.NullHandler()
    logging.getLogger().addHandler(other)
    try:
        logs.setup(str(tmp_path / "a.log"), "info")
        first = logs._installed
        logs.setup(str(tmp_path / "b.log"), "info")
        handlers = logging.getLogger().handlers
        assert first not in handlers and logs._installed in handlers
        assert other in handlers
    finally:
        logging.getLogger().removeHandler(other)


def test_an_unknown_level_is_refused() -> None:
    with pytest.raises(logs.LogError, match="unknown log level"):
        logs.setup("stderr", "chatty")


def test_an_unwritable_sink_is_refused(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(logs.LogError, match="cannot log to"):
        logs.setup(str(blocker / "orgface.log"), "info")


def test_the_command_line_options(runner, tmp_path) -> None:
    path = tmp_path / "orgface.log"
    result = runner.invoke(
        main, ["-l", str(path), "-L", "DEBUG", "config", "paths"]
    )
    assert result.exit_code == 0, result.output
    logs._installed.flush()
    assert "orgface.cli: configuration from" in path.read_text()
    assert "DEBUG" not in result.output


def test_a_bad_level_on_the_command_line(runner) -> None:
    result = runner.invoke(main, ["-L", "chatty", "config", "paths"])
    assert result.exit_code != 0
    assert "chatty" in result.output
