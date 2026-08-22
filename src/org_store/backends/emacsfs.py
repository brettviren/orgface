# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A store backend that reads org files through Emacs.

Same files as :mod:`org_store.backends.orgfs`, read by a different
engine.  org-element *is* the definition of what org means, and it runs
inside the person's own configuration -- their link types, their TODO
keywords, their in-buffer settings -- so this is the only reader that can
promise to agree with what they see in Emacs.  It also sees a buffer's
unsaved edits, which nothing reading the file can.

The price is a running Emacs with ``ox-json`` installed.  Where that is
not wanted, :mod:`~org_store.backends.orgfs` reads the same directory
with no Emacs at all, and the two are checked against each other by the
conformance tests.

Writing does not go through Emacs.  The renderer already produces org
text, and an Emacs-side edit would need a second implementation of
everything :mod:`org_store.render` does; what Emacs is asked instead is
whether it is holding the file, so that unsaved work is never quietly
overwritten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..model import OrgDocument
from ..store import DocId, StoreError
from .orgfs import OrgDirectoryStore


@runtime_checkable
class OrgExporter(Protocol):
    """What this backend needs from an Emacs.

    A port rather than an import: :mod:`org_store` is a library the CLI
    uses, not the other way round, so it declares what it needs and the
    CLI supplies something that does it.  ``org_cli_helper.emacs.Emacs``
    satisfies this as it stands.
    """

    def export_json(self, path: Path) -> dict[str, Any]:
        """The org file at PATH, as canonical JSON."""
        ...

    def buffer_state(self, path: Path) -> dict[str, Any]:
        """Whether Emacs holds PATH, and whether it has unsaved changes."""
        ...

    def revert(self, path: Path) -> dict[str, Any]:
        """Reload PATH into its Emacs buffer, if there is one."""
        ...


class EmacsDirectoryStore(OrgDirectoryStore):
    """Org files in a directory, read through a running Emacs.

    Everything about *where* documents live is inherited: these are the
    same files, named the same way.  Only how one is read, and what is
    checked before writing, is different.
    """

    def __init__(self, root: Path | str, emacs: OrgExporter) -> None:
        super().__init__(root)
        self.emacs = emacs

    def read_document(self, doc_id: DocId) -> OrgDocument:
        """Read a document as Emacs currently sees it.

        Validated here, at the boundary, which makes every read a live
        conformance check against real org-element behaviour -- including
        whatever is peculiar to this person's configuration.
        """
        path = self._existing(doc_id)
        return OrgDocument.model_validate(self.emacs.export_json(path))

    def replace_document(self, doc_id: DocId, doc: OrgDocument) -> None:
        """Rewrite the file, refusing to discard unsaved Emacs edits.

        The store is one writer among several here, and the others
        include a person with the file open.  Overwriting a modified
        buffer would destroy work that Emacs has and the file does not,
        so it is refused rather than resolved: a merge this code invented
        would be a worse answer than a clear stop.

        An unmodified buffer is safe to write under, but Emacs would go
        on showing the old text, so it is reverted afterwards.
        """
        path = self._existing(doc_id)
        state = self.emacs.buffer_state(path)
        if state.get("modified"):
            raise StoreError(
                f"{doc_id} has unsaved changes in Emacs; save or revert the "
                "buffer before writing to it from here"
            )
        super().replace_document(doc_id, doc)
        if state.get("visiting"):
            self.emacs.revert(path)

    def delete_document(self, doc_id: DocId) -> None:
        """Delete the file, refusing while Emacs holds unsaved changes."""
        path = self._existing(doc_id)
        if self.emacs.buffer_state(path).get("modified"):
            raise StoreError(
                f"{doc_id} has unsaved changes in Emacs; not deleting it"
            )
        super().delete_document(doc_id)
