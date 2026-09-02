"""Naming an Instagram conversation after the person in it.

An `external_id` on Instagram is an IGSID -- seventeen digits that identify a
customer to Meta and to nobody else -- so the dashboard listed a column of
numbers. These pin the way back: the handle is read once, stored on the
identity, and preferred over the id everywhere the UI puts a title, without
ever displacing a real name the customer typed on an order.
"""

from __future__ import annotations

import pytest

from dashboard.web import customer_labels, handle_directory
from domain.models import Client
from domain.services import identities

CHANNEL = "instagram_dm"
IGSID = "17841400000000001"


# --------------------------------------------------------------------------
# Storing it
# --------------------------------------------------------------------------


def test_a_handle_is_stored_on_the_identity(db):
    identities.set_platform_profile(db, CHANNEL, IGSID, username="wanas_customer", name="Mona")
    db.commit()

    identity = identities.get(db, CHANNEL, IGSID)
    assert identity.username == "wanas_customer"
    assert identity.profile_name == "Mona"


def test_a_leading_at_is_stripped_on_the_way_in():
    """Stored bare, shown with the `@` -- so it round-trips through search
    and a URL without a stray character deciding whether it matches."""
    assert customer_labels(None, CHANNEL, IGSID, "@wanas_customer")["customer_handle"] == (
        "@wanas_customer"
    )


def test_a_blank_read_never_erases_a_handle_already_known(db):
    """The usual reason for a blank is a call that failed, not a handle that
    changed -- and overwriting a good value with the failure is how a
    conversation silently goes back to being a row of digits."""
    identities.set_platform_profile(db, CHANNEL, IGSID, username="wanas_customer")
    identities.set_platform_profile(db, CHANNEL, IGSID, username="", name="")
    db.commit()

    assert identities.get(db, CHANNEL, IGSID).username == "wanas_customer"


def test_one_lookup_per_customer_not_per_message(db):
    """`needs_platform_profile` is what keeps an extra round trip to Meta out
    of the webhook path for everyone who has already been looked up."""
    assert identities.needs_platform_profile(db, CHANNEL, IGSID) is True
    identities.set_platform_profile(db, CHANNEL, IGSID, username="wanas_customer")
    db.commit()
    assert identities.needs_platform_profile(db, CHANNEL, IGSID) is False


# --------------------------------------------------------------------------
# Showing it
# --------------------------------------------------------------------------


def test_the_handle_titles_a_conversation_with_no_client():
    """The case this exists for: most Instagram conversations have no order
    behind them, so there is no `Client` and nothing else to call them."""
    labels = customer_labels(None, CHANNEL, IGSID, "wanas_customer")
    assert labels["display_name"] == "@wanas_customer"
    assert labels["customer_name"] is None


def test_a_real_name_still_outranks_the_handle():
    """`Client.full_name` is a name a person typed onto an order. A handle is
    what they call themselves in public, which is second best, not better."""
    client = Client(client_id=1, full_name="Mona Ali", phone="201234567890", address="")
    labels = customer_labels(client, CHANNEL, IGSID, "wanas_customer")
    assert labels["display_name"] == "Mona Ali"
    # ...and the handle is still shown, just not as the title.
    assert labels["customer_handle"] == "@wanas_customer"


def test_without_a_handle_the_id_is_still_the_fallback():
    assert customer_labels(None, CHANNEL, IGSID, None)["display_name"] == IGSID


def test_an_igsid_is_never_read_as_a_phone_number():
    """An IGSID is all digits and `is_phone_number` says yes to it, which is
    exactly why `PHONE_CHANNELS` answers that by channel rather than by
    looking at the string. Calling a customer's IGSID their phone number
    would put a number staff might actually dial on the screen."""
    assert customer_labels(None, CHANNEL, IGSID, None)["customer_phone"] is None


def test_whatsapp_is_unaffected():
    """The handle is an Instagram fact; WhatsApp still falls back to the
    number, which for that channel the external_id genuinely is."""
    labels = customer_labels(None, "whatsapp", "201234567890")
    assert labels["display_name"] == "201234567890"
    assert labels["customer_phone"] == "201234567890"
    assert labels["customer_handle"] is None


def test_the_directory_lists_only_identities_that_have_one(db):
    identities.set_platform_profile(db, CHANNEL, IGSID, username="wanas_customer")
    identities.get_or_create(db, CHANNEL, "17841400000000002")
    db.commit()

    assert handle_directory(db) == {(CHANNEL, IGSID): "wanas_customer"}


# --------------------------------------------------------------------------
# Reading it off Meta
# --------------------------------------------------------------------------


@pytest.fixture()
def ig_client(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "999")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "token")
    from integrations.instagram.client import InstagramClient

    return InstagramClient(account_id="999", access_token="token")


def test_a_profile_meta_will_not_share_is_none_not_a_guess(ig_client, monkeypatch):
    """Every failure has to fall back to the id. A *wrong* handle on a
    conversation is worse than no handle at all -- it is staff answering
    somebody while looking at somebody else's name."""
    import httpx

    class _Refused:
        status_code = 400
        text = '{"error":{"message":"Unsupported get request"}}'

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Refused())
    assert ig_client.get_user_profile(IGSID) is None


def test_an_empty_profile_is_none(ig_client, monkeypatch):
    import httpx

    class _Empty:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": IGSID}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Empty())
    assert ig_client.get_user_profile(IGSID) is None


def test_a_readable_profile_comes_back_trimmed(ig_client, monkeypatch):
    import httpx

    class _Ok:
        status_code = 200
        text = ""

        def json(self):
            return {"username": " wanas_customer ", "name": " Mona ", "id": IGSID}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Ok())
    assert ig_client.get_user_profile(IGSID) == {"username": "wanas_customer", "name": "Mona"}


def test_an_unconfigured_client_never_calls_out(monkeypatch):
    import httpx

    from integrations.instagram.client import InstagramClient

    def _explode(*a, **k):
        raise AssertionError("should not have called Meta")

    monkeypatch.setattr(httpx, "get", _explode)
    assert InstagramClient(account_id="", access_token="").get_user_profile(IGSID) is None
