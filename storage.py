"""
storage.py — JSONBin.io persistence layer for StatSnitch.

Responsibilities:
- Load all user data from JSONBin on startup (or create a new bin).
- Keep an in-memory cache that is the single source of truth while the bot runs.
- Flush dirty data to JSONBin every 30 messages OR every 5 minutes, whichever comes first.
- Force-save on shutdown.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("statsnitch.storage")

# Words to ignore when counting per-word frequency
STOPWORDS = {
    "a", "an", "the", "and", "of", "to", "for", "in", "that",
    "is", "was", "it", "with", "as", "be", "this", "at", "by",
    "from", "have", "or", "i", "you", "he", "she", "we", "they",
    "my", "me", "do", "not", "are",
}


def _empty_user(user_id: str, username: str) -> dict:
    """Return a fresh user record."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "user_id": user_id,
        "username": username,
        "total_messages": 0,
        "words": {},
        "reactions_given": 0,
        "reactions_received": 0,
        "messages_edited": 0,
        "messages_deleted": 0,
        "hourly_activity": {},   # "0".."23" → count
        "daily_activity": {},    # "0".."6"  → count  (0=Mon)
        "longest_streak": 0,
        "current_streak": 0,
        "last_streak_date": None,
        "voice_minutes": 0,
        "commands_used": {},
        "mentions_received": 0,
        "mentions_given": 0,
        "attachments_sent": 0,
        "links_sent": 0,
        "caps_messages": 0,      # messages where >50 % chars are uppercase
        "question_marks": 0,
        "exclamation_marks": 0,
        "emoji_usage": {},
        "first_message_date": now,
        "last_message_date": now,
        "last_updated": now,
        # Spam / penalty fields
        "spam_strikes": 0,
        "stats_frozen_until": None,
        "last_spam_reason": None,
    }


