"""
spam.py — Spam detection and penalty system for StatSnitch.

Detection signals (all thresholds configurable at top of file):
  - Message flood      : too many messages in a short window
  - Duplicate spam     : same/near-identical content repeated
  - Mention spam       : too many @mentions in one message or burst
  - Attachment flood   : too many files/images rapidly
  - Caps flood         : too many ALL-CAPS messages in a row
  - Emoji flood        : absurd emoji density in a single message

Penalty tiers (escalating per user):
  Tier 1 (1st offence)  → warning DM only, stats frozen for 10 min
  Tier 2 (2nd offence)  → warning, stats frozen for 1 hour, spam msgs deleted
  Tier 3 (3rd offence)  → warning, stats frozen for 24 hours, Discord timeout 5 min
  Tier 4 (4th+ offence) → warning, stats frozen for 7 days,  Discord timeout 1 hour

Stats-freeze means the user's messages/words/etc. are NOT counted during the
freeze window — they still show on leaderboards but their counts stop growing.
Their spam_strikes and freeze data are persisted in storage.
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import discord
from discord.ext import commands

logger = logging.getLogger("statsnitch.spam")

# ---------------------------------------------------------------------------
# Tuneable thresholds
# ---------------------------------------------------------------------------
FLOOD_WINDOW_SECS    = 5      # seconds to look back for message flood
FLOOD_MAX_MSGS       = 5      # max messages in that window before flagging
DUPLICATE_WINDOW     = 10     # seconds to check for duplicate messages
DUPLICATE_THRESHOLD  = 0.85   # similarity ratio (0-1) to count as duplicate
DUPLICATE_MAX        = 3      # how many near-dupes trigger spam
MENTION_MAX_PER_MSG  = 4      # @mentions in a single message
MENTION_WINDOW_SECS  = 10     # seconds for burst-mention check
MENTION_BURST_MAX    = 6      # total mentions across messages in that window
ATTACH_WINDOW_SECS   = 8      # seconds for attachment flood window
ATTACH_MAX           = 4      # max attachments in that window
CAPS_RUN_MAX         = 4      # consecutive caps-heavy messages before flagging
EMOJI_DENSITY_MAX    = 0.6    # fraction of message chars that are emojis → spam
EMOJI_MIN_LENGTH     = 10     # only apply density check if message is this long

# Freeze durations per tier (in seconds)
FREEZE_DURATIONS = {
    1: 10 * 60,          # 10 minutes
    2: 60 * 60,          # 1 hour
    3: 24 * 60 * 60,     # 24 hours
    4: 7 * 24 * 60 * 60, # 7 days
}

# Discord timeout durations per tier (timedelta, None = no timeout)
TIMEOUT_DURATIONS = {
    1: None,
    2: None,
    3: timedelta(minutes=5),
    4: timedelta(hours=1),
}


# ---------------------------------------------------------------------------
# Per-user sliding-window state (in memory only — resets on restart)
# ---------------------------------------------------------------------------
@dataclass
class _UserWindow:
    # (timestamp, content) pairs for flood / duplicate detection
    recent_messages: deque = field(default_factory=lambda: deque(maxlen=20))
    # timestamps of recent attachment messages
    recent_attachments: deque = field(default_factory=lambda: deque(maxlen=20))
    # timestamps of recent @mention events
    recent_mentions: deque = field(default_factory=lambda: deque(maxlen=30))
    # consecutive caps messages
    caps_run: int = 0


def _similar(a: str, b: str) -> float:
    """Return similarity ratio between two strings."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _emoji_density(text: str) -> float:
    """Return fraction of characters that are emoji/unicode pictographs."""
    emoji_chars = re.findall(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF"
        r"\U0001F000-\U0001F0FF\U0001FA00-\U0001FA6F"
        r"\U0001FA70-\U0001FAFF\u2702-\u27B0]",
        text,
    )
    return len(emoji_chars) / max(len(text), 1)


# ---------------------------------------------------------------------------
# Detection result
# ---------------------------------------------------------------------------
@dataclass
class SpamResult:
    is_spam: bool
    reason: str = ""
    delete_message: bool = False   # should the triggering message be deleted?


