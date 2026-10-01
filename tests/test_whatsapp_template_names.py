"""The WhatsApp template names come from the environment, with the already
approved ones as the default, so moving to `rehla_*` templates is a variable."""

from __future__ import annotations

import pytest

from config.settings import load_settings

NAMES = {
    "WHATSAPP_TEMPLATE_ORDER_UPDATE": ("whatsapp_template_order_update", "wanas_order_update"),
    "WHATSAPP_TEMPLATE_FEEDBACK_REQUEST": ("whatsapp_template_feedback_request", "wanas_feedback_request"),
    "WHATSAPP_TEMPLATE_ORDER_CONFIRMATION": ("whatsapp_template_order_confirmation", "wanas_order_confirmation"),
    "WHATSAPP_TEMPLATE_BACK_IN_STOCK": ("whatsapp_template_back_in_stock", "wanas_back_in_stock"),
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("variable", NAMES)
def test_unset_means_the_approved_name(variable):
    field, default = NAMES[variable]
    assert getattr(load_settings(), field) == default


@pytest.mark.parametrize("variable", NAMES)
def test_a_variable_replaces_it(variable, monkeypatch):
    field, _ = NAMES[variable]
    monkeypatch.setenv(variable, " rehla_new ")
    assert getattr(load_settings(), field) == "rehla_new"


@pytest.mark.parametrize("variable", NAMES)
def test_an_empty_variable_turns_the_template_off(variable, monkeypatch):
    field, _ = NAMES[variable]
    monkeypatch.setenv(variable, "")
    assert getattr(load_settings(), field) == ""
