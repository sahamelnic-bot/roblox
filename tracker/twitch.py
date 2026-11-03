"""Twitch (опційно): які ігри набирають глядачів. Найраніший сигнал для Steam.

Потрібні TWITCH_CLIENT_ID і TWITCH_CLIENT_SECRET (безкоштовно на dev.twitch.tv).
Беремо топ-стріми (до 1500) і сумуємо глядачів по грі.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import common as c
from .report import Section

TOKEN = "https://id.twitch.tv/oauth2/token"
STREAMS = "https://api.twitch.tv/helix/streams"
FIELDS = ["game_id", "name", "viewers", "channels"]
SKIP = {"Just Chatting", "IRL", "Music", "Art", "Talk Shows & Podcasts", "Pools, Hot Tubs, and Beaches",
        "ASMR", "Special Events", "Sports", "Travel & Outdoors", "Politics", "Software and Game Development"}


def collect(data_dir: Path, client_id: str, secret: str, now: datetime, pages: int = 15) -> str:
    tok = c.http_json(TOKEN, data={"client_id": client_id, "client_secret": secret,
                                   "grant_type": "client_credentials"})["access_token"]
    h = {"Client-Id": client_id, "Authorization": f"Bearer {tok}"}
    agg: dict[str, dict] = {}
    cursor = None
    for _ in range(pages):
        params = {"first": 100, **({"after": cursor} if cursor else {})}
        data = c.http_json(STREAMS, params, headers=h)
        for s in data.get("data", []):
            gid, name = s.get("game_id"), s.get("game_name", "")
            if not gid or name in SKIP:
                continue
            e = agg.setdefault(gid, {"game_id": gid, "name": name, "viewers": 0, "channels": 0})
            e["viewers"] += s.get("viewer_count", 0)
            e["channels"] += 1
        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    c.append_csv(data_dir, now, list(agg.values()), FIELDS)
    return f"Twitch: ігор {len(agg)}"


def analyze(data_dir: Path, now: datetime, min_viewers: int = 500, top: int = 15) -> Section:
    items = [m for k, rows in c.load_csv(data_dir, now, 4, "game_id").items()
             if (m := c.momentum(k, rows, "viewers", now))]
    med = c.apply_market(items, min_viewers)
    big = [m for m in items if m.cur >= min_viewers]
    rockets = sorted((m for m in big if m.rel24 is not None), key=lambda m: (m.rel24, m.streak), reverse=True)[:top]

    L = ["## 🟪 Twitch (глядачі по іграх)", "",
         f"Ігор: **{len(items)}**, з ≥ {min_viewers} глядачів: **{len(big)}**. Ринок за 24 год: {c.pct(med.get('g24'))}.",
         "", "| # | Гра | Глядачі | Добу тому | Множник | Швидше ринку | Стрімерів | Діб росту |",
         "|---|---|---|---|---|---|---|---|"]
    for i, m in enumerate(rockets, 1):
        L.append(f"| {i} | {c.cell(m.name)} | {c.n(m.cur)} | {c.n(m.v24)} | {c.xs(m.x24)} | {c.pct(m.rel24)} | "
                 f"{c.n(c.num(m.last, 'channels'))} | {c.streak_icon(m.streak) or '–'} |")
    if not rockets:
        L.append("| | _ще немає даних за добу_ | | | | | | |")
    tg = ["🟪 Twitch – ігри, що ростуть:"]
    tg += [f"{i}. {m.name[:40]} – {c.n(m.cur)} глядачів ({c.xs(m.x24)})" for i, m in enumerate(rockets[:5], 1)] \
        or ["ще немає даних за добу"]
    rising = {}
    for m in rockets[:10]:
        for w in c.tokenize(m.name):
            rising.setdefault(w, f"Twitch-ріст «{m.name[:40]}»")
    return Section("twitch", "Twitch", "\n".join(L), tg, rising)
