"""
personality.py — Roasts, compliments, fortunes, and flavor text for StatSnitch.
All humor is playful and never genuinely mean-spirited.
"""

import random
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Generic roast lines (no stat context needed)
# ---------------------------------------------------------------------------

_GENERIC_ROASTS = [
    "If talking were a sport, you'd be disqualified for doping.",
    "Your message count could fund a therapy session. Just saying.",
    "You're here so often the server should charge rent.",
    "Scientists are studying your activity logs as evidence of life without a social life.",
    "Even your keyboard is tired of you.",
    "The server has a 'Most Likely to Still Be Online at 4 AM' award. It has your name on it.",
    "You've sent so many messages, autocorrect has given up on you.",
    "At this point you're not a user, you're a fixture.",
]

_GENERIC_COMPLIMENTS = [
    "You're the kind of person who makes every server a little louder. We mean that warmly.",
    "Your consistency is honestly impressive. Chaotic, but impressive.",
    "The server would be much quieter without you. Suspiciously quiet.",
    "You've never left anyone on read — gold star for effort.",
    "You're like WiFi: everyone's happier when you're around.",
    "Somehow, through all the chaos, you manage to be a delight.",
    "You make this community feel alive. Stay weird.",
]

_FORTUNES = [
    "The stars say you'll send three more messages before reading this one.",
    "Your next peak hour will be 3 AM. The moon weeps for you.",
    "A great reaction awaits you — from someone who accidentally clicked it.",
    "You will one day delete a message you'll regret deleting. It has already happened.",
    "Your future holds more voice channel silence and more typing bubbles.",
    "Someone in this server is about to mute you. Reflect.",
    "The chaos you've sown shall return to you threefold. Godspeed.",
    "You will hit 10,000 messages before you hit the gym. The numbers don't lie.",
]


# ---------------------------------------------------------------------------
# Stat-based roast generators
# ---------------------------------------------------------------------------

def roast_from_stats(u: dict) -> str:
    roasts = []

    top_word = _top_key(u["words"])
    if top_word:
        roasts.append(
            f"You've said \"{top_word}\" {u['words'][top_word]:,} times. "
            f"Do you have other words?"
        )

    if u["caps_messages"] > 50:
        roasts.append(
            f"You've sent {u['caps_messages']:,} CAPS LOCK messages. "
            f"Everything okay at home?"
        )

    if u["messages_deleted"] > u["total_messages"] * 0.15:
        roasts.append(
            f"You've deleted {u['messages_deleted']:,} messages. "
            f"The regret is palpable from here."
        )

    if u["messages_edited"] > u["total_messages"] * 0.2:
        roasts.append(
            f"You edit {u['messages_edited']:,} messages and still can't get it right."
        )

    peak_hour = _top_key(u["hourly_activity"])
    if peak_hour and int(peak_hour) in range(0, 5):
        roasts.append(
            f"Your peak hour is {peak_hour}:00. The darkness suits you."
        )

    if u["question_marks"] > 200:
        roasts.append(
            f"You've typed {u['question_marks']:,} question marks. "
            f"Some of them were rhetorical. None of them were answered."
        )

    if u["voice_minutes"] < 10 and u["total_messages"] > 500:
        roasts.append(
            "Your voice channel time is almost zero. We don't know what you sound like. "
            "We're fine with that."
        )

    if not roasts:
        roasts.append(random.choice(_GENERIC_ROASTS))

    return random.choice(roasts)


def compliment_from_stats(u: dict) -> str:
    lines = []

    thanks_count = u["words"].get("thanks", 0) + u["words"].get("thank", 0)
    if thanks_count > 20:
        lines.append(
            f"You've said 'thanks' or 'thank' {thanks_count:,} times. "
            f"You're the politest chaos goblin here. 🙏"
        )

    if u["reactions_given"] > u["reactions_received"]:
        lines.append(
            f"You give more reactions than you receive ({u['reactions_given']:,} vs "
            f"{u['reactions_received']:,}). Selfless queen. 👑"
        )

    if u["current_streak"] >= 7:
        lines.append(
            f"You've messaged {u['current_streak']} days in a row. "
            f"Consistency? In this economy? Iconic. 🔥"
        )

    if u["total_messages"] > 1000:
        lines.append(
            f"With {u['total_messages']:,} messages you're basically load-bearing for this server. "
            f"Please don't leave. 🏛️"
        )

    if not lines:
        lines.append(random.choice(_GENERIC_COMPLIMENTS))

    return random.choice(lines)


def fortune_from_stats(u: dict) -> str:
    peak_hour = _top_key(u["hourly_activity"])
    if peak_hour and int(peak_hour) in range(0, 5):
        return (
            f"⚡ You type at {peak_hour}:00 AM. "
            "Your future holds… sleep deprivation and questionable life choices."
        )
    if u["caps_messages"] > 30:
        return (
            "⚡ The CAPS LOCK key fears you. "
            "Your future holds a noise complaint from the internet."
        )
    return f"⚡ {random.choice(_FORTUNES)}"


# ---------------------------------------------------------------------------
# Duel commentary
# ---------------------------------------------------------------------------

def duel_verdict(winner_name: str, loser_name: str, margin_pct: float) -> str:
    if margin_pct > 200:
        return (
            f"🏆 **{winner_name}** obliterated **{loser_name}**. "
            f"This wasn't a duel, it was a war crime."
        )
    if margin_pct > 50:
        return (
            f"🏆 **{winner_name}** wins convincingly. "
            f"**{loser_name}** should consider a new hobby."
        )
    return (
        f"🏆 **{winner_name}** edges out **{loser_name}** by a whisker. "
        f"Both of you need to touch grass."
    )


def compatibility_verdict(mentions_ab: int, mentions_ba: int,
                          shared_peak_hour: bool) -> str:
    if mentions_ab == 0 and mentions_ba == 0:
        return "🎯 Verdict: You've never mentioned each other. Strangers with Wi-Fi."
    if mentions_ab > 10 and mentions_ba > 10:
        return "🎯 Verdict: Work spouses or just lonely? Either way, cute. 💕"
    if mentions_ab > mentions_ba * 3:
        return (
            "🎯 Verdict: You mention them constantly; they barely acknowledge your existence. "
            "This is a parasocial relationship. It's fine."
        )
    if shared_peak_hour:
        return "🎯 Verdict: Same peak hour. Misery loves company. 🦉"
    return "🎯 Verdict: Acquaintances at best. Rivals at worst. The plot thickens."


# ---------------------------------------------------------------------------
# Milestone messages
# ---------------------------------------------------------------------------

MILESTONES = {1_000: "1K", 5_000: "5K", 10_000: "10K", 50_000: "50K"}

_MILESTONE_LINES = {
    "1K":  "🎉 **{name}** just hit **1,000 messages!** Welcome to the problem.",
    "5K":  "🔥 **{name}** hit **5,000 messages!** The server is basically their diary now.",
    "10K": "👑 **{name}** smashed **10,000 messages!** A legend. A menace. A local hero.",
    "50K": "💀 **{name}** reached **50,000 messages.** We're getting them a therapist.",
}

def milestone_message(name: str, label: str) -> str:
    template = _MILESTONE_LINES.get(label, f"🎉 **{{name}}** hit **{label} messages!** Scary.")
    return template.format(name=name)


def streak_milestone_message(name: str, days: int) -> str:
    return (
        f"🔥 **{name}** has messaged for **{days} days in a row!** "
        f"The streak is real. The addiction is real."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _top_key(d: dict) -> str | None:
    if not d:
        return None
    return max(d, key=d.__getitem__)