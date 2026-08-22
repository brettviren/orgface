# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The committed JSON Schema must track the model that generates it."""

from __future__ import annotations

from org_store import schema
from org_store.model import SCHEMA_VERSION


def test_committed_schema_is_current() -> None:
    """Fails when the model changed but the schema was not regenerated.

    Run ``uv run python -m org_store.schema`` and review the diff.
    """
    assert schema.SCHEMA_FILE.exists(), f"missing {schema.SCHEMA_FILE}"
    committed = schema.SCHEMA_FILE.read_text(encoding="utf-8")
    assert committed == schema.rendered(), (
        "the committed JSON Schema is stale; regenerate it with "
        "'uv run python -m org_store.schema' and review the diff"
    )


def test_schema_identifies_its_version() -> None:
    generated = schema.generate()
    assert generated["$id"].endswith(
        f"v{SCHEMA_VERSION}/org-document.schema.json"
    )
    assert generated["$schema"].startswith("https://json-schema.org/")


def test_schema_describes_the_document_root() -> None:
    generated = schema.generate()
    assert generated["title"] == "OrgDocument"
    assert "$$data_type" in generated["properties"]
    assert "contents" in generated["properties"]


def test_schema_uses_wire_names() -> None:
    """Generated with aliases, so it describes the stored JSON."""
    headline = schema.generate()["$defs"]["HeadlineProperties"]["properties"]
    assert "raw-value" in headline
    assert "todo-keyword" in headline
    assert "raw_value" not in headline


def test_write_is_idempotent(tmp_path) -> None:
    target = tmp_path / "sub" / "org-document.schema.json"
    schema.write(target)
    first = target.read_text(encoding="utf-8")
    schema.write(target)
    assert target.read_text(encoding="utf-8") == first == schema.rendered()
