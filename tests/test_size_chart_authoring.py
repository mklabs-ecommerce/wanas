"""Making a size chart from the dashboard, instead of from a JSON file.

An uploaded chart used to be a picture and nothing else: the storefront showed
it, the bot could send it, and neither could answer "what is the chest on a
large". Putting numbers behind one meant editing `data/size_charts.json` and
running `scripts/shopify_size_charts.py`.

Two halves here. `POST /size-charts/read` hands the picture to the vision
model and gives the numbers back as data -- and the tests about it are mostly
about what it *refuses* to report. `POST /size-charts` saves what a staff
member confirmed, into the `size_charts` table that overlays the JSON file.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import providers
from assistant.providers.base import ProviderError, SizeChartReading, normalise_chart_reading
from assistant.providers.fake import ScriptedProvider
from config.settings import settings
from dashboard import shopify_api, web as dashboard
from domain.models import Product, SizeChart
from domain.services import auth
from domain.services.size_charts import all_charts, get_chart

SECRET = "test-dashboard-secret"
PNG = base64.b64encode(b"not really a png, nothing here decodes it").decode()


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(shopify_api.router)
    return TestClient(app)


@pytest.fixture()
def logged_in(client, seeded):
    auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


@pytest.fixture()
def vision(monkeypatch):
    provider = ScriptedProvider()
    providers.set_provider(provider)
    yield provider
    providers.set_provider(None)


def _read(client, **kwargs):
    body = {"filename": "chart.png", "content_type": "image/png", "data": PNG,
            "sizes": ["S", "M", "L"]}
    body.update(kwargs)
    return client.post("/dashboard/api/shopify/size-charts/read", json=body)


# --------------------------------------------------------------------------
# reading the picture
# --------------------------------------------------------------------------


def test_reading_a_chart_requires_login(client):
    assert _read(client).status_code == 401


def test_the_numbers_come_back_as_data(logged_in, vision):
    vision.push_chart_reading(SizeChartReading(
        measurements=[{"key": "width", "label_en": "Width", "label_ar": "العرض"}],
        sizes={"S": {"width": 54}, "M": {"width": 56}},
        unit="cm", confidence=0.9,
    ))

    body = _read(logged_in).json()

    assert body["sizes"] == {"S": {"width": 54}, "M": {"width": 56}}
    assert body["measurements"][0]["label_ar"] == "العرض"
    assert body["unit"] == "cm"


def test_the_products_own_sizes_are_what_it_is_asked_about(logged_in, vision):
    """A chart printing sizes this product does not sell contributes nothing,
    and a row nobody can order is worse than no row."""
    vision.push_chart_reading(SizeChartReading())

    _read(logged_in, sizes=["S", "M"])

    assert vision.chart_calls[0][2] == ["S", "M"]


def test_nothing_is_written_by_reading(logged_in, vision, seeded):
    """The reading is a filled-in form, not a saved chart. A measurement the
    model misread is a customer ordering the wrong size and posting it back."""
    vision.push_chart_reading(SizeChartReading(
        measurements=[{"key": "width", "label_en": "Width", "label_ar": "العرض"}],
        sizes={"S": {"width": 54}},
    ))

    _read(logged_in)
    seeded.expire_all()

    assert seeded.get(SizeChart, "wanas-hoodie") is None


def test_a_provider_that_cannot_read_says_so_instead_of_failing(logged_in, vision):
    """No reading queued -- the scripted provider raises `unsupported`, which
    is what a text-only deployment does. The staff member types the table."""
    res = _read(logged_in)

    assert res.status_code == 503
    assert res.json()["error"] == "reading_unavailable"


def test_a_deployment_with_no_vision_never_spends_the_call(logged_in, monkeypatch):
    class TextOnly(ScriptedProvider):
        supports_vision = False

        def read_size_chart(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("asked a text-only provider to read a picture")

    providers.set_provider(TextOnly())
    try:
        assert _read(logged_in).status_code == 503
    finally:
        providers.set_provider(None)


def test_a_file_that_is_not_an_image_is_refused(logged_in, vision):
    assert _read(logged_in, content_type="application/pdf").status_code == 400


# --------------------------------------------------------------------------
# what the reading is not allowed to claim
# --------------------------------------------------------------------------


def test_a_size_the_product_does_not_sell_is_dropped(seeded):
    reading = normalise_chart_reading(
        {"measurements": [{"key": "width", "label_en": "Width"}],
         "sizes": {"S": {"width": 54}, "XXL": {"width": 70}}},
        sizes=["S", "M"],
    )

    assert set(reading.sizes) == {"S"}


def test_a_size_spelt_differently_is_still_that_size(seeded):
    reading = normalise_chart_reading(
        {"measurements": [{"key": "width", "label_en": "Width"}],
         "sizes": {"s": {"width": 54}}},
        sizes=["S"],
    )

    assert reading.sizes == {"S": {"width": 54}}


def test_a_value_it_could_not_read_stays_blank_rather_than_zero(seeded):
    """The one that matters. A 0 cm chest is a number the bot would quote to a
    customer with a straight face."""
    reading = normalise_chart_reading(
        {"measurements": [{"key": "width", "label_en": "Width"},
                          {"key": "length", "label_en": "Length"}],
         "sizes": {"S": {"width": 54, "length": "--"}}},
        sizes=["S"],
    )

    assert reading.sizes == {"S": {"width": 54}}
    assert "length" not in reading.sizes["S"]


def test_a_column_nothing_filled_in_is_dropped_with_its_values(seeded):
    reading = normalise_chart_reading(
        {"measurements": [{"key": "width", "label_en": "Width"},
                          {"key": "sleeve", "label_en": "Sleeve"}],
         "sizes": {"S": {"width": 54}}},
        sizes=["S"],
    )

    assert [m["key"] for m in reading.measurements] == ["width"]


def test_a_nonsense_unit_falls_back_to_cm(seeded):
    reading = normalise_chart_reading(
        {"measurements": [{"key": "width", "label_en": "Width"}],
         "sizes": {"S": {"width": 54}}, "unit": "furlongs"},
        sizes=["S"],
    )

    assert reading.unit == "cm"


# --------------------------------------------------------------------------
# saving what the staff member confirmed
# --------------------------------------------------------------------------


def _save(client, **kwargs):
    body = {
        "product_id": "wanas-hoodie",
        "title": "WANAS Hoodie",
        "unit": "cm",
        "measurements": [{"key": "width", "label_en": "Width", "label_ar": "العرض"}],
        "sizes": {"S": {"width": 54}, "M": {"width": 56}},
    }
    body.update(kwargs)
    return client.post("/dashboard/api/shopify/size-charts", json=body)


def test_saving_requires_login(client):
    assert _save(client).status_code == 401


def test_a_saved_chart_is_what_the_bot_reads(logged_in, seeded, shopify):
    """The whole point: `get_size_chart` answers out of this, so the bot can
    quote a number instead of sending a picture and hoping."""
    res = _save(logged_in)
    assert res.status_code == 200, res.text
    seeded.expire_all()

    chart = get_chart("wanas-hoodie", seeded)
    assert chart["sizes"]["S"]["width"] == 54
    assert chart["measurements"][0]["label_ar"] == "العرض"
    assert seeded.get(Product, "wanas-hoodie").size_chart == res.json()["chart_id"]


def test_a_database_chart_overlays_the_file_on_the_same_id(logged_in, seeded, shopify):
    """Same shape as every other overlay here -- the file is the default, a
    row wins on the id. The twelve shipped charts keep working untouched."""
    assert get_chart("ringer-boxy-tee")["sizes"]["S"]["width"] == 54

    _save(logged_in, chart_id="ringer-boxy-tee", sizes={"S": {"width": 99}})
    seeded.expire_all()

    assert get_chart("ringer-boxy-tee", seeded)["sizes"]["S"]["width"] == 99
    # ...and without a session it is still the file, which is what the
    # offline scripts and `manage.py` read.
    assert get_chart("ringer-boxy-tee")["sizes"]["S"]["width"] == 54


def test_the_file_charts_are_all_still_listed(logged_in, seeded, shopify):
    _save(logged_in)
    seeded.expire_all()

    charts = all_charts(seeded)

    assert "ringer-boxy-tee" in charts
    assert "wanas-hoodie" in charts


def test_the_storefront_gets_the_table_too(logged_in, seeded, shopify):
    """Both metafields, so the theme renders a real bilingual table rather
    than only the picture."""
    _save(logged_in, image_file_gid="gid://shopify/MediaImage/chart")

    gid = shopify.variant_to_product["wanas-hoodie-s-olive"]
    assert shopify.chart_metafields[gid] == "gid://shopify/MediaImage/chart"
    assert shopify.chart_data[gid]["sizes"][0]["name"] == "S"


def test_a_blank_cell_is_never_saved_as_zero(logged_in, seeded, shopify):
    _save(logged_in, sizes={"S": {"width": 54}, "M": {"width": ""}})
    seeded.expire_all()

    chart = get_chart("wanas-hoodie", seeded)
    assert "M" not in chart["sizes"]
    assert chart["sizes"]["S"] == {"width": 54}


def test_a_column_the_chart_does_not_declare_is_dropped(logged_in, seeded, shopify):
    _save(logged_in, sizes={"S": {"width": 54, "sleeve": 20}})
    seeded.expire_all()

    assert get_chart("wanas-hoodie", seeded)["sizes"]["S"] == {"width": 54}


def test_a_chart_with_no_numbers_at_all_is_refused(logged_in, shopify):
    res = _save(logged_in, sizes={"S": {"width": ""}})

    assert res.status_code == 400


def test_a_chart_with_no_measurements_is_refused(logged_in, shopify):
    assert _save(logged_in, measurements=[]).status_code == 400


def test_an_unknown_product_is_refused(logged_in, shopify):
    assert _save(logged_in, product_id="no-such-product").status_code == 404


def test_who_read_the_numbers_is_recorded(logged_in, seeded, shopify):
    """"A person typed this" and "a model read this" are different claims, and
    the one that goes wrong goes wrong as a returned parcel."""
    _save(logged_in, read_from_image=True)
    seeded.expire_all()

    assert seeded.get(SizeChart, "wanas-hoodie").source == "vision"


def test_the_chart_still_saves_when_shopify_is_down(logged_in, seeded, shopify):
    """A chart the bot can quote but the storefront has not caught up on is a
    good outcome; a storefront table with nothing behind it is not."""
    shopify.down = True

    res = _save(logged_in)
    seeded.expire_all()

    assert res.status_code == 200
    assert res.json()["warnings"]
    assert get_chart("wanas-hoodie", seeded)["sizes"]["S"]["width"] == 54


def test_the_bot_quotes_a_dashboard_chart_like_any_other(logged_in, seeded, shopify):
    from assistant.tools.catalog_tools import get_size_chart

    _save(logged_in)
    seeded.expire_all()

    class Ctx:
        session = seeded

    answer = get_size_chart(Ctx(), "wanas-hoodie")

    assert answer["has_chart"] is True
    assert answer.get("image_only") is not True
    assert answer["sizes"]["S"]["width"] == 54


def test_a_provider_error_while_reading_is_not_an_exception(logged_in, monkeypatch):
    class Broken(ScriptedProvider):
        def read_size_chart(self, *a, **k):
            raise ProviderError("the vision model is down")

    providers.set_provider(Broken())
    try:
        res = _read(logged_in)
    finally:
        providers.set_provider(None)

    assert res.status_code == 503
    assert "down" in res.json()["detail"]
