"""Demo limits Otari cannot know about: per-IP rate limit, daily web-check cap, spend log.

Money is capped by Otari itself: each pool (public, judges) has its own key with a daily
spend ceiling, and the organization has a total ceiling; see app.py. Everything here lives
in one SQLite file (on a server it survives restarts; on Vercel each instance has its own). Days are UTC.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

RATE_PER_IP_HOUR = int(os.getenv("SHOPGUARD_RATE_PER_IP_HOUR", "20"))
DAILY_SEARCH_CAP = int(os.getenv("SHOPGUARD_DAILY_SEARCH_CAP", "25"))
JUDGE_RATE_PER_HOUR = int(os.getenv("SHOPGUARD_JUDGE_RATE_PER_HOUR", "60"))


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Budget:
    def __init__(self, path: str = "shopguard.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hits (who TEXT, ts REAL);
                CREATE INDEX IF NOT EXISTS hits_who_ts ON hits(who, ts);
                CREATE TABLE IF NOT EXISTS spend (day TEXT, pool TEXT, usd REAL DEFAULT 0, searches INTEGER DEFAULT 0,
                                                  PRIMARY KEY (day, pool));
                CREATE TABLE IF NOT EXISTS search_cache (query TEXT PRIMARY KEY, body TEXT, ts REAL);
                """
            )
            self.db.commit()

    # --- rate limit -------------------------------------------------------
    def allow_request(self, who: str, judge: bool = False) -> bool:
        limit = JUDGE_RATE_PER_HOUR if judge else RATE_PER_IP_HOUR
        now = time.time()
        with self.lock:
            self.db.execute("DELETE FROM hits WHERE ts < ?", (now - 3600,))
            (n,) = self.db.execute("SELECT COUNT(*) FROM hits WHERE who = ? AND ts >= ?", (who, now - 3600)).fetchone()
            if n >= limit:
                self.db.commit()
                return False
            self.db.execute("INSERT INTO hits VALUES (?, ?)", (who, now))
            self.db.commit()
            return True

    # --- money ------------------------------------------------------------
    def _row(self, pool: str) -> tuple[float, int]:
        row = self.db.execute("SELECT usd, searches FROM spend WHERE day = ? AND pool = ?", (today(), pool)).fetchone()
        return (row[0], row[1]) if row else (0.0, 0)

    def can_search(self) -> bool:
        with self.lock:
            total = sum(r[0] for r in self.db.execute("SELECT searches FROM spend WHERE day = ?", (today(),)))
        return total < DAILY_SEARCH_CAP

    def record(self, usd: float, searches: int = 0, judge: bool = False) -> None:
        pool = "judge" if judge else "public"
        with self.lock:
            self.db.execute(
                "INSERT INTO spend(day, pool, usd, searches) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(day, pool) DO UPDATE SET usd = usd + excluded.usd, searches = searches + excluded.searches",
                (today(), pool, usd, searches),
            )
            self.db.commit()

    def status(self) -> dict:
        with self.lock:
            pub, pub_s = self._row("public")
            jud, jud_s = self._row("judge")
        return {
            "day_utc": today(),
            "public_usd": round(pub, 4), "judge_usd": round(jud, 4),
            "searches": pub_s + jud_s, "search_cap": DAILY_SEARCH_CAP,
        }

    # --- search cache (used by TavilySearcher) ----------------------------
    def get(self, query: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT body FROM search_cache WHERE query = ?", (query.lower(),)).fetchone()
        return row[0] if row else None

    def put(self, query: str, body: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO search_cache VALUES (?, ?, ?)", (query.lower(), body, time.time()))
            self.db.commit()
