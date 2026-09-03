from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ProfileSubmission:
    nickname: str
    position_code: str | None
    faction_code: str | None
    position_label: str | None
    legacy: bool = False


@dataclass(frozen=True, slots=True)
class PositionSpec:
    code: str
    label: str
    permissions: frozenset[str]
    external: bool = False


# Permissions are intentionally fine-grained even though only a subset is used in v5.
# This lets future modules (events, diplomacy, targets, news) plug into the same model.
POSITIONS: dict[str, PositionSpec] = {
    "leader": PositionSpec(
        "leader",
        "Лидер",
        frozenset({"*"}),
    ),
    "deputy_leader": PositionSpec(
        "deputy_leader",
        "Заместитель лидера",
        frozenset(
            {
                "storage.manage",
                "storage.view",
                "market.create",
                "market.manage",
                "delivery.manage",
                "gp_stock.manage",
                "roles.manage",
                "diplomacy.manage",
                "events.manage",
                "targets.manage",
                "news.manage",
                "info.manage",
            }
        ),
    ),
    "trader": PositionSpec(
        "trader",
        "Торговец",
        frozenset({"market.create", "market.manage", "delivery.manage", "gp_stock.manage", "storage.view"}),
    ),
    "diplomat": PositionSpec(
        "diplomat",
        "Законодатель (Дипломат)",
        frozenset({"market.create", "diplomacy.manage", "news.manage", "info.manage", "storage.view"}),
    ),
    "storekeeper": PositionSpec(
        "storekeeper",
        "Кладовщик",
        frozenset({"market.create", "storage.manage", "storage.view"}),
    ),
    "sho_commander": PositionSpec(
        "sho_commander",
        "Командир ШО",
        frozenset({"market.create", "events.manage", "targets.manage", "storage.view"}),
    ),
    "private": PositionSpec(
        "private",
        "Рядовой",
        frozenset({"market.create", "storage.view"}),
    ),
    "external_leader": PositionSpec(
        "external_leader",
        "Лидер внешней группировки",
        frozenset(),
        external=True,
    ),
    "external_deputy": PositionSpec(
        "external_deputy",
        "Заместитель внешней группировки",
        frozenset(),
        external=True,
    ),
}


ROLE_CAPACITIES: dict[str, int] = {
    "leader": 1,
    "deputy_leader": 5,
}

INTERNAL_POSITION_ORDER: tuple[str, ...] = (
    "leader",
    "deputy_leader",
    "trader",
    "diplomat",
    "storekeeper",
    "sho_commander",
    "private",
)

FACTIONS: dict[str, str] = {
    "mercenaries": "Наёмники",
    "duty": "Долг",
    "monolith": "Монолит",
    "sin": "Грех",
    "renegades": "Ренегаты",
    "bandits": "Бандиты",
    "scientists": "Учёные",
    "freedom": "Свобода",
    "clear_sky": "Чистое Небо",
}

FACTION_ROLE_NAMES: dict[str, str] = {
    "mercenaries": "Наёмников",
    "duty": "Долга",
    "monolith": "Монолита",
    "sin": "Греха",
    "renegades": "Ренегатов",
    "bandits": "Бандитов",
    "scientists": "Учёных",
    "freedom": "Свободы",
    "clear_sky": "Чистого Неба",
}


def _norm(value: str) -> str:
    value = value.strip().casefold().replace("ё", "е")
    value = re.sub(r"\s+", " ", value)
    value = value.replace("–", "-").replace("—", "-")
    return value


INTERNAL_ALIASES: dict[str, str] = {
    _norm("Лидер"): "leader",
    _norm("Заместитель лидера"): "deputy_leader",
    _norm("Зам лидера"): "deputy_leader",
    _norm("Торговец"): "trader",
    _norm("Торговец ГП"): "trader",
    _norm("Законодатель"): "diplomat",
    _norm("Дипломат"): "diplomat",
    _norm("Законодатель (Дипломат)"): "diplomat",
    _norm("Кладовщик"): "storekeeper",
    _norm("Командир ШО"): "sho_commander",
    _norm("Рядовой"): "private",
}

FACTION_ALIASES: dict[str, str] = {}
for code, label in FACTIONS.items():
    aliases = {label}
    if code == "mercenaries":
        aliases.update({"Наемники", "Наёмников", "Наемников"})
    elif code == "duty":
        aliases.update({"Долга"})
    elif code == "monolith":
        aliases.update({"Монолита"})
    elif code == "sin":
        aliases.update({"Греха"})
    elif code == "renegades":
        aliases.update({"Ренегатов"})
    elif code == "bandits":
        aliases.update({"Бандитов"})
    elif code == "scientists":
        aliases.update({"Ученых", "Учёных"})
    elif code == "freedom":
        aliases.update({"Свободы"})
    elif code == "clear_sky":
        aliases.update({"Чистого Неба"})
    for alias in aliases:
        FACTION_ALIASES[_norm(alias)] = code


def _strip_list_prefix(line: str) -> str:
    return re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).strip()


def validate_nickname(value: str) -> str | None:
    nickname = _strip_list_prefix(value)
    if not 2 <= len(nickname) <= 40:
        return None
    low = nickname.casefold()
    if "http://" in low or "https://" in low or "t.me/" in low:
        return None
    if nickname.startswith("/"):
        return None
    return nickname


def parse_position(value: str) -> tuple[str, str | None, str] | None:
    raw = value.strip()
    normalized = _norm(raw)
    internal = INTERNAL_ALIASES.get(normalized)
    if internal:
        return internal, None, POSITIONS[internal].label

    # External examples supported:
    #   Лидер Долга
    #   Заместитель Долга
    #   Заместитель лидера Долга
    # Also accepts nominative group names (e.g. "Лидер Монолит").
    external_patterns = (
        (r"^лидер\s+(.+)$", "external_leader"),
        (r"^заместитель(?:\s+лидера)?\s+(.+)$", "external_deputy"),
        (r"^зам\.?\s*(?:лидера\s+)?(.+)$", "external_deputy"),
    )
    for pattern, role_code in external_patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        faction_code = FACTION_ALIASES.get(_norm(match.group(1)))
        if not faction_code:
            continue
        prefix = "Лидер" if role_code == "external_leader" else "Заместитель"
        return role_code, faction_code, f"{prefix} {FACTION_ROLE_NAMES[faction_code]}"
    return None


def parse_profile(text: str | None, *, allow_legacy: bool = False) -> ProfileSubmission | None:
    if not text:
        return None
    lines = [_strip_list_prefix(line) for line in text.splitlines() if line.strip()]
    if not lines or lines[0].startswith("/"):
        return None
    nickname = validate_nickname(lines[0])
    if not nickname:
        return None
    if len(lines) < 2:
        if allow_legacy:
            return ProfileSubmission(nickname, None, None, None, legacy=True)
        return None
    parsed = parse_position(lines[1])
    if not parsed:
        return ProfileSubmission(nickname, "__invalid__", None, lines[1], legacy=False)
    position_code, faction_code, label = parsed
    return ProfileSubmission(nickname, position_code, faction_code, label, legacy=False)


def position_display(position_code: str | None, faction_code: str | None = None) -> str:
    if not position_code:
        return "Не назначена"
    if position_code in {"external_leader", "external_deputy"}:
        faction = FACTION_ROLE_NAMES.get(faction_code or "", faction_code or "неизвестной группировки")
        return f"{'Лидер' if position_code == 'external_leader' else 'Заместитель'} {faction}"
    spec = POSITIONS.get(position_code)
    return spec.label if spec else position_code


def is_external_position(position_code: str | None) -> bool:
    return bool(position_code and POSITIONS.get(position_code) and POSITIONS[position_code].external)


def has_position_permission(position_code: str | None, permission: str) -> bool:
    # No confirmed position means no functional permissions. This prevents a user from
    # bypassing the role system by registering only a nickname in private chat.
    if not position_code:
        return False
    spec = POSITIONS.get(position_code)
    if not spec:
        return False
    return "*" in spec.permissions or permission in spec.permissions


def allowed_position_lines() -> list[str]:
    return [
        "Лидер",
        "Заместитель лидера",
        "Торговец",
        "Законодатель (Дипломат)",
        "Кладовщик",
        "Командир ШО",
        "Рядовой",
        "Лидер <группировки>",
        "Заместитель <группировки>",
    ]


def external_faction_names() -> Iterable[str]:
    return FACTIONS.values()
