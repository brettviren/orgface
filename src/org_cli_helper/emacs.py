# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lifecycle management for the Emacs daemon backing org-cli-helper.

Modelled on ssh's master control: the first command that needs Emacs starts
a daemon under a private server name, later commands reuse it, and the
daemon shuts itself down once it has been idle for the configured lifetime.
Each request resets that countdown from inside Emacs, so no supervising
process is required on this side.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import elisp
from .config import Config

BOOTSTRAP = Path(__file__).with_name("bootstrap.el")


class DaemonError(Exception):
    """The Emacs daemon could not be reached, started, or evaluated in."""


def _missing_daemon(stderr: str) -> bool:
    """True when emacsclient failed because no daemon is listening.

    Deliberately narrow: a starting a daemon cannot fix a socket path that
    is too long for AF_UNIX, or an Elisp error raised inside a daemon that
    answered us, so those must not look like absence.
    """
    lowered = stderr.lower()
    if "*error*" in lowered or "too long" in lowered:
        return False
    return any(
        hint in lowered
        for hint in (
            "can't find socket",
            "cannot find socket",
            "no socket",
            "connection refused",
            "failed to connect",
            "no such file or directory",
        )
    )


def _emacs_error(stderr: str) -> str:
    """Extract the Elisp error message emacsclient reported, if any."""
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("*ERROR*:"):
            return stripped[len("*ERROR*:") :].strip()
    return stderr.strip()


