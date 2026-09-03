"""Server-side role checks for UMKM Suite v0.7.4."""


class PermissionDenied(RuntimeError):
    pass


ROLE_PERMISSIONS = {
    "OWNER": {
        "finance.read", "finance.write", "finance.export",
        "pos.sell", "pos.void", "pos.read", "product.write", "stock.write",
        "opname.write", "staff.read", "staff.write", "business.write",
    },
    "ADMIN": {
        "finance.read", "finance.write", "finance.export",
        "pos.sell", "pos.void", "pos.read", "product.write", "stock.write",
        "opname.write", "staff.read", "staff.write",
    },
    "FINANCE": {"finance.read", "finance.write", "finance.export", "pos.read"},
    "CASHIER": {"pos.sell", "pos.read"},
    "INVENTORY": {"pos.read", "product.write", "stock.write", "opname.write"},
    "VIEWER": {"finance.read", "pos.read"},
}


def has_permission(role: str | None, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get((role or "").upper(), set())


def require_permission(ctx: dict, permission: str) -> None:
    role = str(ctx.get("role") or "").upper()
    if not has_permission(role, permission):
        raise PermissionDenied(
            f"Peran {role or '-'} tidak memiliki izin menggunakan fitur ini."
        )


def can_manage_role(actor_role: str, target_role: str) -> bool:
    actor = (actor_role or "").upper()
    target = (target_role or "").upper()
    if actor == "OWNER":
        return target in {"ADMIN", "FINANCE", "CASHIER", "INVENTORY", "VIEWER"}
    if actor == "ADMIN":
        return target in {"FINANCE", "CASHIER", "INVENTORY", "VIEWER"}
    return False
