"""A product added straight in Shopify Admin -- not through the dashboard's
own create panel -- gets no wanas.db row, and `catalog.get_products` (the
bot's search) only ever reads wanas.db. See
`integrations/shopify/product_import.py` for the full story.
"""

from __future__ import annotations

from decimal import Decimal

from domain.models import Product, Variant
from domain.services.catalog import get_products
from integrations.shopify.product_import import import_missing_products


def _seed_manual_product(shopify, *, title="Loose Cargo Pants", category="Joggers & Sweatpants", sku=""):
    """A product that exists on the (fake) store but was never pushed through
    `shopify_admin_products.create_product` -- exactly what staff adding a
    product straight in Shopify Admin would leave behind: a real product with
    no wanas.db-recognised SKU on its variant."""
    gid = shopify.shopify_create_product(title=title, description="A pair of pants.", category=category)
    shopify.shopify_create_variants(
        gid,
        [
            {
                "inventoryItem": {"sku": sku, "tracked": True},
                "price": "450.00",
                "optionValues": [{"optionName": "Size", "name": "One Size"}],
            }
        ],
    )
    return gid


def test_nothing_already_known_gets_reimported(seeded, shopify):
    """Every product `seed_from` copied onto the fake shelf already has a
    matching wanas.db SKU -- there is nothing here to do."""
    report = import_missing_products(seeded, apply=True)
    assert report["imported"] == []
    assert report["problems"] == []


def test_a_product_added_straight_in_shopify_admin_becomes_searchable(seeded, shopify):
    _seed_manual_product(shopify, title="Loose Cargo Pants")

    report = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert report["problems"] == []
    assert len(report["imported"]) == 1
    product_id = report["imported"][0]["product_id"]

    product = seeded.get(Product, product_id)
    assert product is not None
    assert product.name == "Loose Cargo Pants"
    assert product.department == "unisex"
    assert product.category == "Joggers & Sweatpants"

    variants = seeded.query(Variant).filter_by(product_id=product_id).all()
    assert len(variants) == 1
    assert variants[0].price == Decimal("450.00")

    # And it is now what the bot's own search actually finds.
    found = get_products(seeded, query="cargo")
    assert any(p["product_id"] == product_id for p in found["products"])


def test_the_sku_is_written_back_to_shopify_so_live_price_and_stock_track_it(seeded, shopify):
    gid = _seed_manual_product(shopify, title="Loose Cargo Pants")

    report = import_missing_products(seeded, apply=True)
    seeded.commit()
    product_id = report["imported"][0]["product_id"]

    variant = seeded.query(Variant).filter_by(product_id=product_id).first()
    detail = shopify.get_product(gid)
    assert detail["variants"][0]["sku"] == variant.variant_id


def test_a_dry_run_reports_but_writes_nothing(seeded, shopify):
    _seed_manual_product(shopify, title="Loose Cargo Pants")

    report = import_missing_products(seeded, apply=False)

    assert len(report["imported"]) == 1
    assert seeded.get(Product, report["imported"][0]["product_id"]) is None


def test_a_variant_carrying_someone_elses_sku_is_reported_not_guessed(seeded, shopify):
    """A non-blank SKU that just is not one of ours was set on purpose by
    someone -- report it, do not silently re-key it."""
    _seed_manual_product(shopify, title="Loose Cargo Pants", sku="STAFF-TYPED-THIS")

    report = import_missing_products(seeded, apply=True)

    assert report["imported"] == []
    assert len(report["problems"]) == 1
    assert "Loose Cargo Pants" in report["problems"][0]


def test_running_it_twice_does_not_create_the_product_a_second_time(seeded, shopify):
    _seed_manual_product(shopify, title="Loose Cargo Pants")
    import_missing_products(seeded, apply=True)
    seeded.commit()

    second = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert second["imported"] == []
    assert second["problems"] == []
    assert seeded.query(Product).filter(Product.name == "Loose Cargo Pants").count() == 1