class Storage:
    """Async wrapper around JSONBin with in-memory caching."""

    JSONBIN_BASE = "https://api.jsonbin.io/v3"
    FLUSH_INTERVAL = 300        # seconds between timed flushes
    FLUSH_EVERY_N  = 30         # dirty-message count that triggers a flush

    def __init__(self, api_key: str, bin_id: str | None = None):
        self._api_key = api_key
        self._bin_id  = bin_id

        # user_id (str) → user dict
        self._data: dict[str, Any] = {}
        self._dirty_count = 0
        self._last_flush  = time.monotonic()
        self._flush_lock  = asyncio.Lock()

        # voice join times:  user_id → datetime
        self._voice_joins: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Pull data from JSONBin (or create a new bin)."""
        async with aiohttp.ClientSession() as session:
            if not self._bin_id:
                await self._create_bin(session)
            else:
                await self._fetch_bin(session)

    async def _create_bin(self, session: aiohttp.ClientSession) -> None:
        url = f"{self.JSONBIN_BASE}/b"
        headers = {
            "Content-Type": "application/json",
            "X-Master-Key": self._api_key,
            "X-Bin-Name": "StatSnitch",
            "X-Bin-Private": "true",
        }
        async with session.post(url, headers=headers, json={"users": {}}) as resp:
            if resp.status in (200, 201):
                body = await resp.json()
                self._bin_id = body["metadata"]["id"]
                logger.info("Created new JSONBin bin: %s", self._bin_id)
                print(f"\n⚠️  New JSONBin bin created. Add this to your .env:\nJSONBIN_BIN_ID={self._bin_id}\n")
            else:
                text = await resp.text()
                raise RuntimeError(f"Failed to create JSONBin bin: {resp.status} {text}")

    async def _fetch_bin(self, session: aiohttp.ClientSession) -> None:
        url = f"{self.JSONBIN_BASE}/b/{self._bin_id}/latest"
        headers = {"X-Master-Key": self._api_key}
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                body = await resp.json()
                self._data = body.get("record", {}).get("users", {})
                logger.info("Loaded %d users from JSONBin.", len(self._data))
            else:
                text = await resp.text()
                raise RuntimeError(f"Failed to fetch JSONBin bin: {resp.status} {text}")

    async def save(self) -> None:
        """Flush all data to JSONBin."""
        async with self._flush_lock:
            async with aiohttp.ClientSession() as session:
                url = f"{self.JSONBIN_BASE}/b/{self._bin_id}"
                headers = {
                    "Content-Type": "application/json",
                    "X-Master-Key": self._api_key,
                }
                payload = {"users": self._data}
                async with session.put(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        self._dirty_count = 0
                        self._last_flush  = time.monotonic()
                        logger.debug("Flushed data to JSONBin.")
                    else:
                        text = await resp.text()
                        logger.error("JSONBin flush failed: %s %s", resp.status, text)

    async def maybe_flush(self) -> None:
        """Flush if dirty threshold or time interval is reached."""
        elapsed = time.monotonic() - self._last_flush
        if self._dirty_count >= self.FLUSH_EVERY_N or elapsed >= self.FLUSH_INTERVAL:
            await self.save()

    # ------------------------------------------------------------------
    # User helpers
    # ------------------------------------------------------------------

    def get_user(self, user_id: str, username: str) -> dict:
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = _empty_user(uid, username)
        else:
            # Keep username up-to-date
            self._data[uid]["username"] = username
        return self._data[uid]

    def all_users(self) -> list[dict]:
        return list(self._data.values())

    def is_frozen(self, user_id: str) -> bool:
        """Return True if this user's stats are currently frozen (spam penalty)."""
        uid = str(user_id)
        if uid not in self._data:
            return False
        fu_str = self._data[uid].get("stats_frozen_until")
        if not fu_str:
            return False
        try:
            fu = datetime.fromisoformat(fu_str)
            return datetime.now(timezone.utc) < fu
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Message tracking
    # ------------------------------------------------------------------

    def track_message(self, user_id: str, username: str, content: str,
                      has_attachment: bool, timestamp: datetime,
                      bot_prefix: str = "!") -> None:
        u = self.get_user(user_id, username)

        # Skip stat accumulation while frozen
        if self.is_frozen(user_id):
            return

        u["total_messages"] += 1
        u["last_message_date"] = timestamp.isoformat()

        # Hourly / daily buckets
        hour = str(timestamp.hour)
        day  = str(timestamp.weekday())
        u["hourly_activity"][hour] = u["hourly_activity"].get(hour, 0) + 1
        u["daily_activity"][day]   = u["daily_activity"].get(day, 0) + 1

        # Streak logic
        self._update_streak(u, timestamp)

        # Attachment
        if has_attachment:
            u["attachments_sent"] += 1

        # Content analysis — strip command prefix so "!chaos" isn't tracked as word "!chaos"
        self._analyse_content(u, content, bot_prefix=bot_prefix)

        u["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._dirty_count += 1

    def _update_streak(self, u: dict, ts: datetime) -> None:
        today = ts.date()
        last  = u.get("last_streak_date")
        if last:
            try:
                from datetime import date
                last_date = date.fromisoformat(last)
                delta = (today - last_date).days
                if delta == 1:
                    u["current_streak"] = u.get("current_streak", 0) + 1
                elif delta == 0:
                    pass  # Same day, no change
                else:
                    u["current_streak"] = 1
            except ValueError:
                u["current_streak"] = 1
        else:
            u["current_streak"] = 1

        if u["current_streak"] > u.get("longest_streak", 0):
            u["longest_streak"] = u["current_streak"]
        u["last_streak_date"] = today.isoformat()

    def _analyse_content(self, u: dict, content: str, bot_prefix: str = "!") -> None:
        import re

        # If the message is a bot command, strip the entire first token (e.g. "!mystats")
        # so command names don't pollute word stats.
        stripped = content.strip()
        if stripped.startswith(bot_prefix):
            # Remove the command token (everything up to first whitespace)
            stripped = re.sub(r'^\S+\s*', '', stripped)
            # If nothing is left (e.g. bare "!mystats"), skip content analysis entirely
            if not stripped:
                return

        # Links
        links = re.findall(r"https?://\S+|www\.\S+", stripped)
        u["links_sent"] += len(links)

        # Punctuation
        u["question_marks"]    += stripped.count("?")
        u["exclamation_marks"] += stripped.count("!")

        # Caps: count alphabetic chars that are uppercase vs lowercase
        alpha = [c for c in stripped if c.isalpha()]
        if alpha and sum(1 for c in alpha if c.isupper()) / len(alpha) > 0.5:
            u["caps_messages"] += 1

        # Mentions
        mentions = re.findall(r"<@!?(\d+)>", stripped)
        u["mentions_given"] += len(mentions)

        # Emojis (unicode + discord custom)
        discord_emojis = re.findall(r"<a?:\w+:\d+>", stripped)
        unicode_emojis = re.findall(
            r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]",
            stripped,
        )
        for e in discord_emojis + unicode_emojis:
            eu = u["emoji_usage"]
            eu[e] = eu.get(e, 0) + 1

        # Words (cleaned, no stopwords)
        raw_words = re.findall(r"[a-zA-Z']+", stripped.lower())
        for w in raw_words:
            if w not in STOPWORDS and len(w) > 1:
                u["words"][w] = u["words"].get(w, 0) + 1

    # ------------------------------------------------------------------
    # Reaction tracking
    # ------------------------------------------------------------------

    def track_reaction_add(self, reactor_id: str, reactor_name: str,
                           author_id: str, author_name: str, emoji: str) -> None:
        r = self.get_user(reactor_id, reactor_name)
        a = self.get_user(author_id,  author_name)
        r["reactions_given"]    += 1
        a["reactions_received"] += 1
        eu = r["emoji_usage"]
        eu[emoji] = eu.get(emoji, 0) + 1
        self._dirty_count += 1

    # ------------------------------------------------------------------
    # Edit / delete tracking
    # ------------------------------------------------------------------

    def track_edit(self, user_id: str, username: str) -> None:
        self.get_user(user_id, username)["messages_edited"] += 1
        self._dirty_count += 1

    def track_delete(self, user_id: str, username: str) -> None:
        self.get_user(user_id, username)["messages_deleted"] += 1
        self._dirty_count += 1

    # ------------------------------------------------------------------
    # Mention received
    # ------------------------------------------------------------------

    def track_mention_received(self, user_id: str, username: str) -> None:
        self.get_user(user_id, username)["mentions_received"] += 1
        self._dirty_count += 1

    # ------------------------------------------------------------------
    # Voice tracking
    # ------------------------------------------------------------------

    def voice_join(self, user_id: str, username: str) -> None:
        self.get_user(user_id, username)  # ensure record exists
        self._voice_joins[str(user_id)] = datetime.now(timezone.utc)

    def voice_leave(self, user_id: str, username: str) -> None:
        uid = str(user_id)
        if uid in self._voice_joins:
            joined  = self._voice_joins.pop(uid)
            minutes = (datetime.now(timezone.utc) - joined).total_seconds() / 60
            self.get_user(uid, username)["voice_minutes"] += minutes
            self._dirty_count += 1

    # ------------------------------------------------------------------
    # Bin ID accessor (for printing after auto-create)
    # ------------------------------------------------------------------

    @property
    def bin_id(self) -> str | None:
        return self._bin_id