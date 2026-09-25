"""The size chart a customer asks for is the one that arrives.

Production, 2026-09-22 and 2026-09-25, one customer (`whatsapp/2010671…`):

    customer: the size chart for the Boxy WNS Tee
    log:      tool get_size_chart({'product_id': 'boxy-wns-tee'})
    bot:      the RINGER BOXY FIT chart                       (09-22)
    customer: asks again
    log:      the reply says a photo is coming and nothing is attached,
              retry 1/2, retry 2/2
    log:      whatsapp send rejected 400: Param image.id is not a valid
              whatsapp business account media attachment ID
    customer: asks again, and again -- no chart ever arrives  (09-25)

Three separate faults, and each group below reproduces one of them exactly.

1. **The wrong chart.** The tool was asked for the right product and answered
   with the wrong chart, because the product row said so. `boxy-wns-tee` was
   seeded with `size_chart: "ringer-boxy-tee"`; commit e0333cb corrected the
   seed file to `wns-boxy-tee`, but the seed only ever runs against an empty
   catalog, so the live row kept the Ringer chart. The corrected chart then
   named a picture that was never committed, so even a correct link had
   nothing to send.
2. **Then nothing at all.** `WhatsAppClient.media_id_for` cached Meta's media
   id for each local picture "forever", and Meta keeps an uploaded file for
   thirty days. Every size chart went out through a dead id and was refused,
   and nothing ever threw the dead id away, so every retry was refused too.
3. **The chart counted as no picture.** `photo_claims` excludes a size chart
   from the photos a reply carries -- right for a claim about the *garment* --
   so a reply saying «دي صورة جدول المقاسات 👆» beside the chart it attached
   was read as a photo promised and nothing sent, and retried.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from assistant import agent, photo_claims
from assistant.channels import whatsapp as adapter
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.base import ToolContext, call_tool, last_product
from common.timeutil import utcnow
from config.settings import PROJECT_ROOT, settings
from domain.models import Product, WhatsAppMedia
from domain.services import size_charts

CHANNEL = "whatsapp"
WHO = "201067177129"

WNS_CHART = "data/size-charts/wns-boxy-tee.png"
RINGER_CHART = "data/size-charts/ringer-boxy-tee.png"

#: Meta's refusal, verbatim from the production log.
DEAD_ID = (
    '400: {"error":{"message":"Param image.id is not a valid whatsapp business '
    'account media attachment ID","code":100,"type":"OAuthException",'
    '"fbtrace_id":"Aca53bM19-FEdL4XuUb_LUd"}}'
)


def _boot() -> None:
    """The startup step that reconciles chart links, as every deploy runs it.

    Looked up by name so this file reproduces the production failure on a
    build that does not have the step yet, rather than failing to import.
    """
    import app

    getattr(app, "_correct_retired_size_charts", lambda: None)()


def _link(session, product_id: str, chart_id: str) -> None:
    session.get(Product, product_id).size_chart = chart_id
    session.commit()


def _ask_for_the_chart(session, product_id: str = "boxy-wns-tee"):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "c1", "name": "get_size_chart", "arguments": {"product_id": product_id}}
                ]
            ),
            ModelReply(text="ده جدول المقاسات، المقاسات بالسنتيمتر والقطعة مفرودة."),
        ]
    )
    return agent.run_turn(session, CHANNEL, WHO, "ابعتلي جدول مقاسات Boxy WNS Tee", provider=provider)


# --- 1. the wrong chart ------------------------------------------------------


def test_boxy_wns_tee_linked_as_production_left_it_gets_its_own_chart_after_boot(seeded):
    """The exact production state: the row still carries the value the seed
    held before e0333cb. Asked for the Boxy WNS Tee's chart, the bot sent the
    Ringer one."""
    _link(seeded, "boxy-wns-tee", "ringer-boxy-tee")
    seeded.rollback()
    _boot()
    seeded.expire_all()

    reply = _ask_for_the_chart(seeded)

    assert RINGER_CHART not in reply.attachments, "another product's chart went out"
    assert reply.attachments == [WNS_CHART]


def test_the_correction_is_exact_and_never_touches_a_staff_choice(seeded):
    """Only the retired value is rewritten. A chart staff picked -- any other
    value, including one the seed never named -- stands."""
    _link(seeded, "boxy-wns-tee", "oversized-graphic-tee")
    seeded.rollback()
    _boot()
    seeded.expire_all()
    assert seeded.get(Product, "boxy-wns-tee").size_chart == "oversized-graphic-tee"
    # And the Ringer tee, which really does use the Ringer chart, keeps it.
    assert seeded.get(Product, "ringer-tee").size_chart == "ringer-boxy-tee"


def test_every_shipped_chart_has_its_picture_on_disk():
    """`wns-boxy-tee.png` was named by the chart file and never committed, so
    the one chart that was finally right had no picture to send. A chart whose
    picture is missing is a chart the customer never sees."""
    missing = [
        chart_id
        for chart_id, chart in size_charts.all_charts().items()
        if chart.get("image") and not (PROJECT_ROOT / chart["image"]).is_file()
    ]
    assert missing == []


def test_the_boxy_wns_tee_chart_is_its_own_numbers_and_its_own_picture(seeded):
    ctx = ToolContext(session=seeded, channel=CHANNEL, external_id=WHO)
    payload = call_tool(ctx, "get_size_chart", {"product_id": "boxy-wns-tee"})

    assert payload["chart_id"] == "wns-boxy-tee"
    assert payload["sizes"]["S"] == {"width": 56, "length": 66}
    assert ctx.attachments == [WNS_CHART]
    assert (PROJECT_ROOT / WNS_CHART).is_file()


def test_the_chart_answer_names_the_product_it_is_for(seeded):
    """A chart's `title` is the chart's, and several products share one. The
    answer has to say which *product* it is for -- read as the product's name,
    "Ringer t-shirt" became what the conversation was about, and the next
    question was answered about the Ringer tee."""
    ctx = ToolContext(session=seeded, channel=CHANNEL, external_id=WHO)
    payload = call_tool(ctx, "get_size_chart", {"product_id": "cairokee-tee"})
    assert payload["product_id"] == "cairokee-tee"
    assert payload["name"] == "Cairokee T-shirt"
    assert payload["title"] == "Oversized t-shirt"

    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "get_size_chart", "arguments": {"product_id": "cairokee-tee"}}
            ],
        },
        {
            "role": "tool_results",
            "results": [{"id": "c1", "name": "get_size_chart", "content": payload}],
        },
    ]
    assert last_product(history)["name"] == "Cairokee T-shirt"
    label = ctx.attachment_labels[ctx.attachments[0]]
    assert label["name"] == "Cairokee T-shirt"
    assert label["product_id"] == "cairokee-tee"


def test_a_chart_whose_picture_is_missing_is_never_attached(seeded, monkeypatch, tmp_path):
    """A path to nothing is not a picture: attaching one promises the customer
    a chart that can only fail on send. The product's own uploaded chart is
    used instead when there is one, and otherwise nothing is attached."""
    chart = dict(size_charts.get_chart("wns-boxy-tee"))
    chart["image"] = "data/size-charts/does-not-exist.png"
    monkeypatch.setattr(size_charts, "_load", lambda path=None: {"wns-boxy-tee": chart})

    ctx = ToolContext(session=seeded, channel=CHANNEL, external_id=WHO)
    payload = call_tool(ctx, "get_size_chart", {"product_id": "boxy-wns-tee"})
    assert ctx.attachments == []
    assert payload["image"] is None
    assert payload["sizes"], "the numbers are still there to quote"

    seeded.get(Product, "boxy-wns-tee").size_chart_image = "https://cdn.shopify.com/wns.png"
    seeded.flush()
    ctx = ToolContext(session=seeded, channel=CHANNEL, external_id=WHO)
    call_tool(ctx, "get_size_chart", {"product_id": "boxy-wns-tee"})
    assert ctx.attachments == ["https://cdn.shopify.com/wns.png"]


# --- 2. then nothing at all --------------------------------------------------


@pytest.fixture()
def meta(monkeypatch):
    """Meta, as it behaved on 2026-09-25: the id cached for a chart weeks ago
    is refused, a freshly uploaded one is accepted."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(settings, whatsapp_phone_number_id="1", whatsapp_access_token="t"),
    )
    state = {"uploads": [], "posts": [], "live": set()}

    def fake_upload(self, path):
        media_id = f"fresh-{len(state['uploads']) + 1}"
        state["uploads"].append(path)
        state["live"].add(media_id)
        return media_id

    def fake_post(self, payload):
        state["posts"].append(payload)
        media_id = (payload.get("image") or {}).get("id")
        if media_id is not None and media_id not in state["live"]:
            return False, DEAD_ID, None
        return True, None, f"wamid.{len(state['posts'])}"

    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", fake_upload)
    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    return state


