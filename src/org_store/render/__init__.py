# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Writing the canonical model back out as org text.

One Jinja2 template per node type, in ``templates/``, dispatched by node
``type``.  A type with no template of its own falls back to
``fallback.org.j2``, which prints whatever text the node holds -- so an
unrecognised construct comes out looking like itself rather than
disappearing.

The split between this module and the templates is the same one the rest
of the package uses: everything that has to be *worked out* -- a
headline's stars, its tag column, which children are blocks -- is computed
here and handed over, and the templates only decide layout.  A template
that had to reach into the model would be a data-processing module in
disguise.

Whitespace has one rule, and every template obeys it: a block's rendering
ends with exactly one newline, and an inline's ends with none.  Blank
lines between blocks are inserted by whatever contains them, so no
template has to guess what follows it.
"""

from __future__ import annotations

from .renderer import OrgRenderer, render_document, render_node

__all__ = ["OrgRenderer", "render_document", "render_node"]
