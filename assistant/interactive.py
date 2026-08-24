"""Tappable replies, described in a channel-neutral shape.

`AGENTS.md` says the governorate is "a picked value from a fixed list, not free
text, because it sets the price". Until now it was picked in spirit only: the
model asked in prose, the customer typed something, and
`shipping.resolve` did its best with the aliases. That works most of the time,
and the times it does not are an address the courier cannot use or a shipping
fee quoted for the wrong governorate.

A list message makes the sentence literally true. The customer taps, the reply
comes back as one of twenty-seven known values, and nothing has to be parsed.

The shape below is not WhatsApp's. It is the same trick the message format
plays for providers: one neutral description, translated by the adapter, so the
tool layer never learns what Meta's JSON looks like and a second channel is a
second translator rather than a second design.

    {"kind": "list", "body": "...", "button": "اختار", "sections": [...]}
    {"kind": "buttons", "body": "...", "buttons": [{"id", "title"}]}
"""

from __future__ import annotations

#: Meta's hard limits, and the reason the governorate picker is two steps.
#: Exceeding one of these is rejected at send time with an error that does not
#: name the field, so they are enforced here where the payload is built.
MAX_ROWS = 10
MAX_BUTTONS = 3
MAX_TITLE = 24
MAX_DESCRIPTION = 72
MAX_BODY = 1024


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def list_message(body: str, button: str, rows: list[dict], *, section_title: str = "") -> dict:
    """A single-section list. `rows` are ``{"id", "title", "description"?}``.

    Truncated rather than refused: a row title one character too long should
    cost the customer an ellipsis, not cost them the picker.
    """
    trimmed = [
        {
            "id": str(row["id"])[:200],
            "title": _clip(str(row.get("title") or row["id"]), MAX_TITLE),
            **(
                {"description": _clip(str(row["description"]), MAX_DESCRIPTION)}
                if row.get("description")
                else {}
            ),
        }
        for row in rows[:MAX_ROWS]
    ]
    section = {"rows": trimmed}
    if section_title:
        section["title"] = _clip(section_title, MAX_TITLE)
    return {
        "kind": "list",
        "body": _clip(body, MAX_BODY),
        "button": _clip(button, MAX_TITLE),
        "sections": [section],
    }


def buttons_message(body: str, buttons: list[dict]) -> dict:
    """Up to three reply buttons. More than three has to be a list."""
    return {
        "kind": "buttons",
        "body": _clip(body, MAX_BODY),
        "buttons": [
            {
                "id": str(button["id"])[:200],
                "title": _clip(str(button.get("title") or button["id"]), MAX_TITLE),
            }
            for button in buttons[:MAX_BUTTONS]
        ],
    }


# --------------------------------------------------------------------------
# the governorate picker
# --------------------------------------------------------------------------

REGION_PROMPT = "اختار المنطقة اللي هنشحنلك فيها 👇"
GOVERNORATE_PROMPT = "تمام، اختار المحافظة 👇"
PICK_BUTTON = "اختار"


def region_picker(regions: list[dict]) -> dict:
    return list_message(
        REGION_PROMPT,
        PICK_BUTTON,
        [{"id": region["region_id"], "title": region["label_ar"]} for region in regions],
        section_title="المناطق",
    )


def governorate_picker(governorates: list[dict], region_label: str = "") -> dict:
    """One region's governorates.

    A governorate with no fee set is still shown. Hiding it would leave a
    customer tapping around for a province that is on the list everywhere else;
    `confirm_order` refuses it with a reason the bot can explain, which is the
    honest failure.
    """
    return list_message(
        GOVERNORATE_PROMPT,
        PICK_BUTTON,
        [
            {"id": row["governorate"], "title": row["label_ar"] or row["governorate"]}
            for row in governorates
        ],
        section_title=region_label or "المحافظات",
    )