def test_a_draft_product_is_not_imported(seeded, shopify):
    gid = _seed_manual_product(shopify, title="Unfinished Prototype")
    shopify.products[gid]["status"] = "DRAFT"

    report = import_missing_products(seeded, apply=True)

    assert report["imported"] == []
    assert report["problems"] == []
    assert seeded.query(Product).filter(Product.name == "Unfinished Prototype").count() == 0


def test_a_half_made_product_is_not_imported(seeded, shopify):
    """A `productCreate` that succeeded and whose variants then failed leaves a
    product wearing nothing but Shopify's own "Default Title" placeholder.
    Mirroring one writes a phantom "One Size" row at 0.00 that the bot offers
    and can never sell -- and it outlives the Shopify product, because import
    is additive and nothing here ever deletes. Three of those had to be
    removed from production by hand."""
    shopify.shopify_create_product(
        title="Half Made Tee", description="", category="T-Shirts",
        options=[{"name": "Size", "values": [{"name": "S"}]}], vendor="Wanas Gallery",
    )

    report = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert report["imported"] == []
    assert report["problems"] == []
    assert seeded.get(Product, "half-made-tee") is None


def test_it_is_imported_once_its_real_variants_land(seeded, shopify):
    """The skip is about the placeholder, not about the product. A staff
    member who finishes it in Shopify Admin gets it on the next run."""
    gid = shopify.shopify_create_product(
        title="Finished Tee", description="", category="T-Shirts",
        options=[{"name": "Size", "values": [{"name": "S"}]}], vendor="Wanas Gallery",
    )
    assert import_missing_products(seeded, apply=True)["imported"] == []

    shopify.shopify_create_variants(
        gid,
        [{
            "inventoryItem": {"sku": "", "tracked": True},
            "price": "300.00",
            "optionValues": [{"optionName": "Size", "name": "S"}],
        }],
    )

    report = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert [i["product_id"] for i in report["imported"]] == ["finished-tee"]
    assert seeded.get(Product, "finished-tee") is not None


# --------------------------------------------------------------------------
# A SKU that is ours, whose local rows went missing
# --------------------------------------------------------------------------


def _seed_orphaned_dashboard_product(
    shopify,
    *,
    product_id="oversized-plain-tee",
    title="Oversized Plain Tee",
    size="S",
    colours=("Black", "Navy"),
):
    """What a dashboard create leaves behind when it pushes to Shopify and
    then fails before mirroring: a real product wearing SKUs in exactly the
    `_variant_id` shape, with no wanas.db rows at all."""
    gid = shopify.shopify_create_product(
        title=title,
        description="",
        category="T-Shirts",
        options=[
            {"name": "Size", "values": [{"name": size}]},
            {"name": "Color", "values": [{"name": c} for c in colours]},
        ],
        vendor="Wanas Gallery",
    )
    shopify.shopify_create_variants(
        gid,
        [
            {
                "inventoryItem": {
                    "sku": f"{product_id}-{size.lower()}-{colour.lower()}",
                    "tracked": True,
                },
                "price": "400.00",
                "optionValues": [
                    {"optionName": "Size", "name": size},
                    {"optionName": "Color", "name": colour},
                ],
            }
            for colour in colours
        ],
    )
    return gid


def test_a_sku_this_codebase_wrote_is_adopted_not_refused(seeded, shopify):
    """The real one, found in production: a product live in Shopify wearing
    our own SKU convention and invisible to the bot forever, because the
    reconcile called its own SKUs somebody else's and refused them every
    boot."""
    _seed_orphaned_dashboard_product(shopify)

    report = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert report["problems"] == []
    assert [e["product_id"] for e in report["imported"]] == ["oversized-plain-tee"]
    assert report["imported"][0]["adopted"] is True

    # Adopted under the id the SKUs already say, so nothing is re-keyed.
    variants = seeded.query(Variant).filter_by(product_id="oversized-plain-tee").all()
    assert sorted(v.variant_id for v in variants) == [
        "oversized-plain-tee-s-black",
        "oversized-plain-tee-s-navy",
    ]
    assert any(p["product_id"] == "oversized-plain-tee" for p in get_products(seeded, query="plain")["products"])