@dataclass
class Emacs:
    """A handle on the managed daemon."""

    config: Config

    # -- process plumbing ---------------------------------------------------

    def _require(self, program: str) -> str:
        found = shutil.which(program)
        if found is None:
            raise DaemonError(f"{program!r} not found on PATH")
        return found

    def _client_argv(self, *extra: str) -> list[str]:
        client = self._require(self.config.emacsclient)
        return [client, "-s", self.config.server_name, *extra]

    def _run(
        self, argv: Sequence[str], timeout: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            # Nonzero is routine here (no daemon yet, Elisp error), so the
            # caller inspects returncode rather than catching an exception.
            return subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DaemonError(f"timed out running {argv[0]}: {exc}") from exc
        except OSError as exc:
            raise DaemonError(f"could not run {argv[0]}: {exc}") from exc

    # -- daemon state -------------------------------------------------------

    def is_running(self) -> bool:
        result = self._run(self._client_argv("-e", "t"), timeout=30)
        return result.returncode == 0

    def start(self) -> None:
        """Start the daemon, waiting for it to become ready."""
        emacs = self._require(self.config.emacs)
        argv = [emacs, f"--daemon={self.config.server_name}"]
        result = self._run(argv, timeout=self.config.start_timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DaemonError(
                f"failed to start Emacs daemon "
                f"{self.config.server_name!r}\n{detail}"
            )

    def stop(self) -> bool:
        """Shut the daemon down; False if it was not running."""
        if not self.is_running():
            return False
        form = "(org-cli-helper-expire)" if self._bootstrapped() else "(kill-emacs)"
        # The daemon dies mid-request, so a nonzero exit here is expected.
        self._run(self._client_argv("-e", form), timeout=60)
        return True

    def _bootstrapped(self) -> bool:
        result = self._run(
            self._client_argv("-e", "(featurep 'org-cli-helper-bootstrap)"),
            timeout=30,
        )
        return result.returncode == 0 and result.stdout.strip() == "t"

    # -- evaluation ---------------------------------------------------------

    def _wrap(self, form: str) -> str:
        """Bootstrap on demand, reset the idle timer, then evaluate FORM."""
        load = elisp.quote_string(str(BOOTSTRAP))
        lifetime = "nil" if self.config.lifetime is None else str(self.config.lifetime)
        return (
            "(progn"
            f" (unless (featurep 'org-cli-helper-bootstrap) (load {load} nil t))"
            f" (org-cli-helper-touch {lifetime})"
            f" {form})"
        )

    def eval(self, form: str, autostart: bool = True) -> str:
        """Evaluate FORM in the daemon and return emacsclient's printed result."""
        if not BOOTSTRAP.exists():
            raise DaemonError(f"missing daemon support file: {BOOTSTRAP}")

        argv = self._client_argv("-e", self._wrap(form))
        result = self._run(argv, timeout=self.config.start_timeout)

        if result.returncode != 0 and autostart and _missing_daemon(result.stderr):
            self.start()
            result = self._run(argv, timeout=self.config.start_timeout)

        if result.returncode != 0:
            raise DaemonError(_emacs_error(result.stderr) or "emacsclient failed")
        return result.stdout

    def eval_json(self, form: str, autostart: bool = True) -> Any:
        """Evaluate FORM, decoding the base64 JSON payload it returns."""
        return elisp.decode_payload(self.eval(form, autostart=autostart))

    # -- operations ---------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """org-roam locations and dailies capture templates."""
        return self.eval_json("(org-cli-helper-info)")

    def commit(self, keys: str, body: str) -> dict[str, Any]:
        """Capture BODY into today's daily note via template KEYS."""
        return self.eval_json(elisp.call("org-cli-helper-commit", keys, body))

    def note(self, topic: str, body: str) -> dict[str, Any]:
        """Capture BODY into the org-roam topic TOPIC, creating it if new."""
        return self.eval_json(elisp.call("org-cli-helper-note", topic, body))

    def topic(self, title: str) -> dict[str, Any]:
        """Locate the org-roam node titled TITLE; ``file`` is None if absent."""
        return self.eval_json(elisp.call("org-cli-helper-topic", title))

    # -- the org store ------------------------------------------------------

    def export_json(self, path: Path) -> dict[str, Any]:
        """Export the org file at PATH through ox-json.

        The whole integration with ox-json is this one call: the daemon
        hands back the exporter's JSON over the base64 channel every other
        command already uses, so there is no second bridge to maintain.
        """
        return self.eval_json(
            elisp.call("org-cli-helper-export-json", str(path))
        )

    def export(self, path: Path, backend: str) -> str:
        """Export the org file at PATH with an org-export BACKEND.

        BACKEND is org's own name for one -- ``html``, ``latex`` -- and
        the daemon loads the matching ``ox-`` library, so this reaches
        every exporter the person has rather than a list fixed here.
        """
        return self.eval_json(
            elisp.call("org-cli-helper-export", str(path), backend)
        )

    def buffer_state(self, path: Path) -> dict[str, Any]:
        """Whether Emacs holds PATH, and whether it has unsaved changes."""
        return self.eval_json(
            elisp.call("org-cli-helper-buffer-state", str(path))
        )

    def revert(self, path: Path) -> dict[str, Any]:
        """Reload PATH into its Emacs buffer, if there is one."""
        return self.eval_json(elisp.call("org-cli-helper-revert", str(path)))

    def status(self) -> dict[str, Any]:
        """Liveness and identity of the daemon, without starting one."""
        if not self.is_running():
            return {"running": False, "server_name": self.config.server_name}
        pid = self.eval("(emacs-pid)", autostart=False).strip()
        return {
            "running": True,
            "server_name": self.config.server_name,
            "pid": int(pid) if pid.isdigit() else None,
            "lifetime": self.config.lifetime,
        }

    def capture_interactive(self, keys: str | None, gui: bool = False) -> None:
        """Hand the terminal (or a new frame) to an interactive dailies capture."""
        self._interactive(
            elisp.call("org-cli-helper-capture-interactive", keys or ""), gui=gui
        )

    def note_interactive(self, topic: str, body: str, gui: bool = False) -> None:
        """Hand the terminal (or a new frame) to an interactive topic note."""
        self._interactive(
            elisp.call("org-cli-helper-note-interactive", topic, body), gui=gui
        )

    def _interactive(self, form: str, gui: bool = False) -> None:
        """Evaluate FORM in a frame of its own, blocking until it is gone."""
        self._require(self.config.emacsclient)
        if not self.is_running():
            self.start()
        # Load the support file first: the interactive client below inherits
        # the terminal, so it must not also carry setup output.
        self.eval("t")

        argv = self._client_argv("-c" if gui else "-t", "-e", form)
        try:
            # Inherits this terminal: emacsclient -t blocks with a live frame
            # until the capture finalises and the frame is deleted.
            completed = subprocess.run(list(argv), check=False)
        except OSError as exc:
            raise DaemonError(f"could not run emacsclient: {exc}") from exc
        if completed.returncode != 0:
            raise DaemonError(
                f"interactive capture exited with status {completed.returncode}"
            )

    def visit(self, path: Path) -> None:
        """Open PATH in a new frame, without waiting for it to close."""
        self._require(self.config.emacsclient)
        if not self.is_running():
            self.start()
        argv = self._client_argv("-c", "-n", str(path))
        result = self._run(argv, timeout=self.config.start_timeout)
        if result.returncode != 0:
            raise DaemonError(_emacs_error(result.stderr) or "emacsclient failed")

    def socket_hint(self) -> str:
        """Where emacsclient will look for this daemon's socket."""
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            return str(Path(runtime) / "emacs" / self.config.server_name)
        tmp = os.environ.get("TMPDIR", "/tmp")
        return str(Path(tmp) / f"emacs{os.getuid()}" / self.config.server_name)
