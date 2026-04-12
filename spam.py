"""
spam.py — Spam detection, queued penalty enforcement, and top.gg vote pardons.

KEY IMPROVEMENTS over v1:
──────────────────────────────────────────────────────────────────────────
1. QUEUED PENALTY DELIVERY
   When a user triggers spam multiple times rapidly, detections are queued
   per-user. A single async worker per user drains the queue one-by-one with
   a PENALTY_COOLDOWN gap between actions, so the bot never fires five
   timeouts simultaneously.  While a penalty is already "in flight" for a
   user, additional spam detections still delete the message immediately,
   but the announcement / timeout is deferred via the queue.

2. EXPONENTIAL BACKOFF TIMEOUTS
   Each strike multiplies freeze and Discord-timeout duration:

     Strike 1 → warn only          | freeze  10 min  | Discord timeout: none
     Strike 2 → warn + delete      | freeze   1 hr   | Discord timeout: none
     Strike 3 → warn + delete      | freeze   6 hrs  | Discord timeout:  5 min
     Strike 4 → warn + delete      | freeze  24 hrs  | Discord timeout: 30 min
     Strike 5 → warn + delete      | freeze   3 days | Discord timeout:  2 hrs
     Strike 6+ → warn + delete     | freeze   7 days | Discord timeout:  6 hrs (cap)

   Strikes decay by 1 after STRIKE_DECAY_DAYS clean days.

3. TOP.GG VOTE PARDONS
   Voting for the bot on top.gg halves the user's remaining freeze (but
   never below VOTE_MIN_FREEZE_MINS, so it can't be gamed away entirely).
   One pardon allowed per VOTE_COOLDOWN_HRS hours.
   Integration: poll mode (set TOPGG_TOKEN in .env) polls every 5 minutes.
   Webhook mode: call PenaltyManager.record_vote(user_id) from your endpoint.
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

import aiohttp
import aiohttp.web
import discord
from discord.ext import commands, tasks

logger = logging.getLogger("statsnitch.spam")


# ============================================================================
# ██  TUNEABLE CONSTANTS
# ============================================================================

# Detection thresholds
FLOOD_WINDOW_SECS    = 5      # seconds window for message flood check
FLOOD_MAX_MSGS       = 5      # messages in window before spam flag
DUPLICATE_WINDOW     = 10     # seconds to look for near-identical messages
DUPLICATE_THRESHOLD  = 0.85   # similarity ratio (0–1); 0.85 = very similar
DUPLICATE_MAX        = 3      # repeated near-dupes before spam flag
MENTION_MAX_PER_MSG  = 4      # @mentions in one message → instant flag
MENTION_WINDOW_SECS  = 10     # window for burst-mention check
MENTION_BURST_MAX    = 6      # total @mentions in that window → flag
ATTACH_WINDOW_SECS   = 8      # window for attachment flood
ATTACH_MAX           = 4      # attachments in that window → flag
CAPS_RUN_MAX         = 4      # consecutive caps-heavy messages → flag
EMOJI_DENSITY_MAX    = 0.6    # fraction of chars that are emoji → flag
EMOJI_MIN_LENGTH     = 10     # minimum message length for emoji density check

# Penalty queue
PENALTY_COOLDOWN     = 3      # seconds between draining queued penalties
PENALTY_IMMUNE_SECS  = 8      # ignore further detections this many seconds after a penalty

# Freeze durations per strike (seconds) — strikes beyond table use the last entry
FREEZE_SCHEDULE: dict[int, int] = {
    1:  10 * 60,              # 10 minutes
    2:   1 * 3600,            # 1 hour
    3:   6 * 3600,            # 6 hours
    4:  24 * 3600,            # 24 hours
    5:   3 * 24 * 3600,       # 3 days
    6:   7 * 24 * 3600,       # 7 days  ← cap; strikes 7+ reuse this
}

# Discord timeout per strike (timedelta or None)
TIMEOUT_SCHEDULE: dict[int, Optional[timedelta]] = {
    1: None,
    2: None,
    3: timedelta(minutes=5),
    4: timedelta(minutes=30),
    5: timedelta(hours=2),
    6: timedelta(hours=6),    # cap
}

# Vote pardon
VOTE_PARDON_DIVISOR  = 2      # freeze is divided by this on vote
VOTE_MIN_FREEZE_MINS = 5      # minimum remaining freeze after pardon (minutes)
VOTE_COOLDOWN_HRS    = 12     # hours before same user can pardon again

# top.gg poll interval
TOPGG_POLL_INTERVAL_MINS = 5

# Strike decay
STRIKE_DECAY_DAYS = 30        # days clean before one strike is removed


# ============================================================================
# ██  HELPERS
# ============================================================================

def _freeze_secs(strike: int) -> int:
    cap = max(FREEZE_SCHEDULE.keys())
    return FREEZE_SCHEDULE[min(strike, cap)]


def _timeout_delta(strike: int) -> Optional[timedelta]:
    cap = max(TIMEOUT_SCHEDULE.keys())
    return TIMEOUT_SCHEDULE[min(strike, cap)]


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        h, m = divmod(secs, 3600)
        return f"{h}h {m//60}m" if m else f"{h}h"
    d, rem = divmod(secs, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _emoji_density(text: str) -> float:
    emoji_chars = re.findall(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF"
        r"\U0001F000-\U0001F0FF\U0001FA00-\U0001FA6F"
        r"\U0001FA70-\U0001FAFF\u2702-\u27B0]",
        text,
    )
    return len(emoji_chars) / max(len(text), 1)


def _freeze_remaining_str(fu_str: Optional[str]) -> str:
    if not fu_str:
        return "✅ Not frozen"
    try:
        fu  = datetime.fromisoformat(fu_str)
        now = datetime.now(timezone.utc)
        if fu <= now:
            return "✅ Not frozen"
        return f"❄️ Frozen · **{_fmt_duration((fu - now).total_seconds())} remaining**"
    except ValueError:
        return "✅ Not frozen"


# ============================================================================
# ██  DETECTION
# ============================================================================

@dataclass
class SpamResult:
    is_spam: bool
    reason: str = ""
    delete_message: bool = False


@dataclass
class _UserWindow:
    recent_messages:    deque = field(default_factory=lambda: deque(maxlen=20))
    recent_attachments: deque = field(default_factory=lambda: deque(maxlen=20))
    recent_mentions:    deque = field(default_factory=lambda: deque(maxlen=30))
    caps_run: int = 0


class SpamDetector:
    """Pure detection — no Discord side-effects."""

    def __init__(self):
        self._windows: dict[str, _UserWindow] = {}

    def _window(self, uid: str) -> _UserWindow:
        if uid not in self._windows:
            self._windows[uid] = _UserWindow()
        return self._windows[uid]

    def check(self, uid: str, content: str,
              mention_count: int, has_attachment: bool) -> SpamResult:
        now = time.monotonic()
        w   = self._window(uid)

        # 1. Message flood
        w.recent_messages.append((now, content))
        flood_msgs = [t for t, _ in w.recent_messages if now - t <= FLOOD_WINDOW_SECS]
        if len(flood_msgs) >= FLOOD_MAX_MSGS:
            return SpamResult(True,
                f"message flood ({len(flood_msgs)} msgs in {FLOOD_WINDOW_SECS}s)",
                delete_message=True)

        # 2. Duplicate spam
        recent_texts = [c for t, c in w.recent_messages if now - t <= DUPLICATE_WINDOW]
        dupe_count = sum(
            1 for old in recent_texts[:-1]
            if _similar(content, old) >= DUPLICATE_THRESHOLD and len(content.strip()) > 5
        )
        if dupe_count >= DUPLICATE_MAX - 1:
            return SpamResult(True,
                f"duplicate messages ({dupe_count + 1}× similar content)",
                delete_message=True)

        # 3. Mention bomb (single message)
        if mention_count > MENTION_MAX_PER_MSG:
            return SpamResult(True,
                f"mention bomb ({mention_count} @mentions in one message)",
                delete_message=True)

        # 4. Mention burst (across messages)
        w.recent_mentions.extend([now] * mention_count)
        burst_mentions = sum(1 for t in w.recent_mentions if now - t <= MENTION_WINDOW_SECS)
        if burst_mentions >= MENTION_BURST_MAX:
            return SpamResult(True,
                f"mention burst ({burst_mentions} mentions in {MENTION_WINDOW_SECS}s)",
                delete_message=True)

        # 5. Attachment flood
        if has_attachment:
            w.recent_attachments.append(now)
        recent_attach = [t for t in w.recent_attachments if now - t <= ATTACH_WINDOW_SECS]
        if len(recent_attach) >= ATTACH_MAX:
            return SpamResult(True,
                f"attachment flood ({len(recent_attach)} files in {ATTACH_WINDOW_SECS}s)",
                delete_message=True)

        # 6. Caps run
        alpha = [c for c in content if c.isalpha()]
        is_caps_heavy = bool(alpha) and (
            sum(1 for c in alpha if c.isupper()) / len(alpha) > 0.7
        )
        w.caps_run = w.caps_run + 1 if is_caps_heavy else 0
        if w.caps_run >= CAPS_RUN_MAX:
            return SpamResult(True,
                f"caps spam ({w.caps_run} ALL-CAPS messages in a row)",
                delete_message=False)

        # 7. Emoji density
        if len(content) >= EMOJI_MIN_LENGTH and _emoji_density(content) >= EMOJI_DENSITY_MAX:
            pct = int(_emoji_density(content) * 100)
            return SpamResult(True,
                f"emoji flood ({pct}% of message is emojis)",
                delete_message=False)

        return SpamResult(False)


# ============================================================================
# ██  PENALTY MANAGER
# ============================================================================

@dataclass
class _PendingPenalty:
    message:        discord.Message
    reason:         str
    delete_message: bool


class PenaltyManager:
    """
    Queues spam penalties per-user and drains them one at a time with a
    cooldown gap so the bot never fires multiple timeouts simultaneously.
    Also handles vote pardons and strike decay.
    """

    def __init__(self, storage):
        self._storage          = storage
        self._queues:           dict[str, asyncio.Queue] = {}
        self._workers:          dict[str, asyncio.Task]  = {}
        self._last_fire:        dict[str, float]          = {}  # monotonic
        self._last_vote_pardon: dict[str, float]          = {}  # monotonic

    # ------------------------------------------------------------------
    # Enqueue detection
    # ------------------------------------------------------------------

    async def enqueue(self, result: SpamResult, message: discord.Message) -> None:
        """
        Entry point from bot.py. Deletes the message immediately if needed,
        then queues the announcement + timeout work to be drip-fed with
        PENALTY_COOLDOWN seconds between each.
        """
        uid = str(message.author.id)

        # Immediate delete — don't wait in queue for this
        if result.delete_message:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

        # Skip queueing announcement if we're still in the immune window
        # (prevents the queue from exploding during a burst)
        if time.monotonic() - self._last_fire.get(uid, 0) < PENALTY_IMMUNE_SECS:
            logger.debug("Immune window active for %s — skipping announcement queue", uid)
            return

        if uid not in self._queues:
            self._queues[uid] = asyncio.Queue()

        await self._queues[uid].put(
            _PendingPenalty(message=message, reason=result.reason,
                            delete_message=result.delete_message)
        )
        self._ensure_worker(uid)

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    def _ensure_worker(self, uid: str) -> None:
        existing = self._workers.get(uid)
        if existing is None or existing.done():
            self._workers[uid] = asyncio.create_task(
                self._drain(uid), name=f"spam-worker-{uid}"
            )

    async def _drain(self, uid: str) -> None:
        """Drains one user's penalty queue, one item every PENALTY_COOLDOWN seconds."""
        q = self._queues[uid]
        while not q.empty():
            pending: _PendingPenalty = await q.get()
            try:
                await self._apply(pending)
            except Exception as exc:
                logger.error("Penalty apply error for %s: %s", uid, exc, exc_info=exc)
            finally:
                q.task_done()
            if not q.empty():
                await asyncio.sleep(PENALTY_COOLDOWN)
        self._workers.pop(uid, None)

    # ------------------------------------------------------------------
    # Core penalty application
    # ------------------------------------------------------------------

    async def _apply(self, pending: _PendingPenalty) -> None:
        uid   = str(pending.message.author.id)
        uname = pending.message.author.display_name
        u     = self._storage.get_user(uid, uname)

        # Decay old strikes before escalating
        self._decay_strikes(u)

        # Increment strike
        strikes = u.get("spam_strikes", 0) + 1
        u["spam_strikes"]          = strikes
        u["last_spam_reason"]      = pending.reason
        u["last_spam_timestamp"]   = datetime.now(timezone.utc).isoformat()

        # Set freeze
        freeze_s     = _freeze_secs(strikes)
        freeze_until = datetime.now(timezone.utc) + timedelta(seconds=freeze_s)
        u["stats_frozen_until"] = freeze_until.isoformat()

        self._last_fire[uid] = time.monotonic()

        logger.warning(
            "Penalty | user=%s strike=%d freeze=%s reason=%s",
            uname, strikes, _fmt_duration(freeze_s), pending.reason,
        )

        # Announce in channel
        timeout_delta = _timeout_delta(strikes)
        bot_id = self._storage._bot_id if hasattr(self._storage, '_bot_id') else 0
        announcement  = _build_announcement(uname, strikes, pending.reason,
                                             freeze_s, timeout_delta, bot_id=bot_id)
        try:
            await pending.message.channel.send(announcement)
        except discord.Forbidden:
            pass

        # Discord timeout
        if timeout_delta and isinstance(pending.message.author, discord.Member):
            try:
                until_dt = datetime.now(timezone.utc) + timeout_delta
                await pending.message.author.timeout(
                    until_dt,
                    reason=f"StatSnitch spam strike {strikes}: {pending.reason}",
                )
                logger.info("Discord timeout: %s for %s", uname, _fmt_duration(timeout_delta.total_seconds()))
            except discord.Forbidden:
                logger.warning("No permission to timeout %s", uname)
            except discord.HTTPException as e:
                logger.error("Timeout HTTP error for %s: %s", uname, e)

        await self._storage.save()

    # ------------------------------------------------------------------
    # Strike decay
    # ------------------------------------------------------------------

    @staticmethod
    def _decay_strikes(u: dict) -> None:
        if u.get("spam_strikes", 0) <= 0:
            return
        last_ts = u.get("last_spam_timestamp")
        if not last_ts:
            return
        try:
            last_dt    = datetime.fromisoformat(last_ts)
            days_clean = (datetime.now(timezone.utc) - last_dt).days
            if days_clean >= STRIKE_DECAY_DAYS:
                before = u["spam_strikes"]
                u["spam_strikes"] = max(0, before - 1)
                logger.info("Strike decayed for %s: %d → %d (%d days clean)",
                            u.get("username", "?"), before, u["spam_strikes"], days_clean)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Vote pardon
    # ------------------------------------------------------------------

    async def record_vote(self, user_id: str,
                          channel: Optional[discord.TextChannel] = None,
                          is_test: bool = False) -> None:
        """
        Called when a top.gg vote is received (webhook or poll).
        Halves the user's remaining freeze (min VOTE_MIN_FREEZE_MINS).
        is_test=True skips the cooldown check so you can test without waiting 12h.
        """
        uid = str(user_id)

        # In-memory cooldown (skip for test votes)
        if not is_test:
            if time.monotonic() - self._last_vote_pardon.get(uid, 0) < VOTE_COOLDOWN_HRS * 3600:
                logger.debug("Vote pardon cooldown active for %s", uid)
                return

        u = self._storage._data.get(uid)
        if not u:
            return

        fu_str = u.get("stats_frozen_until")
        if not fu_str:
            return

        try:
            fu  = datetime.fromisoformat(fu_str)
            now = datetime.now(timezone.utc)
            if fu <= now:
                return

            remaining_secs  = (fu - now).total_seconds()
            new_remaining   = max(remaining_secs / VOTE_PARDON_DIVISOR,
                                  VOTE_MIN_FREEZE_MINS * 60)
            new_fu          = now + timedelta(seconds=new_remaining)
            u["stats_frozen_until"]    = new_fu.isoformat()
            self._last_vote_pardon[uid] = time.monotonic()
            await self._storage.save()

            uname = u.get("username", f"<@{uid}>")
            test_label = " *(test vote)*" if is_test else ""
            logger.info("Vote pardon: %s freeze cut to %s%s",
                        uname, _fmt_duration(new_remaining), test_label)

            if channel:
                await channel.send(
                    f"🗳️ **{uname}** voted for StatSnitch on top.gg!{test_label}\n"
                    f"Freeze time cut in half → **{_fmt_duration(new_remaining)} remaining**.\n"
                    f"Bribery acknowledged. We respect the hustle. 🎉"
                )
        except (ValueError, TypeError) as e:
            logger.error("Vote pardon error for %s: %s", uid, e)


