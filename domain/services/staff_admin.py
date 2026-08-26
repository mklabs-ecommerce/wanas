"""Staff accounts and what each one is allowed to reach.

`domain/services/auth.py` answers "is this person who they say they are" and
deliberately stops there -- its docstring says one role, everyone who can log
in can do everything, and attribution is the only control. That was true while
the only people with a login were the two who built the shop. This module is
the second half: the owner adds a staff member and ticks which sections of the
dashboard that account may open.

**A missing value means full access, never none.** `Staff.role` and
`Staff.permissions` are both nullable and both arrived after accounts already
existed. If an absent role meant "staff with no permissions", the first deploy
would lock every existing account -- including the owner's -- out of the very
screen that hands permissions out, with no way back except editing the
database by hand. So NULL is read as unscoped, exactly as those accounts
behaved yesterday, and scoping is something an owner opts an account into.

Enforcement lives at the routes (`dashboard/guard.py::require_permission`),
not in the sidebar. Hiding a nav item is a courtesy to the person using the
dashboard; it is not a control, because the endpoint it points at is one
`fetch` away either way.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Staff
from domain.services.auth import hash_password

#: The role that is never scoped. Anything else is.
OWNER_ROLE = "owner"
STAFF_ROLE = "staff"
ROLES = (OWNER_ROLE, STAFF_ROLE)


@dataclass(frozen=True)
class Permission:
    #: Both languages, for the same reason `runtime_flags.FlagInfo` carries
    #: both: these strings are server data that travels to the browser, so a
    #: translation kept anywhere else would drift the moment a permission is
    #: added.
    key: str
    label_ar: str
    description_ar: str
    label_en: str
    description_en: str


#: One key per dashboard section, matching the routers they gate. Kept as a
#: flat list rather than nested groups: a permission that maps to exactly one
#: thing a person can see is one an owner can tick without a manual.
PERMISSIONS: tuple[Permission, ...] = (
    Permission("inbox", "الوارد والمحادثات", "قراءة المحادثات والرد عليها",
               "Inbox and conversations", "Read conversations and reply to them"),
    Permission("orders", "الطلبات", "عرض الطلبات وشحنها وإلغاؤها",
               "Orders", "View, fulfil and cancel orders"),
    Permission("products", "المنتجات", "إنشاء المنتجات وتعديلها",
               "Products", "Create and edit products"),
    Permission("inventory", "المخزون", "عرض المخزون وتعديل الكميات",
               "Inventory", "View stock and edit quantities"),
    Permission("collections", "المجموعات", "تجميعات شوبيفاي وأعضاؤها",
               "Collections", "Shopify collections and their members"),
    Permission("customers", "العملاء", "بيانات العملاء وطلباتهم",
               "Customers", "Customer details and their orders"),
    Permission("queue", "قائمة المراجعة", "تبديل المنتجات والتنبيهات",
               "Review queue", "Item swaps and alerts"),
    Permission("analytics", "التحليلات", "الأرقام والرسوم البيانية",
               "Analytics", "The figures and the charts"),
    Permission("settings", "الإعدادات", "المزايا وأرقام الاختبار",
               "Settings", "Features and test numbers"),
    Permission("manage_staff", "إدارة الفريق", "إضافة أعضاء وتحديد صلاحياتهم",
               "Manage the team", "Add members and set their permissions"),
)

PERMISSION_KEYS = tuple(p.key for p in PERMISSIONS)


def is_owner(staff: Staff) -> bool:
    return (staff.role or OWNER_ROLE) == OWNER_ROLE


def permission_keys(staff: Staff) -> tuple[str, ...]:
    """Everything this account may reach. An owner -- including a
    pre-permissions account whose role is still NULL -- gets the whole list."""
    if is_owner(staff):
        return PERMISSION_KEYS
    granted = staff.permissions
    if granted is None:
        # Scoped to a role but never given a permission list: same
        # grandfathering as an absent role. Ticking nothing is a deliberate
        # act, and it is stored as `[]`, which is not this case.
        return PERMISSION_KEYS
    return tuple(key for key in PERMISSION_KEYS if key in granted)


def has_permission(staff: Staff, key: str) -> bool:
    return key in permission_keys(staff)


def _normalise_permissions(values) -> list[str] | None:
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("permissions must be a list of permission keys")
    unknown = [v for v in values if v not in PERMISSION_KEYS]
    if unknown:
        raise ValueError(f"unknown permission(s): {', '.join(sorted(map(str, unknown)))}")
    # Stored in the catalog's own order so two accounts with the same access
    # compare equal, and an empty tick-list stays `[]` -- meaningfully
    # different from NULL, which means "not scoped".
    return [key for key in PERMISSION_KEYS if key in values]


def _normalise_role(role: str | None) -> str:
    role = (role or STAFF_ROLE).strip().lower()
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    return role


def list_staff(session: Session) -> list[Staff]:
    return list(session.scalars(select(Staff).order_by(Staff.staff_id)).all())


def create(
    session: Session,
    username: str,
    password: str,
    *,
    role: str = STAFF_ROLE,
    permissions: list[str] | None = None,
) -> Staff:
    """Like `auth.create_staff`, plus the scope. Kept here rather than
    widening that function's signature: `manage.py create-staff` and the
    dashboard both call this one, and `auth.py` stays about credentials."""
    username = username.strip()
    if not username:
        raise ValueError("username is required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if session.scalar(select(Staff).where(Staff.username == username)):
        raise ValueError(f"staff user {username!r} already exists")

    role = _normalise_role(role)
    staff = Staff(
        username=username,
        password_hash=hash_password(password),
        is_active=True,
        role=role,
        # An owner is never scoped, so storing a list for one would be a
        # second source of truth that `permission_keys` ignores anyway.
        permissions=None if role == OWNER_ROLE else (_normalise_permissions(permissions) or []),
    )
    session.add(staff)
    session.flush()
    return staff


def update(
    session: Session,
    staff: Staff,
    *,
    role: str | None = None,
    permissions: list[str] | None = None,
    is_active: bool | None = None,
    password: str | None = None,
) -> Staff:
    if role is not None:
        staff.role = _normalise_role(role)
        if staff.role == OWNER_ROLE:
            staff.permissions = None
    if permissions is not None and (staff.role or OWNER_ROLE) != OWNER_ROLE:
        staff.permissions = _normalise_permissions(permissions)
    if is_active is not None:
        staff.is_active = bool(is_active)
    if password is not None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        staff.password_hash = hash_password(password)
    session.flush()
    return staff


def owner_count(session: Session, *, exclude: int | None = None) -> int:
    """Active owners, optionally ignoring one account. Nothing may take the
    last one away: a shop with no owner has nobody who can hand out
    permissions, and the only fix is a shell on the database."""
    rows = session.scalars(select(Staff).where(Staff.is_active.is_(True))).all()
    return sum(1 for s in rows if is_owner(s) and s.staff_id != exclude)


def summary(staff: Staff) -> dict:
    return {
        "staff_id": staff.staff_id,
        "username": staff.username,
        "role": staff.role or OWNER_ROLE,
        # What the account can actually reach, resolved -- not the raw
        # column, which is NULL for every grandfathered account and would
        # render as "no access" in the UI while the routes let them through.
        "permissions": list(permission_keys(staff)),
        "is_active": bool(staff.is_active),
        "created_at": staff.created_at.isoformat() if staff.created_at else None,
    }


__all__ = [
    "OWNER_ROLE",
    "STAFF_ROLE",
    "ROLES",
    "PERMISSIONS",
    "PERMISSION_KEYS",
    "Permission",
    "is_owner",
    "permission_keys",
    "has_permission",
    "list_staff",
    "create",
    "update",
    "owner_count",
    "summary",
]
