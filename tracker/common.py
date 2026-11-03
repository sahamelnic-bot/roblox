"""Спільне для всіх джерел: HTTP, CSV-знімки, метрики росту, форматування."""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

USER_AGENT = "trend-tracker/1.0 (+github actions)"

# ---------- HTTP ----------

_last_call: dict[str, float] = {}


def throttle(host: str, min_interval: float) -> None:
    """Не частіше одного запиту на host раз на min_interval секунд."""
    wait = _last_call.get(host, 0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


# пауза між запитами для «чутливих» хостів (сек)
HOST_INTERVAL = {
    "apis.roblox.com": 1.0,
    "games.roblox.com": 1.0,
    "store.steampowered.com": 1.5,
}


def http_json(url: str, params: dict | None = None, headers: dict | None = None,
              data: dict | None = None, retries: int = 5) -> Any:
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    h = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    host = urllib.parse.urlparse(url).hostname or ""
    delay = 5.0
    for attempt in range(retries):
        throttle(host, HOST_INTERVAL.get(host, 0.0))
        try:
            req = urllib.request.Request(full, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            wait = delay
        time.sleep(min(wait, 60))
        delay *= 2
    raise RuntimeError("unreachable")


def chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


# ---------- час / CSV ----------

def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_csv(directory: Path, ts: datetime, rows: list[dict], fields: list[str]) -> Path:
    """Дописує знімок у directory/YYYY-MM-DD.csv (поле ts додається автоматично)."""
    path = directory / f"{ts.astimezone(timezone.utc):%Y-%m-%d}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["ts", *fields]
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({**{k: r.get(k, "") for k in fields}, "ts": fmt_ts(ts)})
    return path


def load_csv(directory: Path, now: datetime, days: int, key: str) -> dict[str, list[dict]]:
    """Знімки за останні `days` днів: {key: [рядки за часом]}, ts – datetime."""
    out: dict[str, list[dict]] = {}
    for i in range(days + 1):
        path = directory / f"{(now - timedelta(days=i)).astimezone(timezone.utc):%Y-%m-%d}.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    r["ts"] = parse_ts(r["ts"])
                except (KeyError, ValueError):
                    continue
                if r.get(key):
                    out.setdefault(r[key], []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["ts"])
    return out


def num(r: dict | None, field: str) -> int | None:
    if r is None:
        return None
    try:
        return int(float(r.get(field) or 0))
    except ValueError:
        return 0


# ---------- метрики ----------

def find_at(rows: list[dict], target: datetime, tol: timedelta) -> dict | None:
    best, best_d = None, tol
    for r in rows:
        d = abs(r["ts"] - target)
        if d <= best_d:
            best, best_d = r, d
    return best


def growth(cur: int | None, old: int | None) -> float | None:
    if cur is None or old is None:
        return None
    return (cur - old) / max(old, 1)


@dataclass
class Momentum:
    key: str
    name: str
    cur: int
    v6: int | None = None
    v24: int | None = None
    v48: int | None = None
    v72: int | None = None
    g6: float | None = None
    g24: float | None = None
    g_prev24: float | None = None   # ріст за попередню добу (48→24 год)
    accel: float | None = None      # g24 - g_prev24: розганяється чи видихається
    streak: int = 0                 # скільки діб поспіль ріст (0..3)
    rel6: float | None = None       # у скільки разів швидше за ринок, -1..∞
    rel24: float | None = None
    last: dict | None = None

    @property
    def x24(self) -> float | None:
        return None if self.v24 is None else self.cur / max(self.v24, 1)


def momentum(key: str, rows: list[dict], field: str, now: datetime,
             tol_h: float = 3.0, name_field: str = "name") -> Momentum | None:
    """Порівнює з тим самим часом доби 1/2/3 дні тому (прибирає добовий цикл)."""
    tol = timedelta(hours=tol_h)
    last = rows[-1]
    if now - last["ts"] > tol:
        return None  # гра зникла з останнього знімка
    t = last["ts"]
    m = Momentum(key=key, name=last.get(name_field, ""), cur=num(last, field) or 0, last=last)
    m.v6 = num(find_at(rows, t - timedelta(hours=6), timedelta(hours=1.5)), field)
    m.v24 = num(find_at(rows, t - timedelta(hours=24), tol), field)
    m.v48 = num(find_at(rows, t - timedelta(hours=48), tol), field)
    m.v72 = num(find_at(rows, t - timedelta(hours=72), tol), field)
    m.g6 = growth(m.cur, m.v6)
    m.g24 = growth(m.cur, m.v24)
    m.g_prev24 = growth(m.v24, m.v48)
    if m.g24 is not None and m.g_prev24 is not None:
        m.accel = m.g24 - m.g_prev24
    # стабільність: рахуємо доби поспіль з ростом, від сьогодні назад
    chain = [m.cur, m.v24, m.v48, m.v72]
    for newer, older in zip(chain, chain[1:]):
        if newer is None or older is None or newer <= older:
            break
        m.streak += 1
    return m


def apply_market(items: list[Momentum], min_value: int) -> dict[str, float | None]:
    """rel = (1+g)/(1+медіана ринку) - 1. Множинне порівняння: x10 з малого
    й x10 з великого важать однаково, а +5% великої гри – майже нуль."""
    med: dict[str, float | None] = {}
    for g_attr, rel_attr in (("g24", "rel24"), ("g6", "rel6")):
        vals = [getattr(m, g_attr) for m in items if getattr(m, g_attr) is not None and m.cur >= min_value]
        mm = median(vals) if vals else 0.0
        med[g_attr] = mm if vals else None
        for m in items:
            g = getattr(m, g_attr)
            if g is not None:
                setattr(m, rel_attr, (1 + g) / (1 + mm) - 1)
    return med


# ---------- текст ----------

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "new", "update", "upd", "event",
    "game", "games", "simulator", "sim", "free", "beta", "alpha", "release", "part",
    "from", "but", "not", "are", "all", "out", "get", "big", "best", "this", "that",
    "roblox", "played", "playing", "play", "most", "ever", "how", "what", "why",
    "who", "can", "will", "his", "her", "they", "have", "has", "was", "into",
    "just", "only", "every", "one", "two", "day", "days", "hours", "minutes",
    "first", "last", "world", "vs", "edition", "demo", "official", "video",
    "shorts", "short", "funny", "moments", "challenge", "secret", "real",
    "everyone", "everything", "nobody", "someone", "something", "anything", "everybody",
    "there", "their", "them", "then", "than", "when", "where", "which", "while",
    "about", "after", "before", "again", "never", "still", "even", "very", "really",
    "here", "now", "too", "off", "over", "under", "back", "way", "long", "time",
    "made", "make", "making", "gets", "got", "went", "going", "gone", "did", "does",
    "doing", "dont", "cant", "wont", "isnt", "its", "our", "ours", "she", "him",
    "these", "those", "some", "any", "much", "many", "more", "less", "like",
    "regular", "home", "need", "want", "know", "think", "look", "see", "said",
    "say", "tried", "try", "trying", "finally", "actually", "literally",
}


def tokenize(text: str) -> set[str]:
    clean = re.sub(r"\[.*?\]|\(.*?\)|#\w+", " ", text.lower())
    clean = re.sub(r"[a-z]+['’](t|s|re|ve|ll|d|m)\b", " ", clean)  # isn't, it's, we're...
    words = re.findall(r"[a-zа-яіїєґ]+", clean)
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.0f}%"


def xs(x: float | None) -> str:
    return "–" if x is None else f"x{x:.1f}"


def n(x: int | None) -> str:
    return "–" if x is None else f"{x:,}".replace(",", " ")


def cell(s: str, limit: int = 60) -> str:
    return s.replace("|", "/").replace("\n", " ")[:limit]


def link(name: str, url: str, limit: int = 60) -> str:
    return f"[{cell(name, limit)}]({url})" if url else cell(name, limit)


def streak_icon(k: int) -> str:
    return {0: "", 1: "↑", 2: "↑↑", 3: "↑↑↑"}.get(k, "")
