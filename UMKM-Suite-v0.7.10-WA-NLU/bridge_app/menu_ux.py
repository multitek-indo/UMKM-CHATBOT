"""Pure helpers for safe WhatsApp menu/free-text routing."""
from __future__ import annotations

import re


# These screens offer numbered shortcuts, but must never lock the user into
# numeric-only input. Free text should continue to the normal POS/AI router.
PASSIVE_MENU_STATES = frozenset({
    "menu_main",
    "menu_pos",
    "menu_business",
    "menu_staff",
    "menu_reminder",
    "menu_export",
})


def numbered_choice(text: str) -> str | None:
    cleaned = (text or "").strip()
    for number in range(10):
        cleaned = cleaned.replace(f"{number}️⃣", str(number))
    match = re.fullmatch(r"\s*([0-9])(?:\s|[.)-])?\s*", cleaned)
    return match.group(1) if match else None


def selected_menu_action(payload: dict, text: str) -> str | None:
    choice = numbered_choice(text)
    if choice is None or choice == "0":
        return None
    actions = payload.get("actions") or []
    index = int(choice) - 1
    return str(actions[index]) if 0 <= index < len(actions) else None


def should_yield_to_free_text(action_type: str | None, text: str) -> bool:
    """Return True when a passive menu must release a non-numeric message."""
    return (
        str(action_type or "") in PASSIVE_MENU_STATES
        and numbered_choice(text) is None
    )


def parse_product_edit_request(text: str) -> tuple[bool, str | None]:
    """Recognize friendly edit-product phrases without involving the AI."""
    match = re.fullmatch(
        r"(?is)\s*(?:pos\s+)?(?:edit|ubah)\s+produk(?:\s+(.+?))?\s*",
        text or "",
    )
    if not match:
        return False, None
    return True, (match.group(1) or "").strip() or None
