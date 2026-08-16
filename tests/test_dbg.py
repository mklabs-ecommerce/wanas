from sqlalchemy import select
from backend.models import Variant
from backend.services import catalog

def test_dbg(seeded, shopify):
    for v in seeded.scalars(select(Variant).where(Variant.product_id == "wanas-hoodie")).all():
        v.stock_qty = 0
        shopify.set(v.variant_id, qty=0)
    seeded.flush()
    print("SHELF:", {k: e["qty"] for k, e in shopify.shelf.items() if k.startswith("wanas-hoodie")})
    target = seeded.get(Variant, "wanas-hoodie-m-olive")
    print("ALTS:", catalog.alternatives_for(seeded, target))
