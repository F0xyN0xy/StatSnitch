# 📊 StatSnitch — The Discord Stats Bot That Judges You

> *"I watch everything. You're welcome."*

StatSnitch is a full-featured Discord stats bot with a sarcastic personality. It tracks every message, reaction, edit, delete, and voice session — then serves the data back with roasts, celebrations, and unsolicited commentary.

---

## 🗂️ File Structure

```
statsnitch/
├── bot.py           # Main bot + all Discord event listeners
├── commands.py      # All 25+ commands as a discord.py Cog
├── storage.py       # JSONBin.io persistence + in-memory caching
├── personality.py   # Roasts, compliments, fortunes, flavor text
├── requirements.txt # Python dependencies
├── .env.example     # Environment variable template
└── README.md        # This file
```

---

## ⚙️ Setup

### 1. Prerequisites

- Python **3.11+**
- A Discord bot token
- A [JSONBin.io](https://jsonbin.io) account (free tier is fine)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Fill in:

| Variable | Where to find it |
|---|---|
| `DISCORD_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) → Your App → Bot → Token |
| `JSONBIN_API_KEY` | [JSONBin.io](https://jsonbin.io) → Account → Master Key |
| `JSONBIN_BIN_ID` | Leave blank — the bot creates it automatically on first run, then prints the ID for you to save |
| `BOT_PREFIX` | Default is `!` — change if you want |

### 4. Discord Bot Permissions

In the Developer Portal, enable these **Privileged Gateway Intents**:
- ✅ Message Content Intent
- ✅ Server Members Intent
- ✅ Presence Intent (optional)

Bot invite URL needs these permissions:
- Read Messages / View Channels
- Send Messages
- Embed Links
- Add Reactions
- Read Message History
- Connect (voice — for voice tracking)

### 5. Run the bot

```bash
python bot.py
```

On first run, the bot will print your new JSONBin bin ID. **Copy it into your `.env`** as `JSONBIN_BIN_ID=` so data persists across restarts.

---

## 📋 All Commands

### Basic Stats
| Command | Description |
|---|---|
| `!mystats` | Your personal dashboard with roast |
| `!topusers` | Server leaderboard with funny titles |
| `!wordstats <word> [@user]` | Word frequency with commentary |
| `!serverstats` | Overall server overview |

### Fun Competitions
| Command | Description |
|---|---|
| `!chaos` | Chaos score leaderboard |
| `!streaks` | Daily message streak leaderboard |
| `!nightowls` | Most active 12am–5am ("Touch grass?") |
| `!earlybirds` | Most active 5am–8am ("Who hurt you?") |
| `!captaincaps` | Most CAPS LOCK usage |
| `!questionqueen` | Most question marks |

### Duel & Compare
| Command | Description |
|---|---|
| `!duel @user` | Head-to-head stat battle |
| `!compatibility @user` | Interaction analysis |

### Reactions & Engagement
| Command | Description |
|---|---|
| `!reactionking` | Most reactions received |
| `!reactiongiver` | Most reactions given |
| `!emojistats [emoji]` | Emoji usage leaderboard |

### Time-Based
| Command | Description |
|---|---|
| `!wasted [@user]` | Estimated time wasted on server |
| `!timecapsule` | Stats from a year ago |

### Fun Random
| Command | Description |
|---|---|
| `!roastme` | Get roasted based on your stats |
| `!complimentme` | Get a wholesome compliment |
| `!fortune` | Fake fortune based on habits |

### More Leaderboards
| Command | Description |
|---|---|
| `!topwords [@user]` | Most used words |
| `!topemoji` | Most used emojis server-wide |
| `!toplinks` | Most links shared |
| `!topattachments` | Most files/images sent |
| `!topedits` | Most edited messages |

### Help
| Command | Description |
|---|---|
| `!statshelp` | Show all commands |

---

## 🔄 Auto-Tracking

The bot automatically tracks on every event:

| Event | What's tracked |
|---|---|
| **Message sent** | Count, words (no stopwords), caps %, links, emojis, attachments, mentions, streaks, hourly/daily activity |
| **Message edited** | Edit counter |
| **Message deleted** | Delete counter |
| **Reaction added** | `reactions_given` for reactor, `reactions_received` for author |
| **Voice join/leave** | Minutes spent in voice |

### 🎉 Auto-Announcements
The bot automatically posts to the channel when:
- A user hits **1K / 5K / 10K / 50K** messages
- A user reaches a **7-day** or **30-day** message streak

---

## 💾 Data Storage

All data is stored in JSONBin.io:

- **In-memory cache** is the live source of truth
- **Flushes to JSONBin** every 30 messages OR every 5 minutes
- **Force-saves on shutdown** (Ctrl+C handled gracefully)
- A local log file `statsnitch.log` records activity

---

## 🔧 Customisation

- **Add roasts**: Edit the lists in `personality.py`
- **Change flush intervals**: Edit `FLUSH_INTERVAL` and `FLUSH_EVERY_N` in `storage.py`
- **Change stopwords**: Edit the `STOPWORDS` set in `storage.py`
- **Add milestones**: Edit the `MILESTONES` dict in `personality.py`

---

## 🐛 Troubleshooting

| Problem | Solution |
|---|---|
| Bot doesn't respond | Check Message Content Intent is enabled |
| JSONBin errors | Verify your `JSONBIN_API_KEY` is the **Master Key**, not an Access Key |
| Missing `JSONBIN_BIN_ID` | Run once, copy the printed bin ID to `.env` |
| Commands not found | Confirm `BOT_PREFIX` in `.env` matches what you type |

---

*StatSnitch: Because someone has to keep receipts.*