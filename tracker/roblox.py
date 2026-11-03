"""Roblox: збір з публічних ендпоінтів + аналіз динаміки.

Ендпоінти explore-api недокументовані, тому ігри з відповіді витягуються
рекурсивно (будь-який dict з universeId) – переживає дрібні зміни формату.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import common as c
from .report import Section

EXPLORE_SORTS = "https://apis.roblox.com/explore-api/v1/get-sorts"
EXPLORE_SORT_CONTENT = "https://apis.roblox.com/explore-api/v1/get-sort-content"
GAMES = "https://games.roblox.com/v1/games"
VOTES = "https://games.roblox.com/v1/games/votes"

FIELDS = ["universe_id", "name", "playing", "visits", "favorites", "up", "down",
          "created", "updated", "genre", "root_place_id", "sorts"]
MAX_TRACKED = 3000
KEEP_MIN = 30          # гру, що випала з головної, трекаємо далі, якщо онлайн ≥ цього


# ---------- збір ----------

def extract_games(payload: Any) -> Iterator[dict]:
    if isinstance(payload, dict):
        if "universeId" in payload:
            yield payload
        for v in payload.values():
            if isinstance(v, (dict, list)):
                yield from extract_games(v)
    elif isinstance(payload, list):
        for item in payload:
            yield from extract_games(item)


def discover(max_sort_pages: int = 5, max_content_pages: int = 3) -> dict[int, set[str]]:
    base = {"sessionId": str(uuid.uuid4()), "device": "computer", "country": "all"}
    found: dict[int, set[str]] = {}

    def add(games: Iterable[dict], sort_id: str) -> None:
        for g in games:
            try:
                found.setdefault(int(g["universeId"]), set()).add(sort_id)
            except (TypeError, ValueError, KeyError):
                pass

    token = None
    for _ in range(max_sort_pages):
        data = c.http_json(EXPLORE_SORTS, {**base, **({"sortsPageToken": token} if token else {})})
        if not isinstance(data, dict):
            break
        for sort in data.get("sorts", []):
            sort_id = str(sort.get("sortId") or sort.get("topicId") or "unknown")
            add(extract_games(sort), sort_id)
            page = sort.get("nextPageToken")
            for _ in range(max_content_pages):
                if not page:
                    break
                try:
                    content = c.http_json(EXPLORE_SORT_CONTENT, {**base, "sortId": sort_id, "pageToken": page})
                except Exception:
                    break
                add(extract_games(content), sort_id)
                page = content.get("nextPageToken") if isinstance(content, dict) else None
        token = data.get("nextSortsPageToken")
        if not token:
            break
    return found


def details(universe_ids: Iterable[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for chunk in c.chunks(sorted(set(universe_ids)), 50):
        q = {"universeIds": ",".join(map(str, chunk))}
        for g in c.http_json(GAMES, q).get("data", []):
            out[int(g["id"])] = {
                "name": g.get("name", ""), "playing": g.get("playing") or 0,
                "visits": g.get("visits") or 0, "favorites": g.get("favoritedCount") or 0,
                "created": g.get("created", ""), "updated": g.get("updated", ""),
                "genre": g.get("genre", ""), "root_place_id": g.get("rootPlaceId", ""),
                "up": 0, "down": 0,
            }
        try:
            for v in c.http_json(VOTES, q).get("data", []):
                if int(v["id"]) in out:
                    out[int(v["id"])].update(up=v.get("upVotes") or 0, down=v.get("downVotes") or 0)
        except Exception:
            pass
    return out


def read_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {int(x) for line in path.read_text(encoding="utf-8").splitlines()
            if (x := line.split("#", 1)[0].strip()).isdigit()}


def collect(data_dir: Path, watchlist: Path, now: datetime) -> str:
    found = discover()
    history = c.load_csv(data_dir, now, 3, "universe_id")
    recent = {int(k) for k, rows in history.items() if (c.num(rows[-1], "playing") or 0) >= KEEP_MIN}
    keep = set(found) | read_ids(watchlist)
    rest = sorted(recent - keep, key=lambda u: c.num(history[str(u)][-1], "playing") or 0, reverse=True)
    tracked = keep | set(rest[: max(0, MAX_TRACKED - len(keep))])
    det = details(tracked)
    rows = [{"universe_id": uid, "sorts": ",".join(sorted(found.get(uid, ()))), **d} for uid, d in det.items()]
    c.append_csv(data_dir, now, rows, FIELDS)
    return f"Roblox: на головній {len(found)}, записано {len(rows)}"


# ---------- аналіз ----------

@dataclass
class Game:
    m: c.Momentum
    age_days: float | None
    like: float | None
    visits_24h: int | None
    visits_prev: int | None
    session_min: float | None   # середня тривалість сесії ≈ онлайн*1440/візити за добу
    update_spike: bool
    url: str
    tokens: set[str]


def _age(created: str, at: datetime) -> float | None:
    try:
        return (at - c.parse_ts(created)).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def compute(data_dir: Path, now: datetime, min_ccu: int) -> tuple[list[Game], dict]:
    games: list[Game] = []
    for key, rows in c.load_csv(data_dir, now, 4, "universe_id").items():
        m = c.momentum(key, rows, "playing", now)
        if m is None:
            continue
        last = m.last
        t = last["ts"]
        d24 = c.find_at(rows, t - timedelta(hours=24), timedelta(hours=3))
        d48 = c.find_at(rows, t - timedelta(hours=48), timedelta(hours=3))
        v24 = (c.num(last, "visits") - c.num(d24, "visits")) if d24 else None
        vprev = (c.num(d24, "visits") - c.num(d48, "visits")) if d24 and d48 else None
        up, down = c.num(last, "up") or 0, c.num(last, "down") or 0
        age = _age(last.get("created", ""), t)
        upd_age = _age(last.get("updated", ""), t)
        place = last.get("root_place_id", "")
        games.append(Game(
            m=m, age_days=age,
            like=up / (up + down) if up + down else None,
            visits_24h=v24, visits_prev=vprev,
            session_min=(m.cur * 1440 / v24) if v24 and v24 > 0 else None,
            update_spike=bool(age and age > 60 and upd_age is not None and upd_age < 3 and (m.g24 or 0) > 0.5),
            url=f"https://www.roblox.com/games/{place}" if place else "",
            tokens=c.tokenize(m.name),
        ))
    med = c.apply_market([g.m for g in games], min_ccu)
    return games, med


@dataclass
class Topic:
    word: str
    games: int
    clones_14d: int
    cur: int
    old: int
    growth: float
    examples: list[str]


def topics(games: list[Game], min_total: int = 1000) -> list[Topic]:
    agg: dict[str, list[Game]] = {}
    for g in games:
        for t in g.tokens:
            agg.setdefault(t, []).append(g)
    out = []
    for word, gs in agg.items():
        with_hist = [g for g in gs if g.m.v24 is not None]
        if len(gs) < 2 or not with_hist:
            continue
        cur = sum(g.m.cur for g in with_hist)
        old = sum(g.m.v24 or 0 for g in with_hist)
        if cur < min_total:
            continue
        clones = sum(1 for g in gs if g.age_days is not None and g.age_days <= 14)
        ex = [g.m.name for g in sorted(gs, key=lambda g: g.m.cur, reverse=True)[:3]]
        out.append(Topic(word, len(gs), clones, cur, old, (cur - old) / max(old, 1), ex))
    out.sort(key=lambda t: t.growth, reverse=True)
    return out


def _age_s(a: float | None) -> str:
    return "–" if a is None else f"{a:.0f} дн"


def analyze(data_dir: Path, now: datetime, min_ccu: int = 300, top: int = 20) -> Section:
    games, med = compute(data_dir, now, min_ccu)
    big = [g for g in games if g.m.cur >= min_ccu]
    rockets = sorted((g for g in big if g.m.rel24 is not None and not g.update_spike),
                     key=lambda g: (g.m.rel24, g.m.streak), reverse=True)[:top]
    spikes = sorted((g for g in big if g.update_spike), key=lambda g: g.m.g24 or 0, reverse=True)[:10]
    early = sorted((g for g in big if g.m.rel6 is not None), key=lambda g: g.m.rel6, reverse=True)[:top]
    fresh = sorted((g for g in big if g.age_days is not None and g.age_days <= 30),
                   key=lambda g: g.m.cur, reverse=True)[:top]
    tps = topics(games)

    L = [f"## 🟥 Roblox",
         "",
         f"Ігор у трекінгу: **{len(games)}**, з онлайном ≥ {min_ccu}: **{len(big)}**. "
         f"Ринок за 24 год: {c.pct(med.get('g24'))}, за 6 год: {c.pct(med.get('g6'))}.",
         "",
         "### 🚀 Ракети (24 год, відносно ринку)",
         "",
         "| # | Гра | Онлайн | Добу тому | Множник | Швидше ринку | Діб росту | Вік | Сесія | Лайки |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for i, g in enumerate(rockets, 1):
        L.append(f"| {i} | {c.link(g.m.name, g.url)} | {c.n(g.m.cur)} | {c.n(g.m.v24)} | {c.xs(g.m.x24)} | "
                 f"{c.pct(g.m.rel24)} | {c.streak_icon(g.m.streak) or '–'} | {_age_s(g.age_days)} | "
                 f"{'–' if g.session_min is None else f'{g.session_min:.0f} хв'} | "
                 f"{'–' if g.like is None else f'{g.like * 100:.0f}%'} |")
    if not rockets:
        L.append("| | _ще немає даних за добу_ | | | | | | | | |")

    L += ["", "### ⚡ Ранній сигнал (6 год, відносно ринку)", "",
          "| # | Гра | Онлайн | 6 год тому | Ріст | Швидше ринку |", "|---|---|---|---|---|---|"]
    for i, g in enumerate(early, 1):
        L.append(f"| {i} | {c.link(g.m.name, g.url)} | {c.n(g.m.cur)} | {c.n(g.m.v6)} | {c.pct(g.m.g6)} | {c.pct(g.m.rel6)} |")

    L += ["", "### 🆕 Нові (≤ 30 днів) з онлайном", "",
          "| # | Гра | Онлайн | Множник 24 год | Вік |", "|---|---|---|---|---|"]
    for i, g in enumerate(fresh, 1):
        L.append(f"| {i} | {c.link(g.m.name, g.url)} | {c.n(g.m.cur)} | {c.xs(g.m.x24)} | {_age_s(g.age_days)} |")

    L += ["", "### 🧠 Теми / механіки (слова в назвах)", "",
          "| Слово | Ігор | Нових за 14 дн | Онлайн | Добу тому | Ріст | Приклади |", "|---|---|---|---|---|---|---|"]
    for t in tps[:top]:
        L.append(f"| **{t.word}** | {t.games} | {t.clones_14d} | {c.n(t.cur)} | {c.n(t.old)} | {c.pct(t.growth)} | "
                 f"{'; '.join(c.cell(e, 35) for e in t.examples)} |")

    if spikes:
        L += ["", "### 🔧 Сплески від оновлень (старі ігри, не новий тренд)", ""]
        L += [f"- {c.link(g.m.name, g.url)} – {c.n(g.m.cur)} онлайн, {c.pct(g.m.g24)}" for g in spikes]

    tg = ["🟥 Roblox – ракети:"]
    tg += [f"{i}. {g.m.name[:40]} – {c.n(g.m.cur)} ({c.xs(g.m.x24)}, {c.pct(g.m.rel24)} vs ринок) {c.streak_icon(g.m.streak)}"
           for i, g in enumerate(rockets[:8], 1)] or ["ще немає даних за добу"]
    hot_topics = [t for t in tps if t.growth > 0.2][:5]
    if hot_topics:
        tg.append("Теми: " + ", ".join(f"{t.word} {c.pct(t.growth)} ({t.clones_14d} нових)" for t in hot_topics))

    rising: dict[str, str] = {}
    for t in hot_topics:
        rising[t.word] = f"Roblox-тема «{t.word}» {c.pct(t.growth)}"
    for g in rockets[:10]:
        for w in g.tokens:
            rising.setdefault(w, f"Roblox-ракета «{g.m.name[:40]}»")
    return Section("roblox", "Roblox", "\n".join(L), tg, rising)
