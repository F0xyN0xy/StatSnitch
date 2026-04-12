"""
bot.py — StatSnitch: the Discord stats bot that judges you.

Usage:
    python bot.py

Required environment variables (see .env.example):
    DISCORD_TOKEN       — your bot token
    JSONBIN_API_KEY     — your JSONBin master key
    JSONBIN_BIN_ID      — your bin ID (leave blank to auto-create)
    BOT_PREFIX          — command prefix (default: !)
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from storage import Storage
from commands import StatCog
from personality import MILESTONES, milestone_message, streak_milestone_message
from spam import SpamDetector, SpamAdminCog, TopGGCog, PenaltyManager
from owner import OwnerCog

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("statsnitch.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("statsnitch")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv()

TOKEN         = os.getenv("DISCORD_TOKEN")
JSONBIN_KEY   = os.getenv("JSONBIN_API_KEY")
JSONBIN_BIN   = os.getenv("JSONBIN_BIN_ID") or None
BOT_PREFIX    = os.getenv("BOT_PREFIX", ".")
OWNER_ID      = int(os.getenv("OWNER_ID", "0"))
TOPGG_TOKEN              = os.getenv("TOPGG_TOKEN") or None
TOPGG_ANNOUNCE_CHANNEL   = os.getenv("TOPGG_ANNOUNCE_CHANNEL_ID") or None
TOPGG_WEBHOOK_PORT       = int(os.getenv("TOPGG_WEBHOOK_PORT", "0")) or None
TOPGG_WEBHOOK_AUTH       = os.getenv("TOPGG_WEBHOOK_AUTH") or None

if not TOKEN:
    sys.exit("❌  DISCORD_TOKEN is not set. Check your .env file.")
if not JSONBIN_KEY:
    sys.exit("❌  JSONBIN_API_KEY is not set. Check your .env file.")


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content  = True
intents.members          = True
intents.reactions        = True
intents.voice_states     = True
intents.guilds           = True

def _get_prefix(bot_instance, message: discord.Message):
    """Accept both BOT_PREFIX (.) for regular commands and ',' for owner commands."""
    return [BOT_PREFIX, ","]

bot            = commands.Bot(command_prefix=_get_prefix, intents=intents, help_command=None)
db             = Storage(api_key=JSONBIN_KEY, bin_id=JSONBIN_BIN)
spam_detector  = SpamDetector()
penalty_manager = PenaltyManager(db)

# Track previous message counts per user for milestone detection
_prev_counts: dict[str, int] = {}
# Track total commands used per user for vote nudge
_cmd_counts: dict[str, int] = {}
VOTE_NUDGE_EVERY = 25   # nudge at 25, 50, 75, 100 … commands


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    await db.load()

    # Seed previous counts
    for u in db.all_users():
        _prev_counts[u["user_id"]] = u["total_messages"]

    # Store bot ID on storage so penalty announcements can build the vote URL
    db._bot_id = bot.user.id

    await bot.add_cog(StatCog(bot, db))
    await bot.add_cog(SpamAdminCog(bot, db, spam_detector, penalty_manager))
    await bot.add_cog(OwnerCog(bot, db, penalty_manager, OWNER_ID))

    if OWNER_ID:
        logger.info("Owner commands enabled for user ID %s (prefix: ,)", OWNER_ID)
    else:
        logger.warning("OWNER_ID not set — owner commands (,unfreeze etc.) are disabled.")

    # top.gg vote pardon integration
    if TOPGG_TOKEN or TOPGG_WEBHOOK_PORT:
        topgg_cog = TopGGCog(
            bot=bot,
            penalty_manager=penalty_manager,
            topgg_token=TOPGG_TOKEN,
            webhook_port=TOPGG_WEBHOOK_PORT,
            webhook_auth=TOPGG_WEBHOOK_AUTH,
        )
        if TOPGG_ANNOUNCE_CHANNEL:
            channel = bot.get_channel(int(TOPGG_ANNOUNCE_CHANNEL))
            if isinstance(channel, discord.TextChannel):
                topgg_cog.set_announce_channel(channel)
        await bot.add_cog(topgg_cog)
        if TOPGG_WEBHOOK_PORT:
            await topgg_cog.start_webhook_server()
            logger.info("top.gg webhook server started on port %s", TOPGG_WEBHOOK_PORT)
        if TOPGG_TOKEN:
            logger.info("top.gg vote polling enabled (every %d min)", 5)
    else:
        logger.info("No TOPGG_TOKEN or TOPGG_WEBHOOK_PORT — vote pardons via automatic detection only.")

    periodic_flush.start()
    logger.info("StatSnitch is online and judging everyone. 👀")
    print(f"\n✅  StatSnitch online as {bot.user}\n")


# ---------------------------------------------------------------------------
# Periodic flush task
# ---------------------------------------------------------------------------
@tasks.loop(minutes=5)
async def periodic_flush():
    try:
        await db.save()
        logger.debug("Periodic flush completed.")
    except Exception as exc:
        # save() handles retries internally — this is a last-resort catch so
        # the task loop itself never terminates from a transient network error
        logger.error("Periodic flush unexpected error: %s", exc)


@periodic_flush.error
async def periodic_flush_error(exc: Exception):
    """
    Called by discord.py when periodic_flush raises an unhandled exception.
    We log it and let the task keep running — don't re-raise.
    The DNS errors in the logs are harmless transient blips; the task
    auto-resumes and data is safe in memory until the next flush succeeds.
    """
    logger.warning(
        "periodic_flush error (task will auto-retry): %s: %s",
        type(exc).__name__, exc,
    )


@periodic_flush.before_loop
async def before_flush():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Message tracking
# ---------------------------------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    uid   = str(message.author.id)
    uname = message.author.display_name
    ts    = message.created_at.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------
    # Developer / tester bypass — skip ALL tracking and spam detection
    # but still process commands so they can test bot responses normally
    # ------------------------------------------------------------------
    if db.is_dev(uid, owner_id=OWNER_ID):
        await bot.process_commands(message)
        return

    # ------------------------------------------------------------------
    # Spam detection — runs BEFORE stat tracking
    # ------------------------------------------------------------------
    mention_count = len([m for m in message.mentions if not m.bot])
    spam_result = spam_detector.check(
        uid=uid,
        content=message.content,
        mention_count=mention_count,
        has_attachment=bool(message.attachments),
    )
    if spam_result.is_spam:
        await penalty_manager.enqueue(spam_result, message)
        # Don't count this message toward stats; don't process as command
        return

    # ------------------------------------------------------------------
    # Normal stat tracking
    # ------------------------------------------------------------------
    db.track_message(
        user_id=uid,
        username=uname,
        content=message.content,
        has_attachment=bool(message.attachments),
        timestamp=ts,
        bot_prefix=BOT_PREFIX,
    )

    # Track mentions received
    for mentioned in message.mentions:
        if not mentioned.bot:
            db.track_mention_received(str(mentioned.id), mentioned.display_name)

    # Milestone check
    u    = db.get_user(uid, uname)
    prev = _prev_counts.get(uid, 0)
    curr = u["total_messages"]
    for threshold, label in MILESTONES.items():
        if prev < threshold <= curr:
            await message.channel.send(milestone_message(uname, label))
    _prev_counts[uid] = curr

    # Streak milestone check
    if u["current_streak"] == 7:
        await message.channel.send(streak_milestone_message(uname, 7))
    elif u["current_streak"] == 30:
        await message.channel.send(streak_milestone_message(uname, 30))

    # Maybe flush
    await db.maybe_flush()

    # Process commands
    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Vote nudge — fires every VOTE_NUDGE_EVERY commands per user
# ---------------------------------------------------------------------------
@bot.event
async def on_command(ctx: commands.Context):
    """Fires after every successfully parsed command invocation."""
    if ctx.author.bot:
        return
    # Don't nudge dev/owner accounts — they're testing
    if db.is_dev(str(ctx.author.id), owner_id=OWNER_ID):
        return

    uid    = str(ctx.author.id)
    bot_id = getattr(db, '_bot_id', 0)

    _cmd_counts[uid] = _cmd_counts.get(uid, 0) + 1
    count = _cmd_counts[uid]

    if count % VOTE_NUDGE_EVERY == 0:
        link = f"https://top.gg/bot/{bot_id}/vote"
        nudges = [
            f"🗳️ Hey {ctx.author.mention}, you've used **{count} commands**! "
            f"If you're enjoying StatSnitch, a vote helps a lot → {link}",
            f"🗳️ {ctx.author.mention} — **{count} commands** and counting. "
            f"We're not asking for money, just a vote → {link}",
            f"🗳️ {count} commands deep, {ctx.author.mention}. "
            f"You clearly live here. Vote rent → {link}",
            f"🗳️ **{count} commands**, {ctx.author.mention}. "
            f"The bot is watching. It would appreciate a vote → {link}",
        ]
        import random
        await ctx.send(random.choice(nudges), delete_after=30)


# ---------------------------------------------------------------------------
# Edit / delete tracking
# ---------------------------------------------------------------------------
@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or before.content == after.content:
        return
    db.track_edit(str(before.author.id), before.author.display_name)
    await db.maybe_flush()


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    db.track_delete(str(message.author.id), message.author.display_name)
    await db.maybe_flush()


# ---------------------------------------------------------------------------
# Reaction tracking
# ---------------------------------------------------------------------------
@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User | discord.Member):
    if user.bot:
        return
    msg = reaction.message
    if msg.author.bot:
        return

    emoji = str(reaction.emoji)
    db.track_reaction_add(
        reactor_id=str(user.id), reactor_name=user.display_name,
        author_id=str(msg.author.id), author_name=msg.author.display_name,
        emoji=emoji,
    )
    await db.maybe_flush()


# ---------------------------------------------------------------------------
# Voice state tracking
# ---------------------------------------------------------------------------
@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    uid, uname = str(member.id), member.display_name

    if before.channel is None and after.channel is not None:
        # Joined voice
        db.voice_join(uid, uname)
    elif before.channel is not None and after.channel is None:
        # Left voice
        db.voice_leave(uid, uname)
        await db.maybe_flush()


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. Try `{BOT_PREFIX}statshelp`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Couldn't find that user. Do they even talk?")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Silently ignore unknown commands
    else:
        logger.error("Command error: %s", error, exc_info=error)
        await ctx.send("💀 Something broke. I blame the user.")


# ---------------------------------------------------------------------------
# Graceful shutdown: force-save before exit
# ---------------------------------------------------------------------------
async def shutdown():
    logger.info("Shutting down — force saving all data...")
    await db.save()
    logger.info("Data saved. Goodbye.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    try:
        async with bot:
            await bot.start(TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 StatSnitch shutting down. Your secrets are safe. For now.")