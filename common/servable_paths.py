"""The local-file path guard, shared by everything that serves catalog files.

One implementation, two exposure levels. `SERVABLE_ROOTS` is what a logged-in
surface (the harness, the staff dashboard) may show a person; `PUBLIC_ROOTS`
is what may reach the public internet -- i.e. Meta's own fetcher, which has
no cookie and no token (`api/public_media.py`). `data/inbound` holds
customers' own photos and voice notes; it is servable to staff and must never
be reachable publicly.

This lives in /common/ because `api/public_media.py` (domain-adjacent) and
`assistant/media_serving.py` (assistant-layer) both need it, and neither
layer may import the other. `assistant/media_serving.py` re-exports these
names unchanged for its historical call sites.
"""

from __future__ import annotations

from pathlib import Path

from backend.config import PROJECT_ROOT

#: Only these roots may be served, so a crafted path cannot walk out of the
#: project. `data/inbound` is what a customer's own photo or voice note
#: downloads into; the other two are catalog assets.
SERVABLE_ROOTS = ("data/size-charts", "data/images", "data/inbound")

#: What may be served to the public internet -- i.e. to Meta's own fetcher,
#: which has no cookie and no token (`backend/public_media.py`). Deliberately
#: narrower than SERVABLE_ROOTS: `data/inbound` holds customers' own photos
#: and voice notes and must never be reachable without a login.
PUBLIC_ROOTS = ("data/size-charts", "data/images")


def _resolve_within(path: str, roots: tuple[str, ...]) -> Path | None:
    """The real file for `path`, or None if it is outside the allowed roots
    or does not exist.

    Containment is checked against the *matched* root's own resolved
    directory, not `PROJECT_ROOT` as a whole. A naive prefix match plus a
    PROJECT_ROOT-only containment check would let
    `data/size-charts/../inbound/x.jpg` through: it starts with
    `data/size-charts` as a string, and after `resolve()` collapses the `..`
    it still lands inside `PROJECT_ROOT` -- just not inside `data/size-charts`.
    Requiring the resolved target to sit under the resolved matched root
    closes that. The prefix match itself is also segment-aware (`root` or
    `root/...`, never a bare string prefix), so `data/size-charts-evil` does
    not count as being inside `data/size-charts`.
    """
    normalised = (path or "").replace("\\", "/").lstrip("/")
    matched_root = next(
        (root for root in roots if normalised == root or normalised.startswith(f"{root}/")),
        None,
    )
    if matched_root is None:
        return None
    root_dir = (PROJECT_ROOT / matched_root).resolve()
    target = (PROJECT_ROOT / normalised).resolve()
    try:
        target.relative_to(root_dir)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def resolve_servable_path(path: str) -> Path | None:
    """The file a logged-in surface may serve, or None."""
    return _resolve_within(path, SERVABLE_ROOTS)


def resolve_public_path(path: str) -> Path | None:
    """The same guard, against the narrower public roots."""
    return _resolve_within(path, PUBLIC_ROOTS)
