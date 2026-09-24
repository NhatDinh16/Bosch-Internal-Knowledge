# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""jarvis.auth.store: the encrypted SQLite token cache.

``cache`` is the public module (get / put / delete / list_tokens); token values
are encrypted at rest by the private ``_encryption`` module.
"""
