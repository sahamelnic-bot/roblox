"""YouTube: стежимо за списком каналів (дешево по квоті), а не шукаємо.

Квота: безкоштовно 10 000 одиниць/день. Пошук = 100 одиниць, тому не шукаємо.
  * channels.list (forHandle)  – 1 од., один раз на канал (кешується)
  * playlistItems.list         – 1 од. на канал за запуск
  * videos.list                – 1 од. на 50 відео
50 каналів щогодини ≈ 60 од. × 24 = ~1 500 од./день.

Що рахуємо:
  * 🔥 Відео-хіти: свіже відео (≤ 3 дні) вже набрало X% від звичайного
    результату цього каналу (медіана переглядів його відео віком 3–14 днів).
    200% = за добу вже вдвічі більше, ніж канал зазвичай має за тиждень.
  * 🧠 Теми: слова в назвах, які за останні 3 дні з'являються частіше,
    ніж за попередні 11 (з вагою переглядів), у ≥ 2 каналів.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from . import common as c
from .report import Section

API = "https://www.googleapis.com/youtube/v3"
FIELDS = ["video_id", "channel_id", "channel", "title", "published", "views"]
KEEP_DAYS = 14


def read_handles(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        h = line.split("#", 1)[0].strip()
        if h:
            out.append(h if h.startswith("@") or h.startswith("UC") else f"@{h}")
    return out


def resolve(handles: list[str], cache_path: Path, key: str) -> tuple[dict[str, dict], list[str]]:
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    missing = []
    for h in handles:
        if h in cache:
            continue
        params = {"part": "id,snippet", "key": key, **({"id": h} if h.startswith("UC") else {"forHandle": h})}
        items = c.http_json(f"{API}/channels", params).get("items", [])
        if items:
            cache[h] = {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}
        else:
            cache[h] = None   # не знайдено – не перевіряємо щогодини
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    missing = [h for h in handles if cache.get(h) is None]
    return {h: v for h, v in cache.items() if v and h in handles}, missing


def collect(data_dir: Path, channels_file: Path, cache_path: Path, key: str, now: datetime) -> str:
    chans, missing = resolve(read_handles(channels_file), cache_path, key)
    video_ids: list[str] = []
    for ch in chans.values():
        uploads = "UU" + ch["id"][2:]
        try:
            items = c.http_json(f"{API}/playlistItems", {"part": "contentDetails", "playlistId": uploads,
                                                         "maxResults": 15, "key": key}).get("items", [])
        except Exception:
            continue
        for it in items:
            cd = it.get("contentDetails", {})
            pub = cd.get("videoPublishedAt")
            if pub and now - c.parse_ts(pub) <= timedelta(days=KEEP_DAYS):
                video_ids.append(cd["videoId"])
    rows = []
    for chunk in c.chunks(video_ids, 50):
        for v in c.http_json(f"{API}/videos", {"part": "snippet,statistics", "id": ",".join(chunk),
                                               "key": key}).get("items", []):
            sn, st = v.get("snippet", {}), v.get("statistics", {})
            rows.append({"video_id": v["id"], "channel_id": sn.get("channelId", ""),
                         "channel": sn.get("channelTitle", ""), "title": sn.get("title", ""),
                         "published": sn.get("publishedAt", ""), "views": st.get("viewCount", 0)})
    c.append_csv(data_dir, now, rows, FIELDS)
    msg = f"YouTube: каналів {len(chans)}, відео {len(rows)}"
    if missing:
        msg += f"; не знайдено каналів: {', '.join(missing)}"
    return msg


@dataclass
class Video:
    vid: str
    channel_id: str
    channel: str
    title: str
    age_h: float
    views: int
    views_24h: int | None


def _latest(data_dir: Path, now: datetime) -> list[Video]:
    out = []
    for vid, rows in c.load_csv(data_dir, now, KEEP_DAYS, "video_id").items():
        last = rows[-1]
        if now - last["ts"] > timedelta(hours=26):
            continue
        try:
            age = (last["ts"] - c.parse_ts(last["published"])).total_seconds() / 3600
        except (ValueError, KeyError):
            continue
        prev = c.find_at(rows, last["ts"] - timedelta(hours=24), timedelta(hours=3))
        views = c.num(last, "views") or 0
        out.append(Video(vid, last.get("channel_id", ""), last.get("channel", ""), last.get("title", ""),
                         max(age, 0.1), views, views - c.num(prev, "views") if prev else None))
    return out


def analyze(data_dir: Path, now: datetime, top: int = 15) -> Section:
    vids = _latest(data_dir, now)
    by_ch: dict[str, list[Video]] = {}
    for v in vids:
        by_ch.setdefault(v.channel_id, []).append(v)
    baseline = {ch: median([v.views for v in vs if 72 <= v.age_h <= KEEP_DAYS * 24] or [0]) for ch, vs in by_ch.items()}

    hits = []
    for v in vids:
        b = baseline.get(v.channel_id) or 0
        if v.age_h <= 72 and b > 0:
            hits.append((v.views / b, v))
    hits.sort(key=lambda x: x[0], reverse=True)

    # теми: частота слів (з вагою переглядів) за 3 дні vs попередні 11
    def weights(vs: list[Video]) -> tuple[dict[str, float], dict[str, set]]:
        w: dict[str, float] = {}
        chans: dict[str, set] = {}
        total = sum(v.views for v in vs) or 1
        for v in vs:
            for t in c.tokenize(v.title):
                w[t] = w.get(t, 0) + v.views / total
                chans.setdefault(t, set()).add(v.channel_id)
        return w, chans

    recent = [v for v in vids if v.age_h <= 72]
    older = [v for v in vids if v.age_h > 72]
    wr, cr = weights(recent)
    wo, _ = weights(older)
    topics = sorted(((t, wr[t] / (wo.get(t, 0) + 0.02), len(cr[t])) for t in wr if len(cr[t]) >= 2),
                    key=lambda x: x[1], reverse=True)

    L = ["## 🟥▶️ YouTube (стежимо за каналами)", "",
         f"Каналів з даними: **{len(by_ch)}**, відео за {KEEP_DAYS} днів: **{len(vids)}**.", "",
         "### 🔥 Відео-хіти (≤ 3 дні, від звичайного результату каналу)", "",
         "| # | Відео | Канал | Перегляди | За добу | Вік | Від норми каналу |", "|---|---|---|---|---|---|---|"]
    for i, (ratio, v) in enumerate(hits[:top], 1):
        L.append(f"| {i} | {c.link(v.title, f'https://youtu.be/{v.vid}', 70)} | {c.cell(v.channel, 25)} | {c.n(v.views)} | "
                 f"{c.n(v.views_24h)} | {v.age_h:.0f} год | {ratio * 100:.0f}% |")
    if not hits:
        L.append("| | _потрібні відео старші за 3 дні для бази_ | | | | | |")
    L += ["", "### 🧠 Теми в назвах, що ростуть (≥ 2 каналів)", "", "| Слово | Сила | Каналів |", "|---|---|---|"]
    for t, score, nch in topics[:top]:
        L.append(f"| **{t}** | {score:.1f} | {nch} |")

    tg = ["▶️ YouTube – відео-хіти:"]
    tg += [f"{i}. {v.channel[:20]}: {v.title[:50]} – {ratio * 100:.0f}% від норми"
           for i, (ratio, v) in enumerate(hits[:5], 1)] or ["ще мало даних"]
    if topics:
        tg.append("Теми: " + ", ".join(t for t, _, _ in topics[:8]))
    rising = {t: f"YouTube-тема «{t}» ({nch} каналів)" for t, score, nch in topics[:30] if score >= 1.5}
    for ratio, v in hits[:10]:
        if ratio >= 1:
            for w in c.tokenize(v.title):
                rising.setdefault(w, f"YouTube-хіт «{v.title[:40]}» ({v.channel})")
    return Section("youtube", "YouTube", "\n".join(L), tg, rising)