# ============================================================================
# ██  ANNOUNCEMENT TEXT
# ============================================================================

def _build_announcement(name: str, strikes: int, reason: str,
                         freeze_s: int, timeout_delta: Optional[timedelta],
                         bot_id: int = 0) -> str:
    freeze_str  = _fmt_duration(freeze_s)
    timeout_str = _fmt_duration(timeout_delta.total_seconds()) if timeout_delta else None

    headers = {
        1: f"⚠️ **Easy there, {name}.** First warning.",
        2: f"🚨 **Second strike, {name}.** Not a great look.",
        3: f"🔴 **Third strike, {name}.** Things are escalating.",
        4: f"🔥 **Fourth strike, {name}.** We're not laughing.",
        5: f"💀 **FIFTH STRIKE, {name}.** This is getting out of hand.",
    }
    header = headers.get(strikes, f"☠️ **STRIKE {strikes}, {name}.** At this point it's a lifestyle.")

    extra = ""
    if strikes >= 2:
        extra = " Spam messages deleted."
    if timeout_str:
        extra += f" Discord timeout: **{timeout_str}**."

    vote_hint = (
        f"\n💡 **Second chance:** Vote for the bot → <https://top.gg/bot/{bot_id}/vote>\n"
        f"Your pardon is applied **automatically** the moment your vote is counted."
        if strikes >= 2 else ""
    )

    return (
        f"{header}\n"
        f"📋 Reason: _{reason}_\n"
        f"❄️ Stats frozen for **{freeze_str}**.{extra}"
        f"{vote_hint}"
    )


