"""Аналіз динаміки: хто росте найшвидше, а не хто найбільший.

Ключова проблема: онлайн у Roblox має добовий цикл (вечір у США → пік).
Тому:
  * основне порівняння – з тим самим часом доби вчора (24 год);
  * короткі вікна (6 год) нормуються на медіану ринку: якщо всі ігри
    зараз +40% бо вечір, а гра +120% – її «відрив від ринку» = +80 п.п.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from .storage import Snap, parse_ts

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "new", "update", "upd", "event",
    "game", "simulator", "sim", "free", "beta", "alpha", "release", "part",
    "from", "but", "not", "are", "all", "out", "get", "big", "best",
}


@dataclass
class Cfg:
    min_ccu: int = 300           # нижче – шум, ігнор
    top_n: int = 20
    tolerance_h: float = 3.0     # наскільки «24 год тому» може бути неточним
    new_game_days: int = 30


@dataclass
class Stat:
    universe_id: int
    name: str
    playing: int
    playing_24h: int | None = None
    playing_6h: int | None = None
    g24: float | None = None     # ріст онлайну за 24 год, частка (0.3 = +30%)
    g6: float | None = None
    rel24: float | None = None   # g24 мінус медіана ринку
    rel6: float | None = None
    visits_24h: int | None = None       # нові візити за останню добу
    visits_prev_24h: int | None = None  # нові візити за попередню добу
    accel: float | None = None          # прискорення візитів
    age_days: float | None = None
    like_ratio: float | None = None
    root_place_id: str = ""
    sorts: str = ""
    tokens: set[str] = field(default_factory=set)

    @property
    def url(self) -> str:
        return f"https://www.roblox.com/games/{self.root_place_id}" if self.root_place_id else ""


def tokenize(name: str) -> set[str]:
    clean = re.sub(r"\[.*?\]|\(.*?\)", " ", name.lower())
    words = re.findall(r"[a-zа-яіїєґ]+", clean)
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def find_at(snaps: list[Snap], target: datetime, tolerance: timedelta) -> Snap | None:
    best, best_d = None, tolerance
    for s in snaps:
        d = abs(s.ts - target)
        if d <= best_d:
            best, best_d = s, d
    return best


def _growth(cur: int, old: int | None) -> float | None:
    if old is None:
        return None
    return (cur - old) / max(old, 1)


def compute(by_game: dict[int, list[Snap]], now: datetime | None = None, cfg: Cfg | None = None) -> list[Stat]:
    cfg = cfg or Cfg()
    tol = timedelta(hours=cfg.tolerance_h)
    stats: list[Stat] = []
    for uid, snaps in by_game.items():
        last = snaps[-1]
        ref_now = now or last.ts
        # гра має бути в останньому знімку, інакше дані застарілі
        if ref_now - last.ts > tol:
            continue
        s = Stat(universe_id=uid, name=last.name, playing=last.playing,
                 root_place_id=last.root_place_id, sorts=last.sorts, tokens=tokenize(last.name))
        d24 = find_at(snaps, last.ts - timedelta(hours=24), tol)
        d48 = find_at(snaps, last.ts - timedelta(hours=48), tol)
        h6 = find_at(snaps, last.ts - timedelta(hours=6), timedelta(hours=1.5))
        if d24:
            s.playing_24h = d24.playing
            s.g24 = _growth(last.playing, d24.playing)
            s.visits_24h = last.visits - d24.visits
            if d48:
                s.visits_prev_24h = d24.visits - d48.visits
                if s.visits_prev_24h > 0:
                    s.accel = s.visits_24h / s.visits_prev_24h - 1
        if h6:
            s.playing_6h = h6.playing
            s.g6 = _growth(last.playing, h6.playing)
        if last.created:
            try:
                s.age_days = (last.ts - parse_ts(last.created)).total_seconds() / 86400
            except ValueError:
                pass
        if last.up + last.down > 0:
            s.like_ratio = last.up / (last.up + last.down)
        stats.append(s)

    # нормування на ринок (тільки по іграх вище порогу, щоб дрібнота не крутила медіану)
    for attr, rel in (("g24", "rel24"), ("g6", "rel6")):
        vals = [getattr(s, attr) for s in stats if getattr(s, attr) is not None and s.playing >= cfg.min_ccu]
        m = median(vals) if vals else 0.0
        for s in stats:
            g = getattr(s, attr)
            if g is not None:
                setattr(s, rel, g - m)
    return stats


def market_median(stats: list[Stat], attr: str, cfg: Cfg) -> float | None:
    vals = [getattr(s, attr) for s in stats if getattr(s, attr) is not None and s.playing >= cfg.min_ccu]
    return median(vals) if vals else None


@dataclass
class Trend:
    token: str
    games: int
    playing: int
    playing_24h: int
    growth: float
    examples: list[str]


def keyword_trends(stats: list[Stat], cfg: Cfg, min_games: int = 2, min_playing: int = 1000) -> list[Trend]:
    """Тренди механік/тем по словах у назвах (brainrot, steal, tsunami...)."""
    agg: dict[str, list[Stat]] = {}
    for s in stats:
        if s.playing_24h is None:
            continue
        for t in s.tokens:
            agg.setdefault(t, []).append(s)
    out = []
    for t, games in agg.items():
        if len(games) < min_games:
            continue
        cur = sum(g.playing for g in games)
        old = sum(g.playing_24h or 0 for g in games)
        if cur < min_playing:
            continue
        top = sorted(games, key=lambda g: g.playing, reverse=True)[:3]
        out.append(Trend(t, len(games), cur, old, (cur - old) / max(old, 1), [g.name for g in top]))
    out.sort(key=lambda x: x.growth, reverse=True)
    return out


def rank(stats: list[Stat], cfg: Cfg) -> dict[str, list[Stat]]:
    big = [s for s in stats if s.playing >= cfg.min_ccu]
    rockets = sorted((s for s in big if s.rel24 is not None), key=lambda s: s.rel24, reverse=True)
    early = sorted((s for s in big if s.rel6 is not None), key=lambda s: s.rel6, reverse=True)
    accel = sorted((s for s in big if s.accel is not None), key=lambda s: s.accel, reverse=True)
    fresh = sorted(
        (s for s in big if s.age_days is not None and s.age_days <= cfg.new_game_days),
        key=lambda s: s.playing, reverse=True,
    )
    return {
        "rockets": rockets[: cfg.top_n],
        "early": early[: cfg.top_n],
        "accel": accel[: cfg.top_n],
        "fresh": fresh[: cfg.top_n],
    }


# ---------- звіт ----------

def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.0f}%"


def _pp(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.0f} п.п."


def _num(x: int | None) -> str:
    return "–" if x is None else f"{x:,}".replace(",", " ")


def _name(s: Stat) -> str:
    n = s.name.replace("|", "/")[:60]
    return f"[{n}]({s.url})" if s.url else n


def _age(s: Stat) -> str:
    return "–" if s.age_days is None else f"{s.age_days:.0f} дн"


def render_markdown(stats: list[Stat], cfg: Cfg, now: datetime) -> str:
    r = rank(stats, cfg)
    m24 = market_median(stats, "g24", cfg)
    m6 = market_median(stats, "g6", cfg)
    lines = [
        f"# Roblox тренди – {now.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC",
        "",
        f"Відстежується ігор: **{len(stats)}**, з онлайном ≥ {cfg.min_ccu}: "
        f"**{sum(1 for s in stats if s.playing >= cfg.min_ccu)}**. "
        f"Медіана ринку: 24 год {_pct(m24)}, 6 год {_pct(m6)}.",
        "",
        "## 🚀 Ракети – ріст онлайну за 24 год (відносно ринку)",
        "",
        "| # | Гра | Онлайн | 24 год тому | Ріст | Відрив від ринку | Вік | Лайки |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(r["rockets"], 1):
        lr = "–" if s.like_ratio is None else f"{s.like_ratio * 100:.0f}%"
        lines.append(f"| {i} | {_name(s)} | {_num(s.playing)} | {_num(s.playing_24h)} | {_pct(s.g24)} | {_pp(s.rel24)} | {_age(s)} | {lr} |")
    if not r["rockets"]:
        lines.append("| | _ще немає даних за 24 год – зачекай добу_ | | | | | | |")

    lines += ["", "## ⚡ Ранній сигнал – 6 год (відносно ринку)", "",
              "| # | Гра | Онлайн | 6 год тому | Ріст | Відрив від ринку |", "|---|---|---|---|---|---|"]
    for i, s in enumerate(r["early"], 1):
        lines.append(f"| {i} | {_name(s)} | {_num(s.playing)} | {_num(s.playing_6h)} | {_pct(s.g6)} | {_pp(s.rel6)} |")

    lines += ["", "## 📈 Прискорення візитів (доба до доби)", "",
              "| # | Гра | Візитів за добу | За попередню | Прискорення |", "|---|---|---|---|---|"]
    for i, s in enumerate(r["accel"], 1):
        lines.append(f"| {i} | {_name(s)} | {_num(s.visits_24h)} | {_num(s.visits_prev_24h)} | {_pct(s.accel)} |")

    lines += ["", f"## 🆕 Нові ігри (до {cfg.new_game_days} днів) з онлайном", "",
              "| # | Гра | Онлайн | Ріст 24 год | Вік |", "|---|---|---|---|---|"]
    for i, s in enumerate(r["fresh"], 1):
        lines.append(f"| {i} | {_name(s)} | {_num(s.playing)} | {_pct(s.g24)} | {_age(s)} |")

    trends = keyword_trends(stats, cfg)
    lines += ["", "## 🧠 Тренди тем / механік (слова в назвах)", "",
              "| Слово | Ігор | Онлайн | 24 год тому | Ріст | Приклади |", "|---|---|---|---|---|---|"]
    for t in trends[: cfg.top_n]:
        ex = "; ".join(e.replace("|", "/")[:40] for e in t.examples)
        lines.append(f"| **{t.token}** | {t.games} | {_num(t.playing)} | {_num(t.playing_24h)} | {_pct(t.growth)} | {ex} |")
    lines.append("")
    return "\n".join(lines)


def render_telegram(stats: list[Stat], cfg: Cfg, now: datetime, n: int = 10) -> str:
    r = rank(stats, cfg)
    out = [f"🎮 Roblox тренди {now.astimezone(timezone.utc):%d.%m %H:%M} UTC", "", "🚀 Ракети (24 год, відносно ринку):"]
    for i, s in enumerate(r["rockets"][:n], 1):
        out.append(f"{i}. {s.name[:45]} – {_num(s.playing)} онлайн, {_pct(s.g24)} ({_pp(s.rel24)})")
    if not r["rockets"]:
        out.append("ще немає даних за добу")
    trends = keyword_trends(stats, cfg)[:5]
    if trends:
        out += ["", "🧠 Теми, що ростуть:"]
        out += [f"• {t.token}: {t.games} ігор, {_pct(t.growth)}" for t in trends]
    fresh = r["fresh"][:5]
    if fresh:
        out += ["", "🆕 Нові з онлайном:"]
        out += [f"• {s.name[:45]} – {_num(s.playing)} ({_age(s)})" for s in fresh]
    return "\n".join(out)
