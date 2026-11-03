"""CLI: python -m tracker.main run [--notify]

collect – зняти знімок онлайну (головна Roblox + watchlist + все, що бачили за 3 дні)
report  – порахувати динаміку й записати reports/latest.md
run     – collect + report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import analyze, roblox_api, storage

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "snapshots"
WATCHLIST = ROOT / "data" / "watchlist.txt"
REPORTS = ROOT / "reports"

MAX_TRACKED = 3000
KEEP_MIN_PLAYING = 30  # ігри, що випали з головної, трекаємо далі, якщо онлайн ≥ цього


def read_watchlist() -> set[int]:
    if not WATCHLIST.exists():
        return set()
    ids = set()
    for line in WATCHLIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.isdigit():
            ids.add(int(line))
    return ids


def collect(now: datetime) -> int:
    discovered = roblox_api.discover()
    print(f"знайдено на головній: {len(discovered)}")

    history = storage.load(DATA, now, days=3)
    recent = {uid for uid, snaps in history.items() if snaps[-1].playing >= KEEP_MIN_PLAYING}
    tracked = set(discovered) | read_watchlist() | recent
    if len(tracked) > MAX_TRACKED:
        # пріоритет: головна > watchlist > найбільші з історії
        keep = set(discovered) | read_watchlist()
        rest = sorted(recent - keep, key=lambda u: history[u][-1].playing, reverse=True)
        tracked = keep | set(rest[: max(0, MAX_TRACKED - len(keep))])
    print(f"трекаємо: {len(tracked)}")

    details = roblox_api.game_details(tracked)
    rows = []
    for uid, d in details.items():
        sorts = ",".join(sorted(discovered.get(uid, {}).get("sorts", [])))
        rows.append({"universe_id": uid, "sorts": sorts, **d})
    path = storage.append(DATA, now, rows)
    print(f"записано {len(rows)} рядків у {path.relative_to(ROOT)}")
    return len(rows)


def report(now: datetime, cfg: analyze.Cfg) -> list[analyze.Stat]:
    stats = analyze.compute(storage.load(DATA, now, days=3), now=now, cfg=cfg)
    REPORTS.mkdir(exist_ok=True)
    md = analyze.render_markdown(stats, cfg, now)
    (REPORTS / "latest.md").write_text(md, encoding="utf-8")
    print(f"звіт: reports/latest.md ({len(stats)} ігор)")
    return stats


def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані – пропускаю відправку")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
        resp = json.loads(r.read())
        if not resp.get("ok"):
            raise RuntimeError(f"telegram error: {resp}")
    print("відправлено в Telegram")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["collect", "report", "run"])
    p.add_argument("--notify", action="store_true", help="надіслати дайджест у Telegram")
    p.add_argument("--min-ccu", type=int, default=int(os.environ.get("MIN_CCU", 300)))
    p.add_argument("--top", type=int, default=20)
    a = p.parse_args(argv)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cfg = analyze.Cfg(min_ccu=a.min_ccu, top_n=a.top)
    if a.cmd in ("collect", "run"):
        collect(now)
    if a.cmd in ("report", "run"):
        stats = report(now, cfg)
        if a.notify:
            send_telegram(analyze.render_telegram(stats, cfg, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
