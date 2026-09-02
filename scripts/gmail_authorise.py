"""Mint the Gmail refresh token the alert emails send with.

Run this **once, on your own machine** -- it opens a browser, asks Google for
permission to send mail as you, and prints a refresh token to paste into
Railway. Nothing here touches the database and nothing is written to disk.

    python scripts/gmail_authorise.py

Before you run it, in the Google Cloud console (console.cloud.google.com):

  1. Create a project (any name).
  2. APIs & Services -> Library -> enable **Gmail API**.
  3. APIs & Services -> OAuth consent screen -> External. Fill in the app
     name and your own email. Add the scope
     `https://www.googleapis.com/auth/gmail.send`, and add yourself as a
     test user.
  4. **Publish the app** (OAuth consent screen -> Publish app). This matters
     more than it looks: while the screen is left in *Testing*, Google
     expires every refresh token after **seven days**, and the alerts would
     stop a week after they started with nothing to show for it. Published
     but unverified is fine for one user -- you will see an "unverified app"
     warning during step 6 and can continue past it.
  5. Credentials -> Create credentials -> OAuth client ID -> **Desktop app**.
     Copy the client id and client secret.
  6. Run this script, paste those two in, and approve in the browser.

The three values then go in Railway as GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET
and GMAIL_REFRESH_TOKEN. Treat the secret and the token like every other
credential here: never committed, never logged.

Why not SMTP: Railway blocks all of it. From inside the container ports 25,
465, 587 and 2525 answer "Network is unreachable" while plain HTTP connects
instantly, so an app password cannot deliver from production however correct
it is. See `integrations/mail/client.py`.
"""

from __future__ import annotations

import http.server
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Catches Google's redirect and nothing else.

    The `state` value is checked before the code is accepted: without it any
    page open in the same browser could hand this listener a code of its own
    choosing.
    """

    code: str | None = None
    state: str = ""

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        ok = bool(code) and secrets.compare_digest(given, _Catcher.state)
        if ok:
            _Catcher.code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<h2>Done — you can close this tab.</h2>"
            if ok
            else "<h2>Something went wrong. Check the terminal.</h2>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args) -> None:
        """Silence the default request logging -- the URL carries the code."""


def main() -> int:
    print(__doc__.split("Before you run it")[0].strip())
    print()
    client_id = input("GMAIL_CLIENT_ID: ").strip()
    client_secret = input("GMAIL_CLIENT_SECRET: ").strip()
    if not client_id or not client_secret:
        print("Both are required. See the steps at the top of this file.")
        return 1

    port = _free_port()
    redirect_uri = f"http://localhost:{port}"
    _Catcher.state = secrets.token_urlsafe(24)
    _Catcher.code = None

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        # Both are needed to be *given* a refresh token: offline access asks
        # for one, and consent forces the prompt even if this account has
        # approved the app before -- without it a second run returns an
        # access token only, and the script would look broken.
        "access_type": "offline",
        "prompt": "consent",
        "state": _Catcher.state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print()
    print("Opening your browser. If it does not open, paste this in yourself:")
    print()
    print(f"  {url}")
    print()
    print('An "unverified app" warning is expected — continue past it.')
    webbrowser.open(url)

    thread.join(timeout=300)
    server.server_close()

    if not _Catcher.code:
        print("No authorisation code came back (timed out, refused, or state mismatch).")
        return 1

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": _Catcher.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"Google refused the exchange ({response.status_code}).")
        try:
            print("  error:", (response.json() or {}).get("error"))
        except ValueError:
            pass
        return 1

    refresh_token = (response.json() or {}).get("refresh_token")
    if not refresh_token:
        print(
            "Google returned no refresh token. That happens when this account has "
            "already approved the app and the consent prompt was skipped -- revoke "
            "it at myaccount.google.com/permissions and run this again."
        )
        return 1

    print()
    print("Refresh token (paste into Railway as GMAIL_REFRESH_TOKEN):")
    print()
    print(f"  {refresh_token}")
    print()
    print("Set all three, and keep them out of the repo:")
    print("  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN")
    print()
    print(
        "If the alerts stop about a week from now, the OAuth consent screen was "
        "left in Testing — publish it and mint a new token."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