# ============================================================================
# ██  TOP.GG POLL COG
# ============================================================================

# ============================================================================
# ██  TOP.GG INTEGRATION COG  (poll + optional webhook server)
# ============================================================================

class TopGGCog(commands.Cog, name="TopGG"):
    """
    Handles top.gg vote detection via two complementary methods:

    METHOD 1 — POLLING (always enabled when TOPGG_TOKEN is set)
      Polls the /votes endpoint every TOPGG_POLL_INTERVAL_MINS minutes.
      Simple, requires no open port. Slight delay (up to 5 min).
      NOTE: The /votes endpoint only returns the last 1000 votes total —
      this is fine for most bots but if you get >1000 votes, use webhooks.

    METHOD 2 — WEBHOOK (enabled when TOPGG_WEBHOOK_PORT is set)
      top.gg POSTs to your server instantly on every vote.
      Zero delay, works for any vote volume.
      Requires a public URL/port (or a reverse proxy like nginx/Caddy).
      Set TOPGG_WEBHOOK_PORT and TOPGG_WEBHOOK_AUTH in .env.
      Then point top.gg to: http://your-server-ip:PORT/topgg-webhook
      (Settings: https://top.gg/bot/YOUR_BOT_ID/webhooks)
    """

    def __init__(self, bot: commands.Bot, penalty_manager: PenaltyManager,
                 topgg_token: Optional[str],
                 webhook_port: Optional[int],
                 webhook_auth: Optional[str]):
        self.bot              = bot
        self.pm               = penalty_manager
        self._token           = topgg_token
        self._webhook_port    = webhook_port
        self._webhook_auth    = webhook_auth
        self._seen_votes:     set[str] = set()
        self._announce_ch:    Optional[discord.TextChannel] = None
        self._webhook_runner: Optional[aiohttp.web.AppRunner] = None

        if topgg_token:
            self.poll_votes.start()

    def cog_unload(self):
        if self._token:
            self.poll_votes.cancel()
        if self._webhook_runner:
            asyncio.create_task(self._stop_webhook())

    def set_announce_channel(self, channel: discord.TextChannel) -> None:
        self._announce_ch = channel

    # ── Webhook server ────────────────────────────────────────────────

    async def start_webhook_server(self) -> None:
        """Start the aiohttp webhook listener. Called from on_ready."""
        if not self._webhook_port:
            return

        app = aiohttp.web.Application()
        app.router.add_post("/topgg-webhook", self._handle_webhook)
        self._webhook_runner = aiohttp.web.AppRunner(app)
        await self._webhook_runner.setup()
        site = aiohttp.web.TCPSite(self._webhook_runner, "0.0.0.0", self._webhook_port)
        await site.start()
        logger.info("top.gg webhook server listening on port %s", self._webhook_port)

    async def _stop_webhook(self) -> None:
        if self._webhook_runner:
            await self._webhook_runner.cleanup()

    async def _handle_webhook(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        """
        POST /topgg-webhook
        top.gg sends: { "bot": "...", "user": "...", "type": "upvote"|"test", "isWeekend": bool }
        Must respond 200 within 5 seconds or top.gg retries (up to 10 times, exponential backoff).
        """
        # Verify authorization header
        if self._webhook_auth:
            auth = request.headers.get("Authorization", "")
            if auth != self._webhook_auth:
                logger.warning("top.gg webhook: bad auth header")
                return aiohttp.web.Response(status=401, text="Unauthorized")

        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.Response(status=400, text="Bad JSON")

        vote_type = data.get("type", "")
        user_id   = str(data.get("user", ""))
        is_test   = vote_type == "test"

        logger.info("top.gg webhook received: user=%s type=%s", user_id, vote_type)

        if user_id and (vote_type == "upvote" or is_test):
            # Fire-and-forget — we must return 200 within 5s
            asyncio.create_task(
                self.pm.record_vote(user_id, channel=self._announce_ch, is_test=is_test)
            )

        # Acknowledge immediately — top.gg requires 200 within 5 seconds
        return aiohttp.web.Response(status=200, text="OK")

    # ── Poll fallback ─────────────────────────────────────────────────

    @tasks.loop(minutes=TOPGG_POLL_INTERVAL_MINS)
    async def poll_votes(self):
        """Poll top.gg /votes endpoint for recent voters."""
        if not self.bot.user or not self._token:
            return
        url     = f"https://top.gg/api/bots/{self.bot.user.id}/votes"
        headers = {"Authorization": self._token}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("top.gg poll: HTTP %s", resp.status)
                        return
                    votes = await resp.json()
        except Exception as e:
            logger.error("top.gg poll error: %s", e)
            return

        for voter in votes:
            user_id = str(voter.get("id", ""))
            if not user_id or user_id in self._seen_votes:
                continue
            self._seen_votes.add(user_id)
            await self.pm.record_vote(user_id, channel=self._announce_ch)

        # Prevent unbounded growth
        if len(self._seen_votes) > 500:
            self._seen_votes = set(list(self._seen_votes)[-500:])

    @poll_votes.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


# ============================================================================
# ██  ADMIN COG
# ============================================================================

class SpamAdminCog(commands.Cog, name="SpamAdmin"):
    """Mod-only spam management commands."""

    def __init__(self, bot: commands.Bot, storage, detector: SpamDetector,
                 penalty_manager: PenaltyManager):
        self.bot = bot
        self.db  = storage
        self.det = detector
        self.pm  = penalty_manager

    def _is_mod(self, ctx: commands.Context) -> bool:
        if isinstance(ctx.author, discord.Member):
            return (
                ctx.author.guild_permissions.moderate_members
                or ctx.author.guild_permissions.administrator
            )
        return False

    @commands.command(name="spamstatus")
    async def spamstatus(self, ctx: commands.Context,
                         member: discord.Member | None = None):
        """📋 Show spam record for yourself or another user."""
        target = member or ctx.author
        uid    = str(target.id)
        u      = self.db.get_user(uid, target.display_name)

        strikes     = u.get("spam_strikes", 0)
        last_reason = u.get("last_spam_reason", "N/A")
        freeze_str  = _freeze_remaining_str(u.get("stats_frozen_until"))

        next_strike       = strikes + 1
        next_freeze_str   = _fmt_duration(_freeze_secs(next_strike))
        next_timeout      = _timeout_delta(next_strike)
        next_timeout_str  = _fmt_duration(next_timeout.total_seconds()) if next_timeout else "none"

        color = (discord.Color.green()  if strikes == 0 else
                 discord.Color.yellow() if strikes <= 2 else
                 discord.Color.red())

        embed = discord.Embed(
            title=f"🚨 Spam Record: {target.display_name}", color=color
        )
        embed.add_field(name="🎯 Strikes",       value=str(strikes),      inline=True)
        embed.add_field(name="🔒 Freeze status",  value=freeze_str,        inline=True)
        embed.add_field(name="📋 Last reason",    value=last_reason,       inline=False)
        embed.add_field(
            name="⚠️ Next offence",
            value=f"Freeze **{next_freeze_str}** + Discord timeout **{next_timeout_str}**",
            inline=False,
        )
        if strikes >= 2:
            embed.set_footer(text=f"💡 Vote on top.gg to automatically halve your freeze!")
        await ctx.send(embed=embed)

    @commands.command(name="spamclear")
    async def spamclear(self, ctx: commands.Context, member: discord.Member):
        """🧹 Clear spam strikes and unfreeze a user. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return
        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)
        u["spam_strikes"]        = 0
        u["stats_frozen_until"]  = None
        u["last_spam_reason"]    = None
        u["last_spam_timestamp"] = None
        await self.db.save()
        await ctx.send(
            f"✅ Cleared spam record for **{member.display_name}**. "
            f"Don't make me regret this."
        )

    @commands.command(name="spamfreeze")
    async def spamfreeze(self, ctx: commands.Context,
                         member: discord.Member, hours: float = 1.0):
        """❄️ Manually freeze a user's stats for N hours. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return
        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)
        fu  = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        u["stats_frozen_until"] = fu
        u["last_spam_reason"]   = f"Manual freeze by {ctx.author.display_name}"
        await self.db.save()
        await ctx.send(
            f"❄️ **{member.display_name}**'s stats frozen for "
            f"**{hours:.1f} hour(s)**. Cold-blooded. Love it."
        )

    @commands.command(name="spamunfreeze")
    async def spamunfreeze(self, ctx: commands.Context, member: discord.Member):
        """🔓 Unfreeze a user's stats immediately. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return
        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)
        u["stats_frozen_until"] = None
        await self.db.save()
        await ctx.send(f"🔓 **{member.display_name}**'s stats are unfrozen. Play nice.")

    @commands.command(name="spamlog")
    async def spamlog(self, ctx: commands.Context):
        """📊 Show all users with spam strikes. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return
        flagged = sorted(
            [u for u in self.db.all_users() if u.get("spam_strikes", 0) > 0],
            key=lambda u: u.get("spam_strikes", 0), reverse=True,
        )
        if not flagged:
            await ctx.send("📋 No spam records. Suspicious.")
            return
        embed = discord.Embed(title="🚨 Spam Records", color=discord.Color.red())
        lines = []
        for u in flagged[:15]:
            freeze = _freeze_remaining_str(u.get("stats_frozen_until"))
            lines.append(
                f"**{u['username']}** — {u.get('spam_strikes',0)} strike(s) · {freeze}"
            )
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.command(name="spamqueue")
    async def spamqueue(self, ctx: commands.Context):
        """⚙️ Show active penalty queue depths. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return
        active = {uid: q.qsize() for uid, q in self.pm._queues.items() if not q.empty()}
        if not active:
            await ctx.send("📭 Penalty queue is empty. Everyone's behaving. For now.")
            return
        lines = [f"<@{uid}>: **{n}** pending" for uid, n in active.items()]
        await ctx.send("📬 **Active penalty queues:**\n" + "\n".join(lines))