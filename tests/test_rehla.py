"""Rehla: the seed, the published facts, the girls' vocabulary, and selling
with no Shopify at all (integrations/shopify/local_shelf.py)."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from domain.models import Order, ShippingRate, Variant
from domain.services import carts, garments, orders, search_terms, shop_facts
from domain.services.catalog import get_products

USE_REAL_CATALOG = True


def test_seed_is_the_rehla_catalog(seeded):
    from domain.models import Product

    assert seeded.query(Product).count() == 21
    assert seeded.query(Variant).count() == 204
    ids = [v.variant_id for v in seeded.query(Variant).all()]
    assert len(ids) == len(set(ids))
    assert {p.department for p in seeded.query(Product).all()} == {"women"}
    categories = {p.category for p in seeded.query(Product).all()}
    assert categories == {"Tops", "T-Shirts", "Pants", "Hoodies & Jackets", "Caps"}


def test_every_seeded_photo_ships_and_fits_whatsapp(seeded):
    from pathlib import Path

    from domain.models import Product

    for product in seeded.query(Product).all():
        assert product.images, product.product_id
        for path in product.images:
            file = Path(path)
            assert file.exists(), path
            assert file.stat().st_size < 5 * 1024 * 1024, path


def test_shipping_is_70_for_cairo_and_giza_85_elsewhere():
    assert shop_facts.fee_for("Cairo") == Decimal("70")
    assert shop_facts.fee_for("Giza") == Decimal("70")
    assert shop_facts.fee_for("Aswan") == Decimal("85")
    assert shop_facts.shipping_line() == "70 جنيه للقاهرة والجيزة، و85 جنيه لباقي المحافظات"
    assert shop_facts.delivery_line() == "من 3 لـ 5 أيام"
    assert "كاش عند الاستلام" in shop_facts.PAYMENT_LINE


def test_girls_vocabulary_finds_the_shelf(seeded):
    def names(query):
        return {p["name"] for p in get_products(seeded, query=query)["products"]}

    assert "Rehla V-Halter Neck Backless Top" in names("توب ضهر مفتوح")
    assert "Rehla Yoga Pants" in names("بنطلون واسع")
    assert "Rehla Long Sleeve Off Shoulder Top" in names("بلوزة للمحجبات")
    assert names("بادي كم طويل")


def test_sold_out_is_said_in_egyptian():
    from assistant import reply_rules

    for badge in ("SOLD OUT", "sold out", "Sold-Out", "soldout"):
        text, fixes = reply_rules.correct(
            f"اللون الأسود {badge} حاليًا", vocabulary={}, references={}, states_money=False
        )
        assert "خلصانة" in text and "sold" not in text.lower(), text
        assert "SOLD OUT -> خلصانة" in fixes


def test_the_prompt_assumes_no_gender():
    from assistant.prompt import SYSTEM_PROMPT

    assert "بصيغة المؤنث" not in SYSTEM_PROMPT
    assert "متفترضش أبداً الزبون ولد ولا بنت" in SYSTEM_PROMPT
    assert "حضرتك" in SYSTEM_PROMPT
    assert "SOLD OUT" in SYSTEM_PROMPT  # named only to forbid it


#: Feminine second-person forms and pet names. The customer may be anyone --
#: a man buying a gift reads «تحبي» as the shop not listening.
_GENDERED = re.compile(r"بيكي|عليكي|معاكي|ليكي|تحبي|عايزة|قوليلي|ابعتيلي|اسألي|يا قمر|حبيبتي|يا جميلة")


def test_no_canned_line_assumes_the_customer_is_a_woman():
    from assistant import comment_replies
    from assistant.tools.support_tools import HANDOFF_CLOSINGS

    lines = [comment_replies.SHORT_DM, *HANDOFF_CLOSINGS.values()]
    for bank in (*comment_replies._BANKS.values(), *comment_replies._DM_OPENERS.values()):
        lines.extend(bank)
    lines.extend(comment_replies._SHORT_PUBLIC)
    product = {
        "name": "Rehla Tops", "category": "Tops", "any_in_stock": True, "price_from": 450,
        "price_to": 450, "in_stock_sizes": ["S"], "in_stock_colors": ["Black"],
    }
    lines.append(comment_replies.product_dm(product, greeting=True))
    lines.append(comment_replies.product_dm({**product, "any_in_stock": False}, greeting=True))
    offenders = [line for line in lines if _GENDERED.search(line)]
    assert not offenders, offenders


def test_what_rehla_does_not_sell():
    assert garments.not_sold("عندكم طرح؟")[0] == "طرح"
    assert garments.not_sold("فيه فساتين")[0] == "فساتين"
    assert garments.not_sold("Rehla White shirt") is None
    for word in garments._NOT_SOLD_NORMALIZED:
        assert word not in search_terms.SYNONYMS


@pytest.mark.no_shopify
def test_order_without_shopify_is_recorded_locally(seeded, monkeypatch):
    from integrations.shopify import local_shelf

    monkeypatch.setattr(local_shelf, "active", lambda: True)
    from app import _ensure_shipping_fees_set

    _ensure_shipping_fees_set()
    seeded.expire_all()
    assert seeded.get(ShippingRate, "Cairo").fee == Decimal("70")

    variant = seeded.query(Variant).filter(Variant.stock_qty > 0).first()
    before = variant.stock_qty
    carts.add(seeded, "whatsapp", "201011112222", variant.variant_id, 1)
    seeded.commit()

    result = orders.place_order(
        seeded,
        channel="whatsapp",
        external_id="201011112222",
        customer_name="منة أحمد",
        governorate="Cairo",
        address="15 شارع التحرير",
        contact_phone="01012345678",
    )
    assert "error" not in result, result
    order = seeded.query(Order).one()
    assert order.shopify_order_id is None
    assert order.order_id.startswith("RHL-")
    assert order.shipping_fee == Decimal("70")
    seeded.expire_all()
    assert seeded.get(Variant, variant.variant_id).stock_qty == before - 1

