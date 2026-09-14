"""No error code reaches a staff member's screen as a bare code.

Approving an item add in production showed «الإضافة مانفذتش store_permission».
The Arabic sentence for that code existed -- and nothing consulted it. The
mapping was wired into the two queue approve buttons only, while every other
error surface went through `toastError`, which rendered `err.message`; and
`ApiError` builds that from `body.detail || body.error`, so for every JSON
refusal this backend produces, `err.message` *is* the code. Twenty-six call
sites, one of them showing a word no staff member can act on and no
indication that a sentence saying what to do existed.

Two rules, and they are structural rather than a list anybody has to
remember:

1. Every `{"error": "..."}` code the dashboard API can answer with has an
   entry in `ERROR_REASONS`.
2. Nothing in the rendering path falls back to the code. An unmapped one
   becomes a readable sentence on screen and a `console.warn` in the log.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "dashboard" / "dashboard.html"

#: Where a staff-facing refusal can come from. `domain/services` and the
#: Shopify admin modules are included because the dashboard returns their
#: payloads verbatim -- `apply_add`'s `store_permission` reached the screen
#: through `JSONResponse(result)` without the dashboard ever naming it.
_SOURCES = (
    ROOT / "dashboard",
    ROOT / "domain" / "services",
    ROOT / "integrations" / "shopify",
)

#: Codes that never reach this dashboard's rendering path.
#: `unauthenticated` is mapped anyway (it is cheap and it is real), but these
#: are the ones raised only at surfaces staff never see.
_NOT_STAFF_FACING = {
    # The bot's own tool refusals: answered to the model in a tool result and
    # turned into Arabic by the model, never rendered by the dashboard.
    "no_product_in_context",
    "product_in_conversation",
    "garment_not_sold",
}

_ERROR_LITERAL = re.compile(r'"error":\s*"([a-z_]+)"')


def _mapped_codes() -> set[str]:
    """The keys of `ERROR_REASONS`, read out of the page."""
    page = PAGE.read_text(encoding="utf-8")
    block = re.search(r"const ERROR_REASONS = \{(.*?)\n\};", page, re.S)
    assert block, "ERROR_REASONS is gone from dashboard.html"
    return set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))


def _raised_codes() -> set[str]:
    found: set[str] = set()
    for base in _SOURCES:
        for path in base.rglob("*.py"):
            found.update(_ERROR_LITERAL.findall(path.read_text(encoding="utf-8")))
    return found - _NOT_STAFF_FACING


def test_every_error_code_the_api_can_return_has_a_sentence():
    """The one that would have caught `store_permission` on the screen."""
    missing = sorted(_raised_codes() - _mapped_codes())
    assert not missing, (
        f"{len(missing)} error code(s) can reach staff with no Arabic sentence, so they "
        "render as bare codes. Add each to ERROR_REASONS in dashboard/dashboard.html:\n"
        + "\n".join(f"  {code}" for code in missing)
    )


def test_the_fallback_is_a_sentence_and_never_the_code():
    """`errorText` must not reach for `err.message` or `err.code` as display
    text. That is precisely what `toastError` used to do."""
    page = PAGE.read_text(encoding="utf-8")
    body = re.search(r"function errorText\(err\) \{(.*?)\n\}", page, re.S)
    assert body, "errorText is gone from dashboard.html"
    source = body.group(1)
    # The code may be logged; it may not be returned.
    for line in source.splitlines():
        if "return" not in line:
            continue
        assert "err.message" not in line, f"errorText returns the raw message: {line.strip()}"
        assert "err.code" not in line, f"errorText returns the raw code: {line.strip()}"
    assert "console.warn" in source, "an unmapped code must still be logged somewhere"
    assert "UNKNOWN_REASON" in source, "there must be a readable fallback sentence"


def test_nothing_renders_err_message_directly():
    """The systemic half. `ApiError.message` is `body.detail || body.error`,
    so any surface that prints it prints a code."""
    # Block comments stripped first: the explanation of this very bug names
    # `err.message` several times, and a startswith() check does not survive a
    # wrapped comment line.
    page = re.sub(r"/\*.*?\*/", "", PAGE.read_text(encoding="utf-8"), flags=re.S)
    offenders = [
        line.strip()
        for line in page.splitlines()
        if "err.message" in line and not line.strip().startswith("//")
    ]
    assert not offenders, (
        "these lines put ApiError.message on screen, which is the raw error code:\n"
        + "\n".join(f"  {line}" for line in offenders)
    )


def test_every_error_surface_goes_through_the_resolver():
    """`toastError` and `errorBanner` are the two places an API failure is
    rendered. Both must resolve; a third one added later is caught by the
    test above, since it would have to reach for `err.message` to misbehave."""
    page = PAGE.read_text(encoding="utf-8")
    assert "const toastError = (err, what) => toast(what, errorText(err)" in page
    assert "esc(errorText(err))" in page, "errorBanner must resolve too"


@pytest.mark.parametrize(
    "code",
    ["store_permission", "store_refused", "insufficient_stock", "not_modifiable", "not_open"],
)
def test_the_codes_from_the_production_failures_are_mapped(code):
    """Named individually because each one was actually seen by somebody."""
    assert code in _mapped_codes()


def test_the_mapping_has_nothing_the_api_never_returns():
    """A sentence for a code that cannot happen is a sentence nobody
    maintains -- and it hides the fact that the real code is missing."""
    stale = sorted(_mapped_codes() - _raised_codes())
    assert not stale, f"ERROR_REASONS has entries no API returns: {stale}"