def test_adopting_writes_nothing_back_to_shopify(seeded, shopify, monkeypatch):
    """The SKUs on the variants are already the ones `_mirror_local` derives.
    Writing them again is a call that can only fail."""
    from integrations.shopify import product_import

    def refuse(*a, **k):
        raise AssertionError("adopting must not write SKUs back to Shopify")

    _seed_orphaned_dashboard_product(shopify)
    monkeypatch.setattr(product_import.admin, "shopify_update_variants", refuse)

    assert import_missing_products(seeded, apply=True)["problems"] == []


def test_a_sku_that_only_looks_like_ours_is_still_refused(seeded, shopify):
    """The check is exact: every SKU has to rebuild itself from the
    convention. One that does not is somebody's own scheme."""
    gid = shopify.shopify_create_product(
        title="Cargo Pants", description="", category="Joggers & Sweatpants",
        options=[{"name": "Size", "values": [{"name": "S"}]}], vendor="Wanas Gallery",
    )
    shopify.shopify_create_variants(
        gid,
        [{
            "inventoryItem": {"sku": "cargo-pants-small", "tracked": True},
            "price": "400.00",
            "optionValues": [{"optionName": "Size", "name": "S"}],
        }],
    )

    report = import_missing_products(seeded, apply=True)
    assert report["imported"] == []
    assert len(report["problems"]) == 1


def test_an_adoptable_id_already_taken_is_reported_rather_than_collided(seeded, shopify):
    """If the id belongs to a different product the SKUs cannot mean what they
    look like, and adopting would fold two products into one.

    The size and colour are ones the hoodie does not sell, so none of these
    SKUs is a known variant -- otherwise the product reads as already linked
    and never reaches the adoption check at all."""
    _seed_orphaned_dashboard_product(
        shopify,
        product_id="wanas-hoodie",
        title="Not The Hoodie",
        size="XXS",
        colours=("Chartreuse",),
    )

    report = import_missing_products(seeded, apply=True)

    assert report["imported"] == []
    assert len(report["problems"]) == 1
    assert seeded.get(Product, "wanas-hoodie").name != "Not The Hoodie"


def test_adopting_is_idempotent(seeded, shopify):
    _seed_orphaned_dashboard_product(shopify)
    import_missing_products(seeded, apply=True)
    seeded.commit()

    second = import_missing_products(seeded, apply=True)
    assert second["imported"] == []
    assert second["problems"] == []


# --------------------------------------------------------------------------
# One product, from the webhook
# --------------------------------------------------------------------------


def test_one_product_is_imported_by_gid(seeded, shopify):
    """What the `products/create` webhook calls: the same rules, without a
    catalogue read."""
    from integrations.shopify.product_import import import_product

    gid = _seed_manual_product(shopify, title="Loose Cargo Pants")
    entry = import_product(seeded, gid)
    seeded.commit()

    assert entry is not None
    assert seeded.get(Product, entry["product_id"]).name == "Loose Cargo Pants"


def test_one_product_already_known_is_left_alone(seeded, shopify):
    from integrations.shopify.product_import import import_product

    gid = _seed_manual_product(shopify, title="Loose Cargo Pants")
    import_product(seeded, gid)
    seeded.commit()

    assert import_product(seeded, gid) is None


def test_one_product_that_is_only_a_placeholder_is_skipped(seeded, shopify):
    """Same rule the boot reconcile applies -- and the reason the webhook
    subscribes to products/update as well as products/create."""
    from integrations.shopify.product_import import import_product

    gid = shopify.shopify_create_product(
        title="Half Made Tee", description="", category="T-Shirts",
        options=[{"name": "Size", "values": [{"name": "S"}]}], vendor="Wanas Gallery",
    )
    assert import_product(seeded, gid) is None
    assert seeded.get(Product, "half-made-tee") is None