# ---------------------------------------------------------------------------
# Main detector class
# ---------------------------------------------------------------------------
class SpamDetector:
    """
    Stateless-per-instance spam detector.
    Call `check(message)` for every incoming non-bot message.
    """

    def __init__(self):
        self._windows: dict[str, _UserWindow] = {}

    def _window(self, uid: str) -> _UserWindow:
        if uid not in self._windows:
            self._windows[uid] = _UserWindow()
        return self._windows[uid]

    def check(self, uid: str, content: str, mention_count: int,
              has_attachment: bool) -> SpamResult:
        """
        Run all spam checks for a message.
        Returns a SpamResult indicating whether spam was detected and why.
        """
        now = time.monotonic()
        w   = self._window(uid)

        # --- 1. Message flood ---
        w.recent_messages.append((now, content))
        recent_flood = [t for t, _ in w.recent_messages if now - t <= FLOOD_WINDOW_SECS]
        if len(recent_flood) >= FLOOD_MAX_MSGS:
            return SpamResult(True, f"message flood ({len(recent_flood)} msgs in {FLOOD_WINDOW_SECS}s)", delete_message=True)

        # --- 2. Duplicate / near-duplicate spam ---
        recent_texts = [c for t, c in w.recent_messages if now - t <= DUPLICATE_WINDOW]
        dupe_count = sum(
            1 for old in recent_texts[:-1]
            if _similar(content, old) >= DUPLICATE_THRESHOLD and len(content.strip()) > 5
        )
        if dupe_count >= DUPLICATE_MAX - 1:  # -1 because current msg is already appended
            return SpamResult(True, f"duplicate messages ({dupe_count+1}× similar content)", delete_message=True)

        # --- 3. Mention spam (single message) ---
        if mention_count > MENTION_MAX_PER_MSG:
            return SpamResult(True, f"mention bomb ({mention_count} @mentions in one message)", delete_message=True)

        # --- 4. Mention burst (across messages) ---
        w.recent_mentions.extend([now] * mention_count)
        burst_mentions = sum(1 for t in w.recent_mentions if now - t <= MENTION_WINDOW_SECS)
        if burst_mentions >= MENTION_BURST_MAX:
            return SpamResult(True, f"mention burst ({burst_mentions} mentions in {MENTION_WINDOW_SECS}s)", delete_message=True)

        # --- 5. Attachment flood ---
        if has_attachment:
            w.recent_attachments.append(now)
        recent_attach = [t for t in w.recent_attachments if now - t <= ATTACH_WINDOW_SECS]
        if len(recent_attach) >= ATTACH_MAX:
            return SpamResult(True, f"attachment flood ({len(recent_attach)} files in {ATTACH_WINDOW_SECS}s)", delete_message=True)

        # --- 6. Caps run ---
        alpha = [c for c in content if c.isalpha()]
        is_caps_heavy = bool(alpha) and sum(1 for c in alpha if c.isupper()) / len(alpha) > 0.7
        if is_caps_heavy:
            w.caps_run += 1
        else:
            w.caps_run = 0
        if w.caps_run >= CAPS_RUN_MAX:
            return SpamResult(True, f"caps spam ({w.caps_run} ALL-CAPS messages in a row)", delete_message=False)

        # --- 7. Emoji density ---
        if len(content) >= EMOJI_MIN_LENGTH and _emoji_density(content) >= EMOJI_DENSITY_MAX:
            density_pct = int(_emoji_density(content) * 100)
            return SpamResult(True, f"emoji flood ({density_pct}% of message is emojis)", delete_message=False)

        return SpamResult(False)


# ---------------------------------------------------------------------------
# Penalty announcements
# ---------------------------------------------------------------------------

_TIER_MESSAGES = {
    1: (
        "⚠️ **Spam detected!** Take it easy, {name}.\n"
        "Reason: _{reason}_\n"
        "Your stats are **frozen for 10 minutes**. Calm down."
    ),
    2: (
        "🚨 **Second offence, {name}.** Not great.\n"
        "Reason: _{reason}_\n"
        "Your stats are **frozen for 1 hour** and your spam messages were deleted."
    ),
    3: (
        "🔴 **Third strike, {name}.** Discord timeout incoming.\n"
        "Reason: _{reason}_\n"
        "Stats frozen **24 hours**. Discord timeout **5 minutes**.\n"
        "Maybe touch grass?"
    ),
    4: (
        "💀 **Repeat offender alert: {name}.**\n"
        "Reason: _{reason}_\n"
        "Stats frozen **7 days**. Discord timeout **1 hour**.\n"
        "We'd say we're surprised, but we're not."
    ),
}


async def apply_penalty(
    message: discord.Message,
    reason: str,
    delete_msg: bool,
    storage,        # Storage instance (avoid circular import typing)
) -> None:
    """
    Apply the appropriate penalty tier for a spam detection.
    Modifies the user's storage record and optionally applies a Discord timeout.
    """
    uid   = str(message.author.id)
    uname = message.author.display_name

    # Increment strike counter in storage
    u = storage.get_user(uid, uname)
    strikes = u.get("spam_strikes", 0) + 1
    u["spam_strikes"] = strikes

    # Determine tier (cap at 4)
    tier = min(strikes, 4)

    # Set freeze expiry
    freeze_secs = FREEZE_DURATIONS[tier]
    freeze_until = (datetime.now(timezone.utc) + timedelta(seconds=freeze_secs)).isoformat()
    u["stats_frozen_until"] = freeze_until
    u["last_spam_reason"]   = reason

    logger.warning(
        "Spam detected from %s (uid=%s) tier=%d reason=%s",
        uname, uid, tier, reason,
    )

    # Delete the offending message
    if delete_msg:
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    # Post public warning in the channel
    announcement = _TIER_MESSAGES[tier].format(name=uname, reason=reason)
    try:
        await message.channel.send(announcement)
    except discord.Forbidden:
        pass

    # Apply Discord timeout for tier 3+
    timeout_delta = TIMEOUT_DURATIONS.get(tier)
    if timeout_delta and isinstance(message.author, discord.Member):
        try:
            until_dt = datetime.now(timezone.utc) + timeout_delta
            await message.author.timeout(until_dt, reason=f"StatSnitch spam: {reason}")
            logger.info("Timed out %s for %s", uname, timeout_delta)
        except discord.Forbidden:
            logger.warning("Missing permissions to timeout %s", uname)
        except discord.HTTPException as e:
            logger.error("Failed to timeout %s: %s", uname, e)

    # Force-save so the freeze persists
    await storage.save()