def _cache(session, path: str, media_id: str, *, age: timedelta = timedelta(days=1)) -> None:
    session.add(WhatsAppMedia(path=path, media_id=media_id, uploaded_at=utcnow() - age))
    session.commit()


def test_a_media_id_meta_refuses_is_uploaded_again_and_the_chart_still_arrives(seeded, meta):
    """The production send, exactly: a cached id Meta no longer knows."""
    _cache(seeded, RINGER_CHART, "expired-id")
    seeded.rollback()
    client = adapter.WhatsAppClient()

    sent = client.send_image(WHO, RINGER_CHART)

    assert sent.delivered is True, sent.error
    assert meta["uploads"] == [RINGER_CHART]
    seeded.expire_all()
    assert seeded.get(WhatsAppMedia, RINGER_CHART).media_id == "fresh-1"


def test_asking_again_after_a_refusal_does_not_send_the_dead_id_again(seeded, meta):
    """«When I tried again afterwards, it stopped sending any size chart at
    all»: the second, third and fourth sends all reused the same dead id."""
    _cache(seeded, RINGER_CHART, "expired-id")
    seeded.rollback()
    client = adapter.WhatsAppClient()

    for _ in range(3):
        assert client.send_image(WHO, RINGER_CHART).delivered is True

    dead = [p for p in meta["posts"] if (p.get("image") or {}).get("id") == "expired-id"]
    assert len(dead) <= 1, "the id Meta refused was sent again"
    assert meta["uploads"] == [RINGER_CHART]


