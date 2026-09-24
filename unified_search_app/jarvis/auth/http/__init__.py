# SPDX-FileCopyrightText: 2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
"""jarvis.auth.http: HTTP transport, error handling, cookies, and proxy support.

bearer      one-shot Bearer-token authentication and reauthentication
transport   requests.Session construction, SSPI/Negotiate proxy transport
errors      raise_for_status / describe_http_error
cookies     cookie-jar scoping and selection
proxy       corporate-proxy (Zscaler) bypass helpers
"""
