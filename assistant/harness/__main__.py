"""Local chat harness.

    python -m assistant.harness [--id 201000000001] [--channel whatsapp]

Throwaway scaffolding for the flow, not a product surface. It calls the same
`handle_message(channel, external_id, text)` entry point the WhatsApp adapter
calls, with a fake identity -- WhatsApp needs Meta approval and a live
webhook, and without a local way in, the hardest component in the system is
also the last one you can test.

The WhatsApp adapter is the only thing that changes when it is swapped in.
"""

from __future__ import annotations

import argparse
import logging
import sys

from assistant import session as session_store
from assistant.runtime import handle_message
from config.settings import settings
from domain.db import engine, session_scope
from domain.models import Base
from domain.services import (
    identities,
    notifications,
)

BANNER = """
┌─ Wanas Gallery — local chat harness ──────────────────────────────────┐
│ Same entry point as WhatsApp: handle_message(channel, external_id).   │
│ Commands:  /reset  clear this conversation's history                  │
│            /cart   show what the tools see                            │
│            /image <path>   simulate an incoming photo                 │
│            /unpause  simulate a staff resolve in the dashboard        │
│            /quit                                                      │
└───────────────────────────────────────────────────────────────────────┘
"""


def _use_utf8() -> None:
    """The replies are Arabic and the Windows console defaults to a legacy
    code page, which turns every message into a UnicodeEncodeError."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-tty
            pass


def _drain_outbound() -> list:
    """Proactive messages (confirmation, status pushes, feedback request) that
    the Notification service produced during the last turn."""
    sender = notifications.get_sender()
    sent = list(getattr(sender, "sent", []))
    if hasattr(sender, "clear"):
        sender.clear()
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assistant.harness")
    parser.add_argument("--id", dest="external_id", default="201000000001", help="fake channel identity")
    parser.add_argument("--channel", default="whatsapp")
    parser.add_argument("--verbose", "-v", action="store_true", help="show tool calls and logs")
    args = parser.parse_args(argv)

    _use_utf8()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    Base.metadata.create_all(engine)

    provider_name = settings.llm_provider
    print(BANNER)
    print(f"identity: {args.channel}/{args.external_id}   provider: {provider_name}")
    if provider_name in {"fake", "rehearsal"} or not settings.llm_api_key:
        print("no LLM key set — running the rehearsal stand-in; type 'help' for its commands.\n")
    else:
        print("talking to the real model — write like a customer would.\n")

    while True:
        try:
            text = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        if text == "/reset":
            with session_scope() as db:
                session_store.clear(db, args.channel, args.external_id)
            print("· history cleared (the cart survives, by design)\n")
            continue
        if text == "/unpause":
            with session_scope() as db:
                identities.unpause(db, args.channel, args.external_id)
            print("· conversation un-paused (this is a staff action in the dashboard)\n")
            continue
        if text == "/cart":
            from domain.services import carts

            with session_scope() as db:
                print(carts.cart_payload(db, args.channel, args.external_id), "\n")
            continue

        image_paths = None
        if text.startswith("/image"):
            parts = text.split(maxsplit=1)
            image_paths = [parts[1] if len(parts) > 1 else "data/images/customer-photo.jpg"]
            text = ""

        reply = handle_message(
            args.channel,
            args.external_id,
            text,
            image_paths=image_paths,
        )

        if reply.paused and reply.text is None:
            print("bot › (silent — conversation is with a person; /unpause to hand it back)\n")
        elif reply.text:
            print(f"bot › {reply.text}")
            for path in reply.attachments:
                print(f"     📎 {path}")
            if args.verbose and reply.tool_calls:
                print(f"     · tools: {', '.join(reply.tool_calls)}")
            print()

        for message in _drain_outbound():
            label = "image" if message.kind == "image" else message.template or "text"
            print(f"     ↗ proactive [{label}] to {message.to}:")
            for line in message.text.splitlines():
                print(f"       {line}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
