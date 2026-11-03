"""Steam: кандидати з публічних списків + онлайн з офіційного API.

Офіційно й без ключа:
  * ISteamChartsService/GetMostPlayedGames – топ-100 за онлайном
  * store featuredcategories – новинки, топ продажів
  * store search (popularnew / topsellers) – популярні новинки
  * ISteamUserStats/GetNumberOfCurrentPlayers – онлайн конкретної гри
  * store appdetails – назва, дата релізу, ціна (кешуємо, ліміт ~200 запитів / 5 хв)
SteamSpy top100in2weeks – неофіційний, але відкритий.
SteamDB НЕ використовуємо: API немає, скрапінг заборонений.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import common as c
from .report import Section

MOST_PLAYED = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
CURRENT_PLAYERS = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
FEATURED = "https://store.steampowered.com/api/featuredcategories"
SEARCH = "https://store.steampowered.com/search/results/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
STEAMSPY = "https://steamspy.com/api.php"

FIELDS = ["appid", "name", "players", "sources"]
MAX_TRACKED = 800
KEEP_MIN = 20
NEW_DETAILS_PER_RUN = 40


def _add(found: dict[int, dict], appid, name: str, source: str) -> None:
    try:
        appid = int(appid)
    except (TypeError, ValueError):
        return
    e = found.setdefault(appid, {"name": "", "sources": set()})
    if name and not e["name"]:
        e["name"] = name
    e["sources"].add(source)


def discover() -> tuple[dict[int, dict], list[str]]:
    found: dict[int, dict] = {}
    errors: list[str] = []

    try:
        data = c.http_json(MOST_PLAYED)
        for r in data.get("response", {}).get("ranks", []):
            _add(found, r.get("appid"), "", "most_played")
    except Exception as e:
        errors.append(f"most_played: {e}")

    try:
        data = c.http_json(FEATURED, {"cc": "us", "l": "english"})
        for cat in ("new_releases", "top_sellers", "specials", "coming_soon"):
            for it in (data.get(cat) or {}).get("items", []):
                _add(found, it.get("id"), it.get("name", ""), cat)
    except Exception as e:
        errors.append(f"featured: {e}")

    for flt in ("popularnew", "topsellers"):
        try:
            data = c.http_json(SEARCH, {"filter": flt, "count": 100, "start": 0, "json": 1, "cc": "us", "l": "english"})
            for it in data.get("items", []):
                m = re.search(r"/apps/(\d+)/", it.get("logo", ""))
                if m:
                    _add(found, m.group(1), it.get("name", ""), flt)
        except Exception as e:
            errors.append(f"search {flt}: {e}")

    try:
        data = c.http_json(STEAMSPY, {"request": "top100in2weeks"})
        for appid, it in (data or {}).items():
            _add(found, appid, (it or {}).get("name", ""), "steamspy")
    except Exception as e:
        errors.append(f"steamspy: {e}")
    return found, errors


def players(appid: int) -> int | None:
    r = c.http_json(CURRENT_PLAYERS, {"appid": appid}, retries=2).get("response", {})
    return r.get("player_count") if r.get("result") == 1 else None


def load_meta(path: Path) -> dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def fetch_meta(appid: int) -> dict | None:
    data = c.http_json(APPDETAILS, {"appids": appid, "cc": "us", "l": "english"}, retries=2)
    d = (data or {}).get(str(appid), {})
    if not d.get("success"):
        return {"name": "", "type": "unknown"}
    d = d["data"]
    rd = d.get("release_date") or {}
    return {
        "name": d.get("name", ""),
        "type": d.get("type", ""),
        "release": rd.get("date", ""),
        "coming_soon": bool(rd.get("coming_soon")),
        "price": (d.get("price_overview") or {}).get("final_formatted", "Free" if d.get("is_free") else ""),
        "genres": ", ".join(g.get("description", "") for g in d.get("genres", [])[:3]),
    }


def read_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {int(x) for line in path.read_text(encoding="utf-8").splitlines()
            if (x := line.split("#", 1)[0].strip()).isdigit()}


def collect(data_dir: Path, meta_path: Path, watchlist: Path, now: datetime) -> str:
    found, errors = discover()
    if not found and errors:
        raise RuntimeError("усі списки кандидатів недоступні: " + "; ".join(errors))
    meta = load_meta(meta_path)
    history = c.load_csv(data_dir, now, 3, "appid")
    recent = {int(k) for k, rows in history.items() if (c.num(rows[-1], "players") or 0) >= KEEP_MIN}
    keep = set(found) | read_ids(watchlist)
    rest = sorted(recent - keep, key=lambda a: c.num(history[str(a)][-1], "players") or 0, reverse=True)
    tracked = keep | set(rest[: max(0, MAX_TRACKED - len(keep))])

    # мета для нових ігор (поступово, щоб не впертись у ліміт store API)
    new = [a for a in tracked if str(a) not in meta][:NEW_DETAILS_PER_RUN]
    for a in new:
        try:
            meta[str(a)] = fetch_meta(a)
        except Exception:
            break
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")

    rows, failed = [], 0
    for a in sorted(tracked):
        m = meta.get(str(a), {})
        if m.get("type") not in (None, "", "game") or m.get("coming_soon"):
            continue  # DLC, саундтреки, ще не вийшли
        try:
            p = players(a)
        except Exception:
            failed += 1
            continue
        if p is None:
            continue
        name = m.get("name") or found.get(a, {}).get("name", "")
        rows.append({"appid": a, "name": name, "players": p,
                     "sources": ",".join(sorted(found.get(a, {}).get("sources", ())))})
    c.append_csv(data_dir, now, rows, FIELDS)
    msg = f"Steam: кандидатів {len(found)}, записано {len(rows)}, помилок онлайну {failed}"
    if errors:
        msg += f"; джерела з помилками: {'; '.join(errors)}"
    return msg


# ---------- аналіз ----------

def _release_age(s: str, at: datetime) -> float | None:
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(s, fmt).replace(tzinfo=at.tzinfo)
            return (at - d).total_seconds() / 86400
        except (ValueError, TypeError):
            continue
    return None


@dataclass
class App:
    m: c.Momentum
    age_days: float | None
    price: str
    genres: str
    url: str


def analyze(data_dir: Path, meta_path: Path, now: datetime, min_players: int = 200, top: int = 20) -> Section:
    meta = load_meta(meta_path)
    apps: list[App] = []
    for key, rows in c.load_csv(data_dir, now, 4, "appid").items():
        m = c.momentum(key, rows, "players", now)
        if m is None:
            continue
        md = meta.get(key, {})
        apps.append(App(m, _release_age(md.get("release", ""), m.last["ts"]), md.get("price", ""),
                        md.get("genres", ""), f"https://store.steampowered.com/app/{key}"))
    med = c.apply_market([a.m for a in apps], min_players)
    big = [a for a in apps if a.m.cur >= min_players]
    rockets = sorted((a for a in big if a.m.rel24 is not None), key=lambda a: (a.m.rel24, a.m.streak), reverse=True)[:top]
    early = sorted((a for a in big if a.m.rel6 is not None), key=lambda a: a.m.rel6, reverse=True)[:top]
    fresh = sorted((a for a in big if a.age_days is not None and a.age_days <= 60), key=lambda a: a.m.cur, reverse=True)[:top]

    def age(a: App) -> str:
        return "–" if a.age_days is None else f"{a.age_days:.0f} дн"

    L = ["## 🟦 Steam", "",
         f"Ігор у трекінгу: **{len(apps)}**, з онлайном ≥ {min_players}: **{len(big)}**. "
         f"Ринок за 24 год: {c.pct(med.get('g24'))}.",
         "", "### 🚀 Ракети (24 год, відносно ринку)", "",
         "| # | Гра | Онлайн | Добу тому | Множник | Швидше ринку | Діб росту | Від релізу | Ціна | Жанр |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for i, a in enumerate(rockets, 1):
        L.append(f"| {i} | {c.link(a.m.name, a.url)} | {c.n(a.m.cur)} | {c.n(a.m.v24)} | {c.xs(a.m.x24)} | "
                 f"{c.pct(a.m.rel24)} | {c.streak_icon(a.m.streak) or '–'} | {age(a)} | {c.cell(a.price, 12)} | {c.cell(a.genres, 30)} |")
    if not rockets:
        L.append("| | _ще немає даних за добу_ | | | | | | | | |")
    L += ["", "### ⚡ Ранній сигнал (6 год)", "", "| # | Гра | Онлайн | 6 год тому | Швидше ринку |", "|---|---|---|---|---|"]
    for i, a in enumerate(early, 1):
        L.append(f"| {i} | {c.link(a.m.name, a.url)} | {c.n(a.m.cur)} | {c.n(a.m.v6)} | {c.pct(a.m.rel6)} |")
    L += ["", "### 🆕 Релізи ≤ 60 днів з онлайном", "", "| # | Гра | Онлайн | Множник 24 год | Від релізу | Ціна |", "|---|---|---|---|---|---|"]
    for i, a in enumerate(fresh, 1):
        L.append(f"| {i} | {c.link(a.m.name, a.url)} | {c.n(a.m.cur)} | {c.xs(a.m.x24)} | {age(a)} | {c.cell(a.price, 12)} |")

    tg = ["🟦 Steam – ракети:"]
    tg += [f"{i}. {a.m.name[:40]} – {c.n(a.m.cur)} ({c.xs(a.m.x24)}, {c.pct(a.m.rel24)} vs ринок) {c.streak_icon(a.m.streak)}"
           for i, a in enumerate(rockets[:6], 1)] or ["ще немає даних за добу"]
    rising = {}
    for a in rockets[:10]:
        for w in c.tokenize(a.m.name):
            rising.setdefault(w, f"Steam-ракета «{a.m.name[:40]}»")
    return Section("steam", "Steam", "\n".join(L), tg, rising)
