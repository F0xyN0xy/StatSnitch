"""
owner.py — Owner-only commands (prefix: ,) and developer/tester account management.

OWNER COMMANDS  (only usable by the user ID set in OWNER_ID env var)
──────────────────────────────────────────────────────────────────────
,unfreezeall            — Remove stats freeze from every currently frozen user
,unfreeze @user         — Remove stats freeze from a specific user
,untimeout @user        — Remove Discord timeout from a specific user
,clearpenalties @user   — Remove freeze + strikes + Discord timeout at once
,adddev @user           — Add a user to developer/tester mode
,removedev @user        — Remove a user from developer/tester mode
,devlist                — Show all current dev accounts
,botinfo                — Quick diagnostics (uptime, users tracked, queue depths)

DEVELOPER/TESTER MODE
──────────────────────────────────────────────────────────────────────
Dev accounts are exempt from:
  - Spam detection (no penalties, no timeouts, no freeze)
  - Stats tracking (messages / words / reactions NOT counted)

They still see all bot responses normally — the bot reacts to their
commands as usual, nothing looks different from their side.
This lets you test spam thresholds, command output, and penalty
messages without polluting real stats or accidentally timing yourself out.

Dev IDs are stored in storage so they persist across restarts.
The OWNER_ID is always implicitly a dev account.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

logger = logging.getLogger("statsnitch.owner")


class OwnerCog(commands.Cog, name="Owner"):
    """
    Owner-only commands. Uses a separate ',' prefix so they never
    conflict with regular '.' commands.
    Only the user whose ID matches OWNER_ID in .env can run these.
    """

    def __init__(self, bot: commands.Bot, storage, penalty_manager,
                 owner_id: int):
        self.bot     = bot
        self.db      = storage
        self.pm      = penalty_manager
        self.owner_id = owner_id
        self._start_time = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Auth check
    # ------------------------------------------------------------------

    def _is_owner(self, ctx: commands.Context) -> bool:
        return ctx.author.id == self.owner_id

    async def _owner_only(self, ctx: commands.Context) -> bool:
        """Send an error and return False if not owner."""
        if not self._is_owner(ctx):
            await ctx.message.add_reaction("🚫")
            await ctx.send(
                "⛔ This command is owner-only. Nice try though.",
                delete_after=5,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # ,unfreeze @user
    # ------------------------------------------------------------------

    @commands.command(name="unfreeze")
    async def unfreeze(self, ctx: commands.Context,
                       member: discord.Member | None = None):
        """❄️ Remove stats freeze from a user (or all frozen users)."""
        if not await self._owner_only(ctx):
            return

        if member is None:
            await ctx.send("Usage: `;unfreeze @user` or `;unfreezeall`")
            return

        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)
        was_frozen = self.db.is_frozen(uid)
        u["stats_frozen_until"] = None
        await self.db.save()

        if was_frozen:
            await ctx.send(
                f"✅ Removed stats freeze from **{member.display_name}**. "
                f"Back on the leaderboard they go."
            )
        else:
            await ctx.send(
                f"ℹ️ **{member.display_name}** wasn't frozen. Nothing to do."
            )

    # ------------------------------------------------------------------
    # ,unfreezeall
    # ------------------------------------------------------------------

    @commands.command(name="unfreezeall")
    async def unfreezeall(self, ctx: commands.Context):
        """❄️ Remove stats freeze from every currently frozen user."""
        if not await self._owner_only(ctx):
            return

        unfrozen = []
        for u in self.db.all_users():
            if self.db.is_frozen(u["user_id"]):
                u["stats_frozen_until"] = None
                unfrozen.append(u["username"])

        if not unfrozen:
            await ctx.send("ℹ️ Nobody is currently frozen.")
            return

        await self.db.save()
        names = ", ".join(f"**{n}**" for n in unfrozen)
        await ctx.send(
            f"✅ Removed freeze from {len(unfrozen)} user(s): {names}"
        )

    # ------------------------------------------------------------------
    # ,untimeout @user
    # ------------------------------------------------------------------

    @commands.command(name="untimeout")
    async def untimeout(self, ctx: commands.Context, member: discord.Member):
        """🔓 Remove Discord timeout from a user immediately."""
        if not await self._owner_only(ctx):
            return

        if not isinstance(member, discord.Member):
            await ctx.send("⚠️ Can't find that member in this server.")
            return

        if member.timed_out_until is None:
            await ctx.send(
                f"ℹ️ **{member.display_name}** isn't timed out. Already free."
            )
            return

        try:
            await member.timeout(None, reason=f"Removed by owner {ctx.author}")
            await ctx.send(
                f"✅ Discord timeout removed from **{member.display_name}**."
            )
            logger.info("Owner %s removed timeout from %s", ctx.author, member)
        except discord.Forbidden:
            await ctx.send(
                "⛔ I don't have permission to remove that timeout. "
                "Check my role is above theirs."
            )
        except discord.HTTPException as e:
            await ctx.send(f"❌ Discord API error: {e}")

    # ------------------------------------------------------------------
    # ,clearpenalties @user
    # ------------------------------------------------------------------

    @commands.command(name="clearpenalties")
    async def clearpenalties(self, ctx: commands.Context, member: discord.Member):
        """🧹 Clear freeze + strikes + Discord timeout in one shot."""
        if not await self._owner_only(ctx):
            return

        uid = str(member.id)
        u   = self.db.get_user(uid, member.display_name)

        # Clear storage record
        u["spam_strikes"]        = 0
        u["stats_frozen_until"]  = None
        u["last_spam_reason"]    = None
        u["last_spam_timestamp"] = None

        # Clear in-memory penalty queue for this user
        q = self.pm._queues.get(uid)
        if q:
            while not q.empty():
                try:
                    q.get_nowait()
                    q.task_done()
                except Exception:
                    pass

        # Clear Discord timeout
        timeout_removed = False
        if isinstance(member, discord.Member) and member.timed_out_until:
            try:
                await member.timeout(None, reason=f"Cleared by owner {ctx.author}")
                timeout_removed = True
            except discord.Forbidden:
                pass

        await self.db.save()

        parts = ["spam strikes", "stats freeze"]
        if timeout_removed:
            parts.append("Discord timeout")
        await ctx.send(
            f"✅ Cleared {', '.join(parts)} for **{member.display_name}**. "
            f"Clean slate. Don't waste it."
        )

    # ------------------------------------------------------------------
    # ,adddev @user
    # ------------------------------------------------------------------

    @commands.command(name="adddev")
    async def adddev(self, ctx: commands.Context, member: discord.Member):
        """🛠️ Add a user to developer/tester mode (exempt from spam + stats)."""
        if not await self._owner_only(ctx):
            return

        uid = str(member.id)
        devs: list = self.db._meta.setdefault("dev_ids", [])

        if uid in devs:
            await ctx.send(
                f"ℹ️ **{member.display_name}** is already a dev account."
            )
            return

        devs.append(uid)
        await self.db.save()
        logger.info("Dev account added: %s (%s)", member.display_name, uid)
        await ctx.send(
            f"🛠️ **{member.display_name}** added as dev/tester.\n"
            f"Their messages won't affect stats or trigger spam penalties.\n"
            f"They won't notice anything different."
        )

    # ------------------------------------------------------------------
    # ,removedev @user
    # ------------------------------------------------------------------

    @commands.command(name="removedev")
    async def removedev(self, ctx: commands.Context, member: discord.Member):
        """🛠️ Remove a user from developer/tester mode."""
        if not await self._owner_only(ctx):
            return

        uid  = str(member.id)
        devs: list = self.db._meta.setdefault("dev_ids", [])

        if uid not in devs:
            await ctx.send(
                f"ℹ️ **{member.display_name}** isn't a dev account."
            )
            return

        devs.remove(uid)
        await self.db.save()
        logger.info("Dev account removed: %s (%s)", member.display_name, uid)
        await ctx.send(
            f"✅ **{member.display_name}** removed from dev mode. "
            f"They're a regular user again. Stats resume from now."
        )

    # ------------------------------------------------------------------
    # ,devlist
    # ------------------------------------------------------------------

    @commands.command(name="devlist")
    async def devlist(self, ctx: commands.Context):
        """📋 Show all current dev/tester accounts."""
        if not await self._owner_only(ctx):
            return

        devs: list = self.db._meta.get("dev_ids", [])

        embed = discord.Embed(
            title="🛠️ Developer / Tester Accounts",
            color=discord.Color.dark_gold(),
        )

        lines = [f"👑 <@{self.owner_id}> *(owner — always exempt)*"]
        for uid in devs:
            if uid != str(self.owner_id):
                lines.append(f"🛠️ <@{uid}>")

        embed.description = "\n".join(lines) if lines else "No dev accounts set."
        embed.set_footer(text="Dev accounts are invisible to spam detection and stats.")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # ,botinfo
    # ------------------------------------------------------------------

    @commands.command(name="botinfo")
    async def botinfo(self, ctx: commands.Context):
        """📊 Quick diagnostics: uptime, users tracked, queue depths."""
        if not await self._owner_only(ctx):
            return

        uptime = datetime.now(timezone.utc) - self._start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        mins        = rem // 60

        total_users  = len(self.db.all_users())
        frozen_count = sum(1 for u in self.db.all_users() if self.db.is_frozen(u["user_id"]))
        dev_count    = len(self.db._meta.get("dev_ids", []))
        queue_total  = sum(q.qsize() for q in self.pm._queues.values())
        dirty        = self.db._dirty_count

        embed = discord.Embed(title="🤖 StatSnitch Diagnostics", color=discord.Color.blurple())
        embed.add_field(name="⏱️ Uptime",          value=f"{hours}h {mins}m",   inline=True)
        embed.add_field(name="👥 Users tracked",   value=str(total_users),       inline=True)
        embed.add_field(name="❄️ Frozen users",    value=str(frozen_count),      inline=True)
        embed.add_field(name="🛠️ Dev accounts",    value=str(dev_count),         inline=True)
        embed.add_field(name="📬 Penalty queue",   value=str(queue_total),       inline=True)
        embed.add_field(name="💾 Unsaved changes", value=str(dirty),             inline=True)
        embed.add_field(name="🤖 Bot ID",          value=str(getattr(self.db, '_bot_id', 'N/A')), inline=True)
        await ctx.send(embed=embed)