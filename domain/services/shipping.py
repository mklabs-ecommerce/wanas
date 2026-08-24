"""Shipping rates.

A flat fee per governorate from a table staff edit. The governorate is a
picked value, never free text, because it sets the price -- and because every
spelling variant would otherwise become a different row.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import ShippingRate

#: Common ways a customer writes a governorate that are not its stored key or
#: its Arabic label: districts that sit inside a governorate, missing hamza
#: and ta-marbuta variants, and the English spellings people actually use.
#: Matching against these is what keeps the value picked rather than typed.
ALIASES: dict[str, str] = {
    # Cairo and its well-known districts
    "القاهره": "Cairo",
    "قاهرة": "Cairo",
    "مصر الجديدة": "Cairo",
    "مصر الجديده": "Cairo",
    "مدينة نصر": "Cairo",
    "مدينه نصر": "Cairo",
    "المعادي": "Cairo",
    "التجمع": "Cairo",
    "التجمع الخامس": "Cairo",
    "heliopolis": "Cairo",
    "nasr city": "Cairo",
    "maadi": "Cairo",
    "new cairo": "Cairo",
    "cairo governorate": "Cairo",
    "masr": "Cairo",
    # Giza
    "الجيزه": "Giza",
    "جيزة": "Giza",
    "6 اكتوبر": "Giza",
    "السادس من اكتوبر": "Giza",
    "الشيخ زايد": "Giza",
    "6th of october": "Giza",
    "october": "Giza",
    "sheikh zayed": "Giza",
    "dokki": "Giza",
    "الدقي": "Giza",
    "الهرم": "Giza",
    # Alexandria
    "اسكندرية": "Alexandria",
    "اسكندريه": "Alexandria",
    "الاسكندريه": "Alexandria",
    "alex": "Alexandria",
    # The rest, mostly hamza / ta-marbuta variants
    "القليوبيه": "Qalyubia",
    "بنها": "Qalyubia",
    "شبرا الخيمة": "Qalyubia",
    "الشرقيه": "Sharqia",
    "الزقازيق": "Sharqia",
    "الدقهليه": "Dakahlia",
    "المنصورة": "Dakahlia",
    "المنصوره": "Dakahlia",
    "mansoura": "Dakahlia",
    "الغربيه": "Gharbia",
    "طنطا": "Gharbia",
    "tanta": "Gharbia",
    "المحلة": "Gharbia",
    "المنوفيه": "Monufia",
    "شبين الكوم": "Monufia",
    "البحيره": "Beheira",
    "دمنهور": "Beheira",
    "كفر الشيح": "Kafr El Sheikh",
    "kafr el-sheikh": "Kafr El Sheikh",
    "kafr elsheikh": "Kafr El Sheikh",
    "دمياط": "Damietta",
    "بور سعيد": "Port Said",
    "portsaid": "Port Said",
    "الاسماعيليه": "Ismailia",
    "الاسماعيلية": "Ismailia",
    "السويس": "Suez",
    "شمال سينا": "North Sinai",
    "العريش": "North Sinai",
    "جنوب سينا": "South Sinai",
    "شرم الشيخ": "South Sinai",
    "دهب": "South Sinai",
    "sharm": "South Sinai",
    "sharm el sheikh": "South Sinai",
    "بني سويف": "Beni Suef",
    "beni sweif": "Beni Suef",
    "الفيوم": "Faiyum",
    "fayoum": "Faiyum",
    "المنيا": "Minya",
    "minia": "Minya",
    "اسيوط": "Asyut",
    "assiut": "Asyut",
    "سوهاج": "Sohag",
    "قنا": "Qena",
    "الاقصر": "Luxor",
    "اسوان": "Aswan",
    "البحر الاحمر": "Red Sea",
    "الغردقة": "Red Sea",
    "الغردقه": "Red Sea",
    "hurghada": "Red Sea",
    "الوادي الجديد": "New Valley",
    "الخارجة": "New Valley",
    "مطروح": "Matrouh",
    "مرسى مطروح": "Matrouh",
    "مرسي مطروح": "Matrouh",
    "marsa matrouh": "Matrouh",
}

_ARABIC_NORMALISE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ﻻ": "لا"})


def normalise(value: str) -> str:
    text = unicodedata.normalize("NFKC", (value or "").strip().lower())
    text = text.translate(_ARABIC_NORMALISE)
    text = re.sub(r"\bmuhafazat\b|\bmohafazat\b|\bgovernorate\b|\bمحافظة\b|\bمحافظه\b", " ", text)
    text = re.sub(r"^(el|al|ال)\s+", "", text)
    text = re.sub(r"[^\w؀-ۿ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def resolve(session: Session, value: str) -> str | None:
    """Customer text to a stored key, matched against English, Arabic and the
    alias list. Returns None when it is not in the list at all -- the list is
    the source of truth, never free text."""
    if not value:
        return None
    target = normalise(value)
    if not target:
        return None

    rates = session.scalars(select(ShippingRate)).all()
    for rate in rates:
        if target == normalise(rate.governorate) or target == normalise(rate.label_ar):
            return rate.governorate

    for alias, key in ALIASES.items():
        if target == normalise(alias):
            return key

    # Last resort: a containment match, so "شحن للقاهرة" still lands.
    for rate in rates:
        if normalise(rate.label_ar) and normalise(rate.label_ar) in target:
            return rate.governorate
        if normalise(rate.governorate) in target:
            return rate.governorate
    for alias, key in ALIASES.items():
        if normalise(alias) and normalise(alias) in target:
            return key
    return None


#: The 27 governorates grouped the way an Egyptian would group them.
#:
#: This exists for one practical reason: WhatsApp allows **ten rows** in a list
#: message and there are twenty-seven governorates, so a single picker is not
#: possible. Two steps are -- six regions, then at most eight governorates --
#: and picking twice beats typing an address line that has to be parsed.
#: Order is deliberate: where the orders actually come from, first.
REGIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("greater_cairo", "القاهرة الكبرى", ("Cairo", "Giza", "Qalyubia")),
    ("alexandria", "إسكندرية والساحل", ("Alexandria", "Beheira", "Matrouh")),
    (
        "delta",
        "الدلتا",
        ("Sharqia", "Dakahlia", "Gharbia", "Monufia", "Kafr El Sheikh", "Damietta"),
    ),
    ("canal", "القناة وسينا", ("Port Said", "Ismailia", "Suez", "North Sinai", "South Sinai")),
    (
        "saeed",
        "الصعيد",
        ("Beni Suef", "Faiyum", "Minya", "Asyut", "Sohag", "Qena", "Luxor", "Aswan"),
    ),
    ("remote", "البحر الأحمر والوادي الجديد", ("Red Sea", "New Valley")),
)


def regions() -> list[dict]:
    """The region list, as data rather than as six hardcoded strings."""
    return [
        {"region_id": key, "label_ar": label, "governorates": list(members)}
        for key, label, members in REGIONS
    ]


def resolve_region(value: str) -> str | None:
    """A region key from whatever the customer tapped or typed.

    Matched on the key and the Arabic label, through the same normalisation
    the governorate lookup uses -- an interactive reply comes back as its
    *title*, not its id, on some WhatsApp clients.
    """
    if not value:
        return None
    target = normalise(value)
    if not target:
        return None
    for key, label, _members in REGIONS:
        if target in {normalise(key), normalise(label)}:
            return key
    for key, label, _members in REGIONS:
        if normalise(label) and normalise(label) in target:
            return key
    return None


def governorates_in_region(session: Session, region: str) -> list[dict]:
    """The priced-or-not governorates of one region, with their Arabic labels.

    Reads the rate table rather than the constant so a governorate the shop
    removed from the table never appears in a picker the customer can tap.
    """
    members = next((m for key, _label, m in REGIONS if key == region), ())
    rows = []
    for key in members:
        rate = session.get(ShippingRate, key)
        if rate is None:
            continue
        rows.append(
            {
                "governorate": rate.governorate,
                "label_ar": rate.label_ar,
                "has_fee": rate.fee is not None,
            }
        )
    return rows


def valid_governorates(session: Session) -> list[str]:
    rows = session.scalars(select(ShippingRate).order_by(ShippingRate.governorate)).all()
    return [rate.governorate for rate in rows]


def get_fee(session: Session, governorate: str):
    """The stored fee, or None when the shop has not priced it yet."""
    rate = session.get(ShippingRate, governorate)
    return None if rate is None else rate.fee
