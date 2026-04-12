"""
commands.py — All StatSnitch slash/prefix commands as a discord.py Cog.
"""

import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from personality import (
    roast_from_stats,
    compliment_from_stats,
    fortune_from_stats,
    duel_verdict,
    compatibility_verdict,
    _top_key,
)
from storage import Storage


def _chaos_score(u: dict) -> float:
    return (
        u["total_messages"]  * 0.3
        + u["reactions_given"] * 0.2
        + u["messages_edited"] * 0.5
        + u["messages_deleted"] * 1.0
    )


def _wasted_minutes(u: dict) -> float:
    return (u["total_messages"] * 15 / 60) + u["voice_minutes"]


def _ordinal(n: int) -> str:
    suffix = ["th", "st", "nd", "rd"] + ["th"] * 16
    return f"{n}{suffix[n % 20] if n % 20 < 20 else 'th'}"


def _top_n(d: dict, n: int = 5) -> list[tuple]:
    return sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]


class StatCog(commands.Cog, name="StatSnitch"):
    """All tracking and commands for the StatSnitch bot."""

    def __init__(self, bot: commands.Bot, storage: Storage):
        self.bot = bot
        self.db  = storage

    # ------------------------------------------------------------------
    # Helper: find user in storage by display mention
    # ------------------------------------------------------------------

    def _resolve_member(self, ctx: commands.Context, target: discord.Member | None):
        """Return (user_id_str, username) for a member or the author."""
        m = target or ctx.author
        return str(m.id), m.display_name

    def _get_rank(self, user_id: str, key: str = "total_messages") -> int:
        all_u = sorted(self.db.all_users(), key=lambda u: u.get(key, 0), reverse=True)
        for i, u in enumerate(all_u, 1):
            if u["user_id"] == str(user_id):
                return i
        return -1

    # ==================================================================
    # ██  BASIC STATS
    # ==================================================================

    @commands.command(name="mystats")
    async def mystats(self, ctx: commands.Context):
        """📊 Your personal stats dashboard — with roast included."""
        uid, uname = self._resolve_member(ctx, None)
        u = self.db.get_user(uid, uname)

        rank      = self._get_rank(uid)
        top_word  = _top_key(u["words"])
        top_emoji = _top_key(u["emoji_usage"])
        peak_hour = _top_key(u["hourly_activity"])
        wasted_h  = _wasted_minutes(u) / 60

        embed = discord.Embed(
            title=f"📊 {uname}'s Stats",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="📨 Messages",
            value=f"{u['total_messages']:,} ({_ordinal(rank)} in server)",
            inline=True,
        )
        embed.add_field(
            name="🔥 Top word",
            value=f"\"{top_word}\" ({u['words'].get(top_word, 0):,}×)" if top_word else "None yet",
            inline=True,
        )
        embed.add_field(
            name="❤️ Reactions",
            value=f"{u['reactions_given']:,} given | {u['reactions_received']:,} received",
            inline=False,
        )
        embed.add_field(
            name="⏰ Peak hour",
            value=f"{peak_hour}:00" if peak_hour else "N/A",
            inline=True,
        )
        embed.add_field(
            name="📅 Streak",
            value=f"{u['current_streak']} days 🔥 (best: {u['longest_streak']})",
            inline=True,
        )
        embed.add_field(
            name="⏱️ Time wasted",
            value=f"~{wasted_h:.1f} hours",
            inline=True,
        )
        embed.add_field(
            name="🗑️ Edits / Deletes",
            value=f"{u['messages_edited']:,} / {u['messages_deleted']:,}",
            inline=True,
        )
        embed.add_field(
            name="🎭 Fav emoji",
            value=top_emoji or "None",
            inline=True,
        )
        embed.add_field(
            name="🔗 Links sent",
            value=f"{u['links_sent']:,}",
            inline=True,
        )
        embed.add_field(
            name="💀 Roast",
            value=roast_from_stats(u),
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.command(name="topusers")
    async def topusers(self, ctx: commands.Context):
        """🏆 Leaderboard of the most active users."""
        titles = [
            "👑 The Overlord",
            "🔥 The Menace",
            "💬 The Chatterbox",
            "📡 The Broadcaster",
            "🐦 The Songbird",
            "🗣️ The Loudmouth",
            "📢 The Town Crier",
            "🤫 Still Trying",
            "👶 Baby Steps",
            "🌱 The Lurker",
        ]
        users = sorted(self.db.all_users(), key=lambda u: u["total_messages"], reverse=True)[:10]
        embed = discord.Embed(title="🏆 Top Users Leaderboard", color=discord.Color.gold())
        lines = []
        for i, u in enumerate(users):
            title = titles[i] if i < len(titles) else "📊 Ranked"
            lines.append(f"`{i+1}.` {title} — **{u['username']}** · {u['total_messages']:,} msgs")
        embed.description = "\n".join(lines) if lines else "No data yet. Say something!"
        await ctx.send(embed=embed)

    @commands.command(name="wordstats")
    async def wordstats(self, ctx: commands.Context, word: str, member: discord.Member | None = None):
        """🔍 How many times was a word used?"""
        uid, uname = self._resolve_member(ctx, member)
        u     = self.db.get_user(uid, uname)
        count = u["words"].get(word.lower(), 0)
        if count == 0:
            comment = "0 times. Good. That word is cringe anyway."
        elif count < 10:
            comment = f"{count} times. Just getting started with the obsession."
        elif count < 50:
            comment = f"{count} times. You're warming up."
        else:
            comment = f"{count:,} times. You really love that word, huh? Seek help."
        await ctx.send(f"📖 **{uname}** said **\"{word}\"** → {comment}")

    @commands.command(name="serverstats")
    async def serverstats(self, ctx: commands.Context):
        """🌐 Overall server statistics."""
        users = self.db.all_users()
        if not users:
            await ctx.send("Server empty? Or am I broken?")
            return

        total_msgs = sum(u["total_messages"] for u in users)
        total_reax = sum(u["reactions_received"] for u in users)

        # Aggregate hourly activity
        hour_totals: dict[str, int] = {}
        day_totals:  dict[str, int] = {}
        for u in users:
            for h, c in u["hourly_activity"].items():
                hour_totals[h] = hour_totals.get(h, 0) + c
            for d, c in u["daily_activity"].items():
                day_totals[d]  = day_totals.get(d, 0) + c

        peak_hour = _top_key(hour_totals)
        days_map  = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        peak_day  = days_map.get(int(_top_key(day_totals) or 0), "N/A") if day_totals else "N/A"

        most_active = max(users, key=lambda u: u["total_messages"])

        embed = discord.Embed(title="🌐 Server Stats", color=discord.Color.green())
        embed.add_field(name="📨 Total messages",   value=f"{total_msgs:,}",             inline=True)
        embed.add_field(name="❤️ Total reactions",  value=f"{total_reax:,}",             inline=True)
        embed.add_field(name="👥 Tracked users",    value=f"{len(users):,}",             inline=True)
        embed.add_field(name="⏰ Peak hour",         value=f"{peak_hour}:00" if peak_hour else "N/A", inline=True)
        embed.add_field(name="📅 Peak day",          value=peak_day,                     inline=True)
        embed.add_field(name="🏆 Most active",       value=most_active["username"],       inline=True)
        await ctx.send(embed=embed)

    # ==================================================================
    # ██  FUN COMPETITION
    # ==================================================================

    @commands.command(name="chaos")
    async def chaos(self, ctx: commands.Context):
        """💀 Chaos score leaderboard."""
        users = sorted(self.db.all_users(), key=_chaos_score, reverse=True)[:10]
        embed = discord.Embed(title="💀 Chaos Leaderboard", color=discord.Color.red())
        lines = []
        for i, u in enumerate(users, 1):
            score = _chaos_score(u)
            lines.append(f"`{i}.` **{u['username']}** — {score:,.1f} chaos points")
        embed.description = "\n".join(lines) or "Surprisingly calm server. For now."
        embed.set_footer(text="Formula: msgs×0.3 + reactions×0.2 + edits×0.5 + deletes×1.0")
        await ctx.send(embed=embed)

    @commands.command(name="streaks")
    async def streaks(self, ctx: commands.Context):
        """🔥 Daily message streak leaderboard."""
        users = sorted(self.db.all_users(), key=lambda u: u["current_streak"], reverse=True)[:10]
        embed = discord.Embed(title="🔥 Streak Leaderboard", color=discord.Color.orange())
        lines = []
        for i, u in enumerate(users, 1):
            lines.append(
                f"`{i}.` **{u['username']}** — {u['current_streak']} days "
                f"(best ever: {u['longest_streak']})"
            )
        embed.description = "\n".join(lines) or "Nobody has a streak. Disappointing."
        await ctx.send(embed=embed)

    @commands.command(name="nightowls")
    async def nightowls(self, ctx: commands.Context):
        """🌙 Most active between 12am–5am. Touch grass?"""
        night_hours = {str(h) for h in range(0, 5)}
        def night_score(u):
            return sum(v for h, v in u["hourly_activity"].items() if h in night_hours)

        users = sorted(self.db.all_users(), key=night_score, reverse=True)[:8]
        embed = discord.Embed(title="🌙 Night Owls (12am–5am)", color=discord.Color.dark_blue())
        lines = []
        for i, u in enumerate(users, 1):
            score = night_score(u)
            if score > 0:
                lines.append(f"`{i}.` **{u['username']}** — {score:,} late-night messages 🦉")
        embed.description = "\n".join(lines) or "Everyone sleeps here? Boring."
        embed.set_footer(text="Touch grass when?")
        await ctx.send(embed=embed)

    @commands.command(name="earlybirds")
    async def earlybirds(self, ctx: commands.Context):
        """🌅 Most active between 5am–8am. Who hurt you?"""
        early_hours = {str(h) for h in range(5, 8)}
        def early_score(u):
            return sum(v for h, v in u["hourly_activity"].items() if h in early_hours)

        users = sorted(self.db.all_users(), key=early_score, reverse=True)[:8]
        embed = discord.Embed(title="🌅 Early Birds (5am–8am)", color=discord.Color.yellow())
        lines = []
        for i, u in enumerate(users, 1):
            score = early_score(u)
            if score > 0:
                lines.append(f"`{i}.` **{u['username']}** — {score:,} morning messages 🐓")
        embed.description = "\n".join(lines) or "Nobody's up early. Healthy crowd."
        embed.set_footer(text="Who hurt you?")
        await ctx.send(embed=embed)

    @commands.command(name="captaincaps")
    async def captaincaps(self, ctx: commands.Context):
        """🔊 Who screams the most in CAPS?"""
        users = sorted(self.db.all_users(), key=lambda u: u["caps_messages"], reverse=True)[:8]
        embed = discord.Embed(title="🔊 CAPS LOCK Hall of Shame", color=discord.Color.red())
        lines = []
        for i, u in enumerate(users, 1):
            if u["caps_messages"] > 0:
                lines.append(f"`{i}.` **{u['username']}** — {u['caps_messages']:,} SCREAMING messages")
        embed.description = "\n".join(lines) or "Civilized bunch. Scary."
        embed.set_footer(text="Calm down!")
        await ctx.send(embed=embed)

    @commands.command(name="questionqueen")
    async def questionqueen(self, ctx: commands.Context):
        """❓ Who asks the most questions?"""
        users = sorted(self.db.all_users(), key=lambda u: u["question_marks"], reverse=True)[:8]
        embed = discord.Embed(title="❓ Question Mark Royalty", color=discord.Color.purple())
        lines = []
        for i, u in enumerate(users, 1):
            if u["question_marks"] > 0:
                lines.append(f"`{i}.` **{u['username']}** — {u['question_marks']:,} question marks")
        embed.description = "\n".join(lines) or "Nobody's curious here. Bliss."
        embed.set_footer(text="Some questions are better left unasked.")
        await ctx.send(embed=embed)

    # ==================================================================
    # ██  DUEL & COMPARE
    # ==================================================================

    @commands.command(name="duel")
    async def duel(self, ctx: commands.Context, opponent: discord.Member):
        """⚔️ Battle your stats against another user."""
        a_id, a_name = str(ctx.author.id), ctx.author.display_name
        b_id, b_name = str(opponent.id), opponent.display_name

        if a_id == b_id:
            await ctx.send("You can't duel yourself. That's just exercise.")
            return

        a = self.db.get_user(a_id, a_name)
        b = self.db.get_user(b_id, b_name)

        def _cmp(label, va, vb, unit=""):
            if va > vb:
                diff = va - vb
                return f"{label}: **{va:,}{unit}** vs {vb:,}{unit} — {a_name} wins (+{diff:,})"
            elif vb > va:
                diff = vb - va
                return f"{label}: {va:,}{unit} vs **{vb:,}{unit}** — {b_name} wins (+{diff:,})"
            else:
                return f"{label}: {va:,}{unit} vs {vb:,}{unit} — 🤝 Tie"

        ca, cb = _chaos_score(a), _chaos_score(b)
        winner = a_name if ca >= cb else b_name
        loser  = b_name if ca >= cb else a_name
        margin = abs(ca - cb) / max(cb, 1) * 100

        embed = discord.Embed(
            title=f"⚔️ DUEL: {a_name} vs {b_name}",
            color=discord.Color.red(),
        )
        embed.add_field(name="📨 Messages",  value=_cmp("Messages",  a["total_messages"],   b["total_messages"]),  inline=False)
        embed.add_field(name="❤️ Reactions", value=_cmp("Reactions recv", a["reactions_received"], b["reactions_received"]), inline=False)
        embed.add_field(name="🔥 Streak",    value=_cmp("Streak",    a["current_streak"],   b["current_streak"], "d"), inline=False)
        embed.add_field(name="💀 Chaos",     value=_cmp("Chaos",     int(ca),               int(cb)),             inline=False)
        embed.add_field(name="🏆 Verdict",   value=duel_verdict(winner, loser, margin),     inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="compatibility")
    async def compatibility(self, ctx: commands.Context, other: discord.Member):
        """💕 How much do two users interact?"""
        a_id, a_name = str(ctx.author.id), ctx.author.display_name
        b_id, b_name = str(other.id), other.display_name

        a = self.db.get_user(a_id, a_name)
        b = self.db.get_user(b_id, b_name)

        # mentions_given is total; cross-mentions aren't directly tracked
        # We approximate by noting mentions_given for each user
        # (Real cross-tracking would need per-target counters — out of scope)
        men_a = a["mentions_given"]
        men_b = b["mentions_given"]

        peak_a = _top_key(a["hourly_activity"])
        peak_b = _top_key(b["hourly_activity"])
        shared_peak = peak_a is not None and peak_a == peak_b

        # Common top words
        common = set(list(_top_n(a["words"], 20))) & set(list(_top_n(b["words"], 20)))
        common_display = ", ".join(f'"{w}"' for w, _ in list(_top_n(a["words"], 20))[:3]
                                   if w in dict(_top_n(b["words"], 20))) or "none"

        embed = discord.Embed(
            title=f"💕 Compatibility: {a_name} & {b_name}",
            color=discord.Color.pink() if hasattr(discord.Color, "pink") else discord.Color.magenta(),
        )
        embed.add_field(name="📣 Mentions",   value=f"{a_name} has mentioned {men_a:,} people · {b_name} has mentioned {men_b:,}", inline=False)
        embed.add_field(name="⏰ Peak hour",  value=f"{a_name}: {peak_a}:00 | {b_name}: {peak_b}:00" if peak_a else "No data", inline=False)
        embed.add_field(name="💬 Common top words", value=common_display, inline=False)
        embed.add_field(name="🎯 Verdict",    value=compatibility_verdict(men_a, men_b, shared_peak), inline=False)
        await ctx.send(embed=embed)

    # ==================================================================
    # ██  REACTIONS & ENGAGEMENT
    # ==================================================================

    @commands.command(name="reactionking")
    async def reactionking(self, ctx: commands.Context):
        """❤️ Who receives the most reactions?"""
        users = sorted(self.db.all_users(), key=lambda u: u["reactions_received"], reverse=True)[:8]
        embed = discord.Embed(title="❤️ Reaction Kings/Queens", color=discord.Color.magenta())
        lines = [
            f"`{i}.` **{u['username']}** — {u['reactions_received']:,} reactions received"
            for i, u in enumerate(users, 1) if u["reactions_received"] > 0
        ]
        embed.description = "\n".join(lines) or "No reactions? Sad server."
        embed.set_footer(text="Most loved or most meme'd?")
        await ctx.send(embed=embed)

    @commands.command(name="reactiongiver")
    async def reactiongiver(self, ctx: commands.Context):
        """💊 Who hands out the most reactions?"""
        users = sorted(self.db.all_users(), key=lambda u: u["reactions_given"], reverse=True)[:8]
        embed = discord.Embed(title="💊 Reaction Givers (Validation Addicts)", color=discord.Color.teal())
        lines = [
            f"`{i}.` **{u['username']}** — {u['reactions_given']:,} reactions given"
            for i, u in enumerate(users, 1) if u["reactions_given"] > 0
        ]
        embed.description = "\n".join(lines) or "Stingy bunch."
        embed.set_footer(text="Validation addict 💊")
        await ctx.send(embed=embed)

    @commands.command(name="emojistats")
    async def emojistats(self, ctx: commands.Context, emoji: str | None = None):
        """🎭 Who uses a specific emoji the most?"""
        if emoji:
            users_with = [
                (u["username"], u["emoji_usage"].get(emoji, 0))
                for u in self.db.all_users()
            ]
            users_with = sorted(users_with, key=lambda x: x[1], reverse=True)[:5]
            top_name, top_count = users_with[0] if users_with else ("No one", 0)
            if top_count == 0:
                await ctx.send(f"Nobody uses {emoji} here. It lives a lonely life.")
                return
            lines = [f"`{i}.` **{n}** — {c:,}×" for i, (n, c) in enumerate(users_with, 1) if c > 0]
            embed = discord.Embed(title=f"🎭 {emoji} Leaderboard", color=discord.Color.blurple())
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"{emoji} champion: {top_name} with {top_count:,}. None were genuine.")
            await ctx.send(embed=embed)
        else:
            # Server-wide top emojis
            totals: dict[str, int] = {}
            for u in self.db.all_users():
                for e, c in u["emoji_usage"].items():
                    totals[e] = totals.get(e, 0) + c
            top = _top_n(totals, 10)
            embed = discord.Embed(title="🎭 Server Emoji Leaderboard", color=discord.Color.blurple())
            lines = [f"`{i}.` {e} — {c:,}×" for i, (e, c) in enumerate(top, 1)]
            embed.description = "\n".join(lines) or "No emoji data yet."
            await ctx.send(embed=embed)

    # ==================================================================
    # ██  TIME-BASED
    # ==================================================================

    @commands.command(name="wasted")
    async def wasted(self, ctx: commands.Context, member: discord.Member | None = None):
        """⏱️ How much time have you wasted here?"""
        uid, uname = self._resolve_member(ctx, member)
        u = self.db.get_user(uid, uname)

        typing_mins = u["total_messages"] * 15 / 60
        voice_mins  = u["voice_minutes"]
        total_mins  = typing_mins + voice_mins
        total_hrs   = total_mins / 60

        embed = discord.Embed(title=f"⏱️ {uname}'s Time Wasted", color=discord.Color.greyple())
        embed.add_field(name="⌨️ Typing time",   value=f"~{typing_mins/60:.1f} hrs",  inline=True)
        embed.add_field(name="🎤 Voice time",    value=f"~{voice_mins/60:.1f} hrs",   inline=True)
        embed.add_field(name="💀 TOTAL",         value=f"~{total_hrs:.1f} hours",     inline=False)
        if total_hrs > 40:
            comment = f"That's a full work week. Consider a career change."
        elif total_hrs > 10:
            comment = f"~{total_hrs:.0f} hours = {total_hrs/24:.1f} days of nonsense. Could have learned a language."
        else:
            comment = "Still early. The addiction is just beginning."
        embed.set_footer(text=comment)
        await ctx.send(embed=embed)

    @commands.command(name="timecapsule")
    async def timecapsule(self, ctx: commands.Context):
        """📦 A look at your stats from a year ago (data permitting)."""
        uid, uname = self._resolve_member(ctx, None)
        u = self.db.get_user(uid, uname)

        first = u.get("first_message_date", "")
        try:
            first_dt = datetime.fromisoformat(first)
            now      = datetime.now(timezone.utc)
            age_days = (now - first_dt).days
        except (ValueError, TypeError):
            age_days = 0

        if age_days < 365:
            await ctx.send(
                f"📦 No time capsule yet, {uname}. "
                f"You've only been here {age_days} days. Come back in {365 - age_days} days."
            )
            return

        top_word = _top_key(u["words"])
        await ctx.send(
            f"📦 **{uname}'s Time Capsule:**\n"
            f"You've been here **{age_days} days** and sent **{u['total_messages']:,} messages**.\n"
            f"Your top word is **\"{top_word}\"** ({u['words'].get(top_word,0):,}×).\n"
            f"You've reacted **{u['reactions_given']:,}** times and received **{u['reactions_received']:,}**.\n"
            f"💀 Roast: One year later and the chaos continues. We wouldn't have it any other way."
        )

    # ==================================================================
    # ██  FUN RANDOM
    # ==================================================================

    @commands.command(name="roastme")
    async def roastme(self, ctx: commands.Context):
        """🔥 Get roasted based on your actual stats."""
        uid, uname = self._resolve_member(ctx, None)
        u = self.db.get_user(uid, uname)
        await ctx.send(f"🔥 **{uname}:** {roast_from_stats(u)}")

    @commands.command(name="complimentme")
    async def complimentme(self, ctx: commands.Context):
        """🌸 Get a wholesome compliment based on your stats."""
        uid, uname = self._resolve_member(ctx, None)
        u = self.db.get_user(uid, uname)
        await ctx.send(f"🌸 **{uname}:** {compliment_from_stats(u)}")

    @commands.command(name="fortune")
    async def fortune(self, ctx: commands.Context):
        """🔮 Receive a fake fortune based on your habits."""
        uid, uname = self._resolve_member(ctx, None)
        u = self.db.get_user(uid, uname)
        await ctx.send(f"🔮 **{uname}'s Fortune:** {fortune_from_stats(u)}")

    # ==================================================================
    # ██  LEADERBOARD VARIANTS
    # ==================================================================

    @commands.command(name="topwords")
    async def topwords(self, ctx: commands.Context, member: discord.Member | None = None):
        """📖 Top words used by a user (or the whole server)."""
        if member:
            uid, uname = str(member.id), member.display_name
            u = self.db.get_user(uid, uname)
            words = _top_n(u["words"], 10)
            title = f"📖 {uname}'s Top Words"
        else:
            combined: dict[str, int] = {}
            for u in self.db.all_users():
                for w, c in u["words"].items():
                    combined[w] = combined.get(w, 0) + c
            words = _top_n(combined, 10)
            title = "📖 Server Top Words"

        embed = discord.Embed(title=title, color=discord.Color.blurple())
        lines = [f"`{i}.` **{w}** — {c:,}×" for i, (w, c) in enumerate(words, 1)]
        embed.description = "\n".join(lines) or "No word data yet."
        await ctx.send(embed=embed)

    @commands.command(name="topemoji")
    async def topemoji(self, ctx: commands.Context):
        """🎭 Most used emojis server-wide."""
        totals: dict[str, int] = {}
        for u in self.db.all_users():
            for e, c in u["emoji_usage"].items():
                totals[e] = totals.get(e, 0) + c
        top = _top_n(totals, 10)
        embed = discord.Embed(title="🎭 Top Server Emojis", color=discord.Color.blurple())
        lines = [f"`{i}.` {e} — {c:,}×" for i, (e, c) in enumerate(top, 1)]
        embed.description = "\n".join(lines) or "No emoji data yet."
        await ctx.send(embed=embed)

    @commands.command(name="toplinks")
    async def toplinks(self, ctx: commands.Context):
        """🔗 Who shares the most links?"""
        users = sorted(self.db.all_users(), key=lambda u: u["links_sent"], reverse=True)[:8]
        embed = discord.Embed(title="🔗 Link Enthusiasts", color=discord.Color.blue())
        lines = [
            f"`{i}.` **{u['username']}** — {u['links_sent']:,} links"
            for i, u in enumerate(users, 1) if u["links_sent"] > 0
        ]
        embed.description = "\n".join(lines) or "No link sharing. Mysterious."
        embed.set_footer(text="Internet explorer 🌐")
        await ctx.send(embed=embed)

    @commands.command(name="topattachments")
    async def topattachments(self, ctx: commands.Context):
        """📎 Who sends the most files/images?"""
        users = sorted(self.db.all_users(), key=lambda u: u["attachments_sent"], reverse=True)[:8]
        embed = discord.Embed(title="📎 Gallery Dump Champions", color=discord.Color.green())
        lines = [
            f"`{i}.` **{u['username']}** — {u['attachments_sent']:,} attachments"
            for i, u in enumerate(users, 1) if u["attachments_sent"] > 0
        ]
        embed.description = "\n".join(lines) or "No attachments sent. Clean server."
        embed.set_footer(text="Gallery dump 📸")
        await ctx.send(embed=embed)

    @commands.command(name="topedits")
    async def topedits(self, ctx: commands.Context):
        """✏️ Who edits their messages the most?"""
        users = sorted(self.db.all_users(), key=lambda u: u["messages_edited"], reverse=True)[:8]
        embed = discord.Embed(title="✏️ Chronic Editors", color=discord.Color.orange())
        lines = [
            f"`{i}.` **{u['username']}** — {u['messages_edited']:,} edits"
            for i, u in enumerate(users, 1) if u["messages_edited"] > 0
        ]
        embed.description = "\n".join(lines) or "Everyone commits to their words. Respect."
        embed.set_footer(text="Commit to something! ✏️")
        await ctx.send(embed=embed)

    # ==================================================================
    # ██  VOTE
    # ==================================================================

    @commands.command(name="vote")
    async def vote(self, ctx: commands.Context):
        """🗳️ Vote for StatSnitch on top.gg (and get a freeze pardon if frozen)."""
        from spam import _freeze_remaining_str

        uid    = str(ctx.author.id)
        u      = self.db.get_user(uid, ctx.author.display_name)
        bot_id = getattr(self.db, '_bot_id', 0)
        link   = f"https://top.gg/bot/{bot_id}/vote"

        freeze_str = _freeze_remaining_str(u.get("stats_frozen_until"))
        is_frozen  = self.db.is_frozen(uid)

        embed = discord.Embed(
            title="🗳️ Vote for StatSnitch on top.gg!",
            description=f"**[👉 Click here to vote]({link})**",
            color=discord.Color.red() if is_frozen else discord.Color.blurple(),
            url=link,
        )

        if is_frozen:
            embed.add_field(
                name="❄️ You're currently frozen",
                value=(
                    f"{freeze_str}\n"
                    f"Vote now and your freeze time is **cut in half automatically**. "
                    f"No command needed — just vote and wait a few minutes."
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Why vote?",
                value=(
                    "Voting helps StatSnitch appear higher on top.gg so more servers can find it. "
                    "You can vote every **12 hours**.\n"
                    "If you ever get a spam freeze, a vote will cut it in half. 💡"
                ),
                inline=False,
            )

        embed.set_footer(text="Votes are tracked automatically. No extra steps needed.")
        await ctx.send(embed=embed)

    # ==================================================================
    # ██  HELP
    # ==================================================================

    @commands.command(name="statshelp")
    async def statshelp(self, ctx: commands.Context):
        """📋 Show all StatSnitch commands."""
        embed = discord.Embed(
            title="📋 StatSnitch Commands",
            description="I watch everything. You're welcome.",
            color=discord.Color.blurple(),
        )
        sections = {
            "📊 Basic Stats": [
                "`,mystats` — Your personal dashboard",
                "`,topusers` — Server leaderboard",
                "`,wordstats <word> [@user]` — Word frequency",
                "`,serverstats` — Overall server overview",
            ],
            "🎯 Fun Competitions": [
                "`,chaos` — Chaos score leaderboard",
                "`,streaks` — Streak leaderboard",
                "`,nightowls` — Late-night users",
                "`,earlybirds` — Morning users",
                "`,captaincaps` — CAPS LOCK abusers",
                "`,questionqueen` — Most question marks",
            ],
            "⚔️ Duel & Compare": [
                "`,duel @user` — Stat battle",
                "`,compatibility @user` — Interaction analysis",
            ],
            "❤️ Reactions": [
                "`,reactionking` — Most reactions received",
                "`,reactiongiver` — Most reactions given",
                "`,emojistats [emoji]` — Emoji usage",
            ],
            "⏱️ Time-Based": [
                "`,wasted [@user]` — Time wasted estimate",
                "`,timecapsule` — Your stats from a year ago",
            ],
            "🎲 Fun": [
                "`,roastme` — Get roasted",
                "`,complimentme` — Get a compliment",
                "`,fortune` — Get a fortune",
            ],
            "📈 More Leaderboards": [
                "`,topwords [@user]` — Top words",
                "`,topemoji` — Top emojis",
                "`,toplinks` — Top link sharers",
                "`,topattachments` — Top file senders",
                "`,topedits` — Most edits",
            ],
            "🗳️ Support": [
                "`,vote` — Vote for StatSnitch on top.gg",
            ],
        }
        for section, cmds in sections.items():
            embed.add_field(name=section, value="\n".join(cmds), inline=False)
        await ctx.send(embed=embed)