"""CLI: python -m tracker.main run [--notify] [--sources roblox,steam,twitch,youtube]

collect – знімок по всіх джерелах
report  – аналіз → reports/latest.md (+ Telegram з --notify)
run     – collect + report

Кожне джерело незалежне: якщо одне впало, інші працюють, а помилка
потрапляє у звіт.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import roblox, steam, twitch, youtube
from .report import Section

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("TRACKER_DATA", ROOT / "data"))
CONFIG = ROOT / "config"
REPORTS = Path(os.environ.get("TRACKER_REPORTS", ROOT / "reports"))

RETENTION_DAYS = {"roblox": 6, "steam": 6, "twitch": 6, "youtube": 15}
ALL = ["roblox", "steam", "twitch", "youtube"]


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def enabled(sources: list[str]) -> dict[str, str | None]:
    """{джерело: причина пропуску або None}."""
    out = {}
    for s in sources:
        if s == "twitch" and not (env("TWITCH_CLIENT_ID") and env("TWITCH_CLIENT_SECRET")):
            out[s] = "немає TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET"
        elif s == "youtube" and not env("YOUTUBE_API_KEY"):
            out[s] = "немає YOUTUBE_API_KEY"
        else:
            out[s] = None
    return out


def collect(now: datetime, sources: list[str]) -> dict[str, str]:
    errors = {}
    for s, skip in enabled(sources).items():
        if skip:
            print(f"{s}: пропущено ({skip})")
            continue
        try:
            if s == "roblox":
                msg = roblox.collect(DATA / "roblox", CONFIG / "roblox_watchlist.txt", now)
            elif s == "steam":
                msg = steam.collect(DATA / "steam", DATA / "steam_apps.json", CONFIG / "steam_watchlist.txt", now)
            elif s == "twitch":
                msg = twitch.collect(DATA / "twitch", env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET"), now)
            else:
                msg = youtube.collect(DATA / "youtube", CONFIG / "youtube_channels.txt",
                                      DATA / "youtube_channels.json", env("YOUTUBE_API_KEY"), now)
            print(msg)
        except Exception as e:
            traceback.print_exc()
            errors[s] = f"{type(e).__name__}: {e}"
    return errors


def prune(now: datetime) -> None:
    for s, days in RETENTION_DAYS.items():
        d = DATA / s
        if not d.exists():
            continue
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        for f in d.glob("*.csv"):
            if f.stem < cutoff:
                f.unlink()


def analyze(now: datetime, sources: list[str], min_ccu: int, top: int) -> list[Section]:
    out = []
    for s, skip in enabled(sources).items():
        if skip:
            continue
        try:
            if s == "roblox":
                out.append(roblox.analyze(DATA / "roblox", now, min_ccu, top))
            elif s == "steam":
                out.append(steam.analyze(DATA / "steam", DATA / "steam_apps.json", now, top=top))
            elif s == "twitch":
                out.append(twitch.analyze(DATA / "twitch", now))
            else:
                out.append(youtube.analyze(DATA / "youtube", now))
        except Exception as e:
            traceback.print_exc()
            out.append(Section(s, s, error=f"{type(e).__name__}: {e}"))
    return out


def crossovers(sections: list[Section]) -> list[tuple[str, list[str]]]:
    """Слова, що ростуть одночасно в ≥ 2 різних джерелах – найсильніший сигнал."""
    hits: dict[str, dict[str, str]] = {}
    for s in sections:
        for word, why in s.rising.items():
            hits.setdefault(word, {})[s.source] = why
    res = [(w, list(by.values())) for w, by in hits.items() if len(by) >= 2]
    res.sort(key=lambda x: len(x[1]), reverse=True)
    return res


def render(now: datetime, sections: list[Section], errors: dict[str, str], skipped: dict[str, str | None]) -> tuple[str, str]:
    cross = crossovers(sections)
    md = [f"# Тренди – {now:%Y-%m-%d %H:%M} UTC", ""]
    md += ["## 🔥 Збіги між джерелами", ""]
    if cross:
        md += [f"- **{w}** – " + "; ".join(why) for w, why in cross[:15]]
    else:
        md.append("_поки немає – слово має рости одночасно мінімум у двох джерелах_")
    problems = {**errors, **{s.source: s.error for s in sections if s.error}}
    off = {s: why for s, why in skipped.items() if why}
    if problems or off:
        md += ["", "## ⚠️ Стан джерел", ""]
        md += [f"- ❌ **{s}**: {e}" for s, e in problems.items()]
        md += [f"- ⏸ **{s}**: {why}" for s, why in off.items()]
    for s in sections:
        if s.md:
            md += ["", s.md]
    md.append("")

    tg = [f"📊 Тренди {now:%d.%m %H:%M} UTC", ""]
    if cross:
        tg.append("🔥 Збіги між джерелами:")
        tg += [f"• {w}: " + " + ".join(why) for w, why in cross[:5]]
        tg.append("")
    for s in sections:
        if s.tg:
            tg += s.tg + [""]
    if problems:
        tg.append("⚠️ Впали: " + ", ".join(problems))
    return "\n".join(md), "\n".join(tg)


def send_telegram(text: str) -> None:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram: немає TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID – пропускаю")
        return
    for part in [text[i : i + 3900] for i in range(0, len(text), 3900)]:
        data = urllib.parse.urlencode({"chat_id": chat, "text": part, "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
            if not json.loads(r.read()).get("ok"):
                raise RuntimeError("telegram error")
    print("Telegram: відправлено")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["collect", "report", "run"])
    p.add_argument("--notify", action="store_true")
    p.add_argument("--sources", default=",".join(ALL))
    p.add_argument("--min-ccu", type=int, default=int(os.environ.get("MIN_CCU") or 300))
    p.add_argument("--top", type=int, default=20)
    a = p.parse_args(argv)
    sources = [s for s in a.sources.split(",") if s in ALL]

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    errors: dict[str, str] = {}
    if a.cmd in ("collect", "run"):
        errors = collect(now, sources)
        prune(now)
    if a.cmd in ("report", "run"):
        sections = analyze(now, sources, a.min_ccu, a.top)
        md, tg = render(now, sections, errors, enabled(sources))
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / "latest.md").write_text(md, encoding="utf-8")
        print(f"звіт: {REPORTS / 'latest.md'}")
        if a.notify:
            send_telegram(tg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