def test_an_id_older_than_metas_retention_is_not_trusted(seeded, meta):
    """Meta keeps an uploaded file for thirty days. An id close to that age
    is uploaded again before it is sent, rather than after it is refused."""
    _cache(seeded, RINGER_CHART, "old-id", age=timedelta(days=29))
    seeded.rollback()
    client = adapter.WhatsAppClient()

    assert client.media_id_for(RINGER_CHART) == "fresh-1"
    assert client.send_image(WHO, RINGER_CHART).delivered is True
    assert all((p.get("image") or {}).get("id") != "old-id" for p in meta["posts"])


def test_a_recent_id_is_still_reused(seeded, meta):
    """The cache is still a cache: a chart uploaded yesterday is not uploaded
    again today."""
    _cache(seeded, RINGER_CHART, "fresh-0")
    meta["live"].add("fresh-0")
    seeded.rollback()
    client = adapter.WhatsAppClient()

    assert client.send_image(WHO, RINGER_CHART).delivered is True
    assert meta["uploads"] == []


def test_a_refusal_that_is_not_about_the_id_is_not_retried(seeded, meta, monkeypatch):
    """Only a dead id is worth a fresh upload. Anything else -- the customer
    outside the 24-hour window, a bad number -- would be refused the same way
    again, and uploading a picture to learn that is a slow reply for nothing."""
    _cache(seeded, RINGER_CHART, "fresh-0")
    meta["live"].add("fresh-0")
    seeded.rollback()

    def refuse(self, payload):
        meta["posts"].append(payload)
        return False, '400: {"error":{"message":"Re-engagement message","code":131047}}', None

    monkeypatch.setattr(adapter.WhatsAppClient, "_post", refuse)
    sent = adapter.WhatsAppClient().send_image(WHO, RINGER_CHART)
    assert sent.delivered is False
    assert meta["uploads"] == []
    assert len(meta["posts"]) == 1


# --- 3. the chart counted as no picture --------------------------------------


def _chart_label(name: str = "Boxy WNS Tee") -> dict:
    return {"label": f"{name} size chart", "name": name, "product_id": "boxy-wns-tee"}


def test_a_reply_about_the_chart_it_attached_is_not_an_empty_promise():
    """What the model wrote on 09-25, beside the chart it had attached."""
    why = photo_claims.unbacked_claim(
        "دي صورة جدول المقاسات بتاع Boxy WNS Tee 👆",
        attachments=[WNS_CHART],
        labels={WNS_CHART: _chart_label()},
    )
    assert why == ""


def test_calling_a_chart_a_photo_of_the_garment_is_still_caught():
    """The other half: a chart is still not the garment. «دي صورة التيشيرت»
    beside a measurements table has not shown the customer the shirt."""
    why = photo_claims.unbacked_claim(
        "دي صورة التيشيرت 👆", attachments=[WNS_CHART], labels={WNS_CHART: _chart_label()}
    )
    assert "nothing is attached" in why


def test_the_chart_goes_out_on_the_first_reply_without_a_retry(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "c1", "name": "get_size_chart", "arguments": {"product_id": "boxy-wns-tee"}}
                ]
            ),
            ModelReply(text="دي صورة جدول المقاسات 👆 المقاسات بالسنتيمتر والقطعة مفرودة."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ابعتلي جدول المقاسات", provider=provider)

    assert reply.error is None
    assert reply.attachments == [WNS_CHART]
    assert len(provider.calls) == 2, "a chart described as a chart was sent back for a retry"


def test_an_undelivered_chart_is_resent_by_the_chart_tool_not_the_photo_tool(seeded):
    """The note a turn gets about a refused picture told the model to call
    `get_variants` -- which sends the garment, never the chart. A refused
    chart is resent by `get_size_chart`."""
    from assistant import session as session_store

    session_store.save(
        seeded,
        CHANNEL,
        WHO,
        [
            {"role": "user", "content": "ابعتلي جدول المقاسات"},
            {"role": "assistant", "content": "ده الجدول 👆", "attachments": [WNS_CHART]},
        ],
    )
    session_store.record_undelivered_attachments(
        seeded, CHANNEL, WHO, {WNS_CHART: "Boxy WNS Tee size chart"}
    )
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "c1", "name": "get_size_chart", "arguments": {"product_id": "boxy-wns-tee"}}
                ]
            ),
            ModelReply(text="معلش، الجدول ماوصلش من عندنا. ده هو تاني 👆"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الجدول ماوصلش", provider=provider)
    prompt = provider.calls[0][0]
    assert "get_size_chart" in prompt.split("تنبيه داخلي")[-1]
    assert reply.attachments == [WNS_CHART]


def test_the_chart_file_is_a_real_png():
    """Not a placeholder: the picture the storefront shows for this product."""
    raw = Path(PROJECT_ROOT / WNS_CHART).read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(raw) > 50_000
