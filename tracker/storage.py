"""Знімки зберігаються як CSV по днях: data/snapshots/YYYY-MM-DD.csv.

Текст замість SQLite – щоб git-диффи були маленькі, а файли можна
відкрити в Excel/Google Sheets.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIELDS = [
    "ts", "universe_id", "name", "playing", "visits", "favorites",
    "up", "down", "created", "updated", "genre", "root_place_id", "sorts",
]


@dataclass
class Snap:
    ts: datetime
    universe_id: int
    name: str
    playing: int
    visits: int
    favorites: int
    up: int
    down: int
    created: str
    updated: str
    genre: str
    root_place_id: str
    sorts: str


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(data_dir: Path, ts: datetime, rows: list[dict]) -> Path:
    path = data_dir / f"{ts.astimezone(timezone.utc):%Y-%m-%d}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS} | {"ts": fmt_ts(ts)})
    return path


def load(data_dir: Path, now: datetime, days: int = 4) -> dict[int, list[Snap]]:
    """Усі знімки за останні `days` днів, згруповані по грі й відсортовані за часом."""
    by_game: dict[int, list[Snap]] = {}
    for i in range(days + 1):
        path = data_dir / f"{(now - timedelta(days=i)).astimezone(timezone.utc):%Y-%m-%d}.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    s = Snap(
                        ts=parse_ts(r["ts"]),
                        universe_id=int(r["universe_id"]),
                        name=r.get("name", ""),
                        playing=int(r.get("playing") or 0),
                        visits=int(r.get("visits") or 0),
                        favorites=int(r.get("favorites") or 0),
                        up=int(r.get("up") or 0),
                        down=int(r.get("down") or 0),
                        created=r.get("created", ""),
                        updated=r.get("updated", ""),
                        genre=r.get("genre", ""),
                        root_place_id=r.get("root_place_id", ""),
                        sorts=r.get("sorts", ""),
                    )
                except (KeyError, ValueError):
                    continue
                by_game.setdefault(s.universe_id, []).append(s)
    for snaps in by_game.values():
        snaps.sort(key=lambda s: s.ts)
    return by_game
