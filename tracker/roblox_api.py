"""Мінімальний клієнт публічних (без авторизації) ендпоінтів Roblox.

Тільки stdlib, щоб нічого не ставити в GitHub Actions.
Структура відповідей explore-api недокументована й змінюється, тому
ігри з неї витягуються рекурсивно: будь-який dict з ключем universeId.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterable, Iterator

EXPLORE_SORTS = "https://apis.roblox.com/explore-api/v1/get-sorts"
EXPLORE_SORT_CONTENT = "https://apis.roblox.com/explore-api/v1/get-sort-content"
GAMES = "https://games.roblox.com/v1/games"
VOTES = "https://games.roblox.com/v1/games/votes"

USER_AGENT = "trend-tracker/1.0 (+github actions)"
BATCH = 50


def _get(url: str, params: dict[str, Any], retries: int = 4) -> Any:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    delay = 2.0
    for attempt in range(retries):
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429 / 5xx – чекаємо й пробуємо ще; 4xx – одразу падаємо
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def extract_games(payload: Any) -> Iterator[dict]:
    """Рекурсивно знаходить усі об'єкти ігор (мають universeId)."""
    if isinstance(payload, dict):
        if "universeId" in payload:
            yield payload
        for v in payload.values():
            if isinstance(v, (dict, list)):
                yield from extract_games(v)
    elif isinstance(payload, list):
        for item in payload:
            yield from extract_games(item)


def discover(max_sort_pages: int = 5, max_content_pages: int = 3, device: str = "computer") -> dict[int, dict]:
    """Ігри з усіх підбірок головної (Trending, Up-and-coming, Top playing тощо).

    Повертає {universe_id: {"name": ..., "sorts": set(...)}}.
    """
    session = str(uuid.uuid4())
    base = {"sessionId": session, "device": device, "country": "all"}
    found: dict[int, dict] = {}

    def add(games: Iterable[dict], sort_id: str) -> None:
        for g in games:
            try:
                uid = int(g["universeId"])
            except (TypeError, ValueError, KeyError):
                continue
            entry = found.setdefault(uid, {"name": g.get("name", ""), "sorts": set()})
            entry["sorts"].add(sort_id)

    token = None
    for _ in range(max_sort_pages):
        params = dict(base)
        if token:
            params["sortsPageToken"] = token
        data = _get(EXPLORE_SORTS, params)
        for sort in data.get("sorts", []) if isinstance(data, dict) else []:
            sort_id = str(sort.get("sortId") or sort.get("topicId") or "unknown")
            add(extract_games(sort), sort_id)
            page_token = sort.get("nextPageToken")
            for _ in range(max_content_pages):
                if not page_token:
                    break
                try:
                    content = _get(EXPLORE_SORT_CONTENT, {**base, "sortId": sort_id, "pageToken": page_token})
                except Exception:
                    break
                add(extract_games(content), sort_id)
                page_token = content.get("nextPageToken") if isinstance(content, dict) else None
        token = data.get("nextSortsPageToken") if isinstance(data, dict) else None
        if not token:
            break
    return found


def _chunks(items: list[int], n: int) -> Iterator[list[int]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def game_details(universe_ids: Iterable[int]) -> dict[int, dict]:
    """playing, visits, created, updated, name, genre + лайки/дизлайки."""
    ids = sorted(set(universe_ids))
    out: dict[int, dict] = {}
    for chunk in _chunks(ids, BATCH):
        q = {"universeIds": ",".join(map(str, chunk))}
        for g in _get(GAMES, q).get("data", []):
            out[int(g["id"])] = {
                "name": g.get("name", ""),
                "playing": int(g.get("playing") or 0),
                "visits": int(g.get("visits") or 0),
                "favorites": int(g.get("favoritedCount") or 0),
                "created": g.get("created", ""),
                "updated": g.get("updated", ""),
                "genre": g.get("genre", ""),
                "root_place_id": g.get("rootPlaceId", ""),
                "up": 0,
                "down": 0,
            }
        try:
            for v in _get(VOTES, q).get("data", []):
                uid = int(v["id"])
                if uid in out:
                    out[uid]["up"] = int(v.get("upVotes") or 0)
                    out[uid]["down"] = int(v.get("downVotes") or 0)
        except Exception:
            pass  # лайки – не критично
    return out