# ---------------------------------------------------------------------------
# Admin commands Cog
# ---------------------------------------------------------------------------
class SpamAdminCog(commands.Cog, name="SpamAdmin"):
    """Mod-only spam management commands."""

    def __init__(self, bot: commands.Bot, storage, detector: SpamDetector):
        self.bot      = bot
        self.db       = storage
        self.detector = detector

    def _is_mod(self, ctx: commands.Context) -> bool:
        if isinstance(ctx.author, discord.Member):
            return (
                ctx.author.guild_permissions.moderate_members
                or ctx.author.guild_permissions.administrator
            )
        return False

    @commands.command(name="spamstatus")
    async def spamstatus(self, ctx: commands.Context, member: discord.Member | None = None):
        """📋 Show spam record for a user (or yourself)."""
        target = member or ctx.author
        uid    = str(target.id)
        u      = self.db.get_user(uid, target.display_name)

        strikes     = u.get("spam_strikes", 0)
        frozen_until = u.get("stats_frozen_until")
        last_reason  = u.get("last_spam_reason", "N/A")

        if frozen_until:
            try:
                fu = datetime.fromisoformat(frozen_until)
                now = datetime.now(timezone.utc)
                if fu > now:
                    remaining = fu - now
                    mins = int(remaining.total_seconds() // 60)
                    freeze_str = f"⏳ Frozen for **{mins} more minutes**"
                else:
                    freeze_str = "✅ Not currently frozen"
            except ValueError:
                freeze_str = "✅ Not currently frozen"
        else:
            freeze_str = "✅ Never frozen"

        embed = discord.Embed(
            title=f"🚨 Spam Record: {target.display_name}",
            color=discord.Color.red() if strikes > 0 else discord.Color.green(),
        )
        embed.add_field(name="🎯 Strikes",      value=str(strikes),    inline=True)
        embed.add_field(name="🔒 Freeze status", value=freeze_str,      inline=False)
        embed.add_field(name="📋 Last reason",   value=last_reason,     inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="spamclear")
    async def spamclear(self, ctx: commands.Context, member: discord.Member):
        """🧹 Clear a user's spam strikes and freeze. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return

        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)
        u["spam_strikes"]      = 0
        u["stats_frozen_until"] = None
        u["last_spam_reason"]  = None
        await self.db.save()
        await ctx.send(
            f"✅ Cleared spam record for **{member.display_name}**. "
            f"Don't make me regret this."
        )

    @commands.command(name="spamfreeze")
    async def spamfreeze(self, ctx: commands.Context, member: discord.Member, hours: float = 1.0):
        """❄️ Manually freeze a user's stats for N hours. (Mods only)"""
        if not self._is_mod(ctx):
            await ctx.send("⛔ You need `Moderate Members` permission for that.")
            return

        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)
        freeze_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        u["stats_frozen_until"] = freeze_until
        u["last_spam_reason"]   = f"Manual freeze by {ctx.author.display_name}"
        await self.db.save()
        await ctx.send(
            f"❄️ **{member.display_name}**'s stats frozen for **{hours:.1f} hour(s)**. "
            f"Cold-blooded. Love it."
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

        flagged = [
            u for u in self.db.all_users()
            if u.get("spam_strikes", 0) > 0
        ]
        flagged.sort(key=lambda u: u.get("spam_strikes", 0), reverse=True)

        if not flagged:
            await ctx.send("📋 No spam records. Suspicious.")
            return

        embed = discord.Embed(title="🚨 Spam Records", color=discord.Color.red())
        lines = []
        now = datetime.now(timezone.utc)
        for u in flagged[:15]:
            frozen = ""
            fu_str = u.get("stats_frozen_until")
            if fu_str:
                try:
                    fu = datetime.fromisoformat(fu_str)
                    if fu > now:
                        mins = int((fu - now).total_seconds() // 60)
                        frozen = f" ❄️ {mins}m left"
                except ValueError:
                    pass
            lines.append(
                f"**{u['username']}** — {u.get('spam_strikes', 0)} strike(s){frozen}"
            )
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)