"""Секція звіту від одного джерела."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Section:
    source: str                       # roblox / steam / twitch / youtube
    title: str
    md: str = ""                      # повний markdown для reports/latest.md
    tg: list[str] = field(default_factory=list)   # короткі рядки для Telegram
    # слова, що ростуть у цьому джерелі: {слово: опис для збігів}
    rising: dict[str, str] = field(default_factory=dict)
    error: str | None = None
