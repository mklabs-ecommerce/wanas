"""Fill in the @handles of Instagram conversations that predate them.

`assistant/channels/instagram.py` learns a customer's handle the first time
they write, which means every conversation already in the database has none
and would keep showing a bare IGSID until that person happens to message
again. This asks Meta for the ones that are missing.

Dry-run by default like every script here; `--apply` writes. Safe to re-run:
it only ever looks at identities with no username, and a profile Meta will
not share is skipped rather than guessed at, so a second run costs one call
per still-unknown customer and changes nothing else.

    python scripts/instagram_backfill_usernames.py
    python scripts/instagram_backfill_usernames.py --apply

Meta only shares a profile for someone who has actually messaged the
account, and only with the messaging permissions granted -- so "not readable"
is an ordinary outcome here, not a failure of the script.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from domain.db import session_scope  # noqa: E402
from domain.models import ChannelIdentity  # noqa: E402
from domain.services import identities  # noqa: E402
from integrations.instagram.client import InstagramClient  # noqa: E402

CHANNEL = "instagram_dm"

#: A courtesy gap between calls. This is a backfill over a list that only
#: grows once, not a hot path, and there is no reason to make Meta rate-limit
#: us to find out how fast we could have gone.
PAUSE_SECONDS = 0.3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the handles (default: dry run)")
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after this many lookups (0 = no limit)"
    )
    args = parser.parse_args()

    if not settings.instagram_configured:
        print("Instagram is not configured (INSTAGRAM_ACCOUNT_ID / INSTAGRAM_ACCESS_TOKEN).")
        return 1

    with session_scope() as db:
        pending = [
            row.external_id
            for row in db.query(ChannelIdentity)
            .filter(ChannelIdentity.channel == CHANNEL)
            .filter(ChannelIdentity.username.is_(None))
            .all()
        ]

    if not pending:
        print("Every Instagram identity already has a handle. Nothing to do.")
        return 0

    if args.limit > 0:
        pending = pending[: args.limit]

    print(f"{len(pending)} Instagram identit{'y' if len(pending) == 1 else 'ies'} with no handle.")
    client = InstagramClient()
    found = 0
    unreadable = 0

    for igsid in pending:
        profile = client.get_user_profile(igsid)
        if not profile or not profile.get("username"):
            unreadable += 1
            print(f"  {igsid}: not readable")
        else:
            found += 1
            real_name = f" ({profile['name']})" if profile.get("name") else ""
            print(f"  {igsid}: @{profile['username']}{real_name}")
            if args.apply:
                with session_scope() as db:
                    identities.set_platform_profile(
                        db,
                        CHANNEL,
                        igsid,
                        username=profile.get("username"),
                        name=profile.get("name"),
                    )
        time.sleep(PAUSE_SECONDS)

    print()
    print(f"readable: {found}   not readable: {unreadable}")
    if not args.apply:
        print("Dry run -- nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
