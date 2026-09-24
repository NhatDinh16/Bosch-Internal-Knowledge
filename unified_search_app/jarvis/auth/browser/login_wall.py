# SPDX-FileCopyrightText: 2025-2026 ETAS GmbH
# SPDX-License-Identifier: LicenseRef-BISL-1.0
# Author: Timm Korte <Timm.Korte@etas.com>
"""Recognise a login wall on a rendered page and start its single-sign-on flow.

A login wall is the sign-in page an application shows in place of its content.
Recognising it by URL alone does not work: a single-page app renders the wall on
the *content* URL, so a caller that only compares URLs believes it reached the
page it asked for and hands the wall back as if it were content.

This module therefore judges the live DOM. One rule:

    A page is a login wall when it shows a way to authenticate - either an **SSO
    kickoff control** or a **credentials form**.

Both signals are structural, deliberately not "the page looks short". Visible text
length reads like a good proxy for "this is a wall, not an article" and is not one:
measured against real Bosch portals, content pages come in at 666 and 1057
characters while a video page comes in at 2126, so any threshold sits in the middle
of the content distribution rather than below it. What actually separates the two
is that a content page of a signed-in portal offers no way to sign in.

- The **SSO kickoff control** is the link or button that hands the browser to the
  corporate identity provider. Only this one can be acted on.
- A **credentials form** is a visible password input inside a form that also holds
  a visible identifier input. Both halves are required: a settings page's
  change-password form has three password fields and no identifier, so it is not
  mistaken for a sign-in form.

The kickoff control is matched in DOM order, first hit wins, on either signal:

- ``SSO_HREF_PATTERNS`` - the federation-protocol endpoint the link points at,
  matched against the resolved **path** of a **same-origin** href. Protocol paths
  are brand-independent and by far the most reliable signal. Off-site links are
  never clicked: following one would navigate away from the page the caller asked
  for, using the user's live session.
- ``SSO_TEXT_PATTERNS`` - the visible label, for controls that post a form or
  route client-side and so carry no telling href. These are deliberately
  brand-qualified: a bare "Continue with" or "Sign in with" also fits "Continue
  with Google" on a multi-provider chooser, which is not the control we want.

Only visible elements count, so a sign-in modal an app parks in the DOM of an
authenticated page is ignored.

**Extending:** add the pattern to the matching tuple. Nothing else knows these
shapes. Each tuple is joined into one case-insensitive alternation - and the href
one gets ``_HREF_TAIL`` appended, which supplies the end-anchor - so an entry is a
plain regex fragment: no anchors, no flags, no capture-group assumptions.
The href and text fragments are compiled by the browser's own ``RegExp``, so they
must stay inside a JavaScript-compatible subset: no ``\\b``/``\\w``/``\\B``, no
Python-only group syntax. ``test_login_wall.py`` enforces that.

**Known gaps**, all failing safe (the wall is not recognised, so the caller hands
back the sign-in page rather than acting on the wrong thing): a kickoff control
labelled only "Login" or "Anmelden" with no telling href - too common on content
pages to match without clicking the wrong thing - or one that is an unlabelled
icon, and a wall inside an iframe or a shadow root.

Credentials are never typed here. Bosch sign-in is Kerberos/WIA or federated, so
the only action this module takes is clicking the kickoff control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Federation-protocol endpoints an SSO kickoff link points at, matched against the
# resolved path of a same-origin href. Each has to END the path (bar a known
# initiator verb, see _HREF_TAIL), so a feature page that merely lives under the
# same word - "/sso/status", "/products/sso-pricing" - is not taken for the endpoint.
SSO_HREF_PATTERNS: tuple[str, ...] = (
    r"/saml",
    r"/sso",
    r"/shibboleth\.sso",
    r"/adfs/ls",
    r"/wsfed",
    r"/oauth2?/authorize",
    r"/(oidc|openid)",
    r"/auth/(azure|entra|microsoft|bosch)",
)
_HREF_TAIL = r"(/(login|signin|sign-in|init|start|redirect|authorize))?/?$"

# Visible labels of an SSO kickoff control, English and German. Every entry names
# the corporate identity provider: an unqualified "sign in with" or "continue
# with" would match a third-party button on a multi-provider chooser. "bosch"
# appears because this is the Bosch auth layer; another tenant adds its own brand.
SSO_TEXT_PATTERNS: tuple[str, ...] = (
    r"single.?sign.?on",
    r"(sign|log) ?in with (bosch|microsoft|azure|entra)",
    r"continue with (bosch|microsoft|azure|entra)",
    r"anmelden mit (bosch|microsoft|azure|entra)",
    r"bosch (account|azure|entra)",
    r"login for employees",
    r"login f(u|ue|ü)r mitarbeitende",
    r"mitarbeiteranmeldung",
)


def _alternation(patterns: tuple[str, ...]) -> str:
    return "(" + "|".join(patterns) + ")"


_SSO_HREF_ALTERNATION = _alternation(SSO_HREF_PATTERNS) + _HREF_TAIL
_SSO_TEXT_ALTERNATION = _alternation(SSO_TEXT_PATTERNS)

Reason = Literal["credentials-form", "sso-control"]


@dataclass(frozen=True)
class LoginWall:
    """A recognised login wall and, if present, the control that leaves it."""

    reason: Reason  # the affordance that identified it
    label: str = ""  # visible text of the SSO kickoff control
    href: str = ""  # its resolved href, when it has one

    @property
    def has_kickoff(self) -> bool:
        return bool(self.label or self.href)

    def __str__(self) -> str:
        target = f" -> {self.label or self.href}" if self.has_kickoff else ""
        return f"{self.reason}{target}"


# One DOM pass: find the kickoff control and note whether a credentials form is
# present. With `expect` set the control is CLICKED, but only if it is still the one
# the caller classified - the page may have re-rendered between the classifying pass
# and this one.
_PROBE_JS = """
(({hrefPattern, textPattern, expect}) => {
  const hrefRe = new RegExp(hrefPattern, 'i');
  const textRe = new RegExp(textPattern, 'i');
  const visible = el => el.checkVisibility
    ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
    : el.getClientRects().length > 0;
  const label = el => (el.innerText || el.textContent || el.value || '').trim();
  const sameOriginPath = el => {
    if (el.tagName !== 'A') return null;   // a button has no href to resolve
    try {
      const resolved = new URL(el.getAttribute('href'), document.baseURI);
      return resolved.origin === location.origin ? resolved.pathname : null;
    } catch (e) {
      return null;                          // javascript:, mailto:, malformed
    }
  };

  let target = null;
  for (const el of document.querySelectorAll('a[href], button, input[type=submit]')) {
    if (!visible(el)) continue;
    const path = sameOriginPath(el);
    if ((path && hrefRe.test(path)) || textRe.test(label(el))) { target = el; break; }
  }

  const kickoff = target
    ? {label: label(target).slice(0, 120), href: (sameOriginPath(target) || '')}
    : null;
  const matches = !!(kickoff && expect
    && kickoff.label === expect.label && kickoff.href === expect.href);
  if (matches) target.click();

  // A sign-in form asks who you are; a change-password form already knows.
  const credentials = [...document.querySelectorAll('input[type=password]')]
    .filter(visible)
    .some(pw => pw.form && [...pw.form.querySelectorAll(
      'input[type=email], input[type=text], input[type=tel], input:not([type])'
    )].some(visible));

  return {kickoff: kickoff, clicked: matches, credentials: credentials};
})
"""


def classify(probe: dict) -> LoginWall | None:
    """Apply the wall rule to one DOM probe. See the module docstring."""
    kickoff = probe.get("kickoff") or {}
    label, href = kickoff.get("label", ""), kickoff.get("href", "")
    if label or href:
        return LoginWall("sso-control", label, href)
    if probe.get("credentials"):
        return LoginWall("credentials-form")
    return None


async def _probe(page: Any, *, expect: dict | None = None) -> dict:
    return await page.evaluate(
        _PROBE_JS,
        {
            "hrefPattern": _SSO_HREF_ALTERNATION,
            "textPattern": _SSO_TEXT_ALTERNATION,
            "expect": expect,
        },
    )


def _log_probe_failure(exc: Exception) -> None:
    """A probe racing an in-flight navigation is benign; anything else is a bug.

    A malformed pattern reaches us as a RegExp SyntaxError from the page, which
    would otherwise silently disable detection on every single page.
    """
    message = str(exc)
    if "context was destroyed" in message or "navigation" in message.lower():
        logger.debug("login-wall probe skipped (navigation in flight): %s", message)
    else:
        logger.warning("login-wall probe failed: %s", message)


async def detect(page: Any) -> LoginWall | None:
    """Return the login wall *page* currently shows, or None. No side effects."""
    try:
        return classify(await _probe(page))
    except Exception as exc:
        _log_probe_failure(exc)
        return None


async def start_sso(page: Any, service: str = "") -> LoginWall | None:
    """Click the SSO kickoff control if *page* is a login wall that offers one.

    Returns the wall that was acted on, or None when the page is not a wall, the
    wall offers no kickoff control (an external-credentials-only form), or the
    control changed under us. The caller waits for the resulting navigation.

    The page is classified before anything is clicked, and the click pass only
    fires on the very control that was classified - the rules decide, not the mere
    presence of a matching link.
    """
    wall = await detect(page)
    if wall is None or not wall.has_kickoff:
        return None
    try:
        clicked = (await _probe(page, expect={"label": wall.label, "href": wall.href})).get(
            "clicked"
        )
    except Exception as exc:
        _log_probe_failure(exc)
        return None
    if not clicked:
        logger.debug("login-wall kickoff %r vanished before the click", wall.label)
        return None
    logger.info("[%s] Login wall (%s): clicked SSO kickoff", service or "auth", wall)
    return wall
