# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Ways of getting org content into the canonical model.

Each adapter here turns some other representation into an
:class:`~org_store.model.OrgDocument`.  They are separate from the store
backends: a backend decides where documents live, an adapter decides how
one is read.  The org-file backend uses the ``org-parser`` adapter, but
nothing stops another caller using it on text from anywhere.
"""
