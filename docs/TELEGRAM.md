# Telegram bot

qwe-qwe can run as a Telegram bot — same agent, same memory, accessible from your phone with no UI of its own to maintain. Streaming responses, slash commands, voice messages, images, formatted text. Routines deliver their output here.

## Setup (one-time)

### 1. Create the bot via BotFather

In Telegram: open [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. You'll get a **bot token** like `1234567890:ABCdef-xyz123…`. Keep it private.

### 2. Set the token in qwe-qwe

```bash
# CLI:
/telegram token 1234567890:ABCdef-xyz123...

# Web: Settings → Telegram → paste token → Save
```

### 3. Start the bot

```
/telegram start          # CLI
# or Settings → Telegram → Start (Web)
```

qwe-qwe begins polling Telegram. The bot is now listening, but it doesn't know who's allowed to talk to it.

### 4. Generate an activation code

```
/telegram activate
→ Activation code: 482917 (expires in 10 minutes)
```

### 5. Send the code to the bot in Telegram

Open your bot in Telegram, send `482917`. qwe-qwe verifies the code, records your Telegram user ID as **the owner**, and the bot is now exclusively yours.

You're done. Send any message to the bot and it'll route through the agent.

## Security model

**Only ONE owner** — set at activation time. Anyone else who messages the bot gets a polite "this bot is private" reply.

**Codes are one-shot** — once you've activated, the code is invalidated. If you need to re-activate (e.g. wiped your Telegram, switched phones), `/telegram revoke` clears the owner; then `/telegram activate` generates a new code.

**Brute-force protection** — 3 wrong activation attempts (sending random numbers to the bot) → **permanent ban** for that Telegram user ID. Generate a new code; the banned user remains banned. This protects against someone with your bot's URL trying to guess the code.

**Token rotation** — if you suspect the bot token leaked, `/newbot` a fresh one in BotFather and update `/telegram token <new>`. Old token is dead, no one can take over.

## Daily use

Once activated, the bot acts as your phone-side qwe-qwe:

```
You (Telegram): Сколько у меня встреч завтра?
Bot: [tool_search("calendar")] [...] You have 3 meetings tomorrow: ...

You (Telegram): Запомни — Anna просит счёт-фактуру до пятницы.
Bot: [memory_save "Anna needs invoice by Friday"] Запомнил.

You (Telegram): /memory
Bot: 47 memories total. Recent:
  · Anna needs invoice by Friday (2 min ago)
  · ...
```

The bot shares **all** state with the Web UI / CLI — same memories, same threads, same skills. A note saved from your phone shows up in the Web UI; a routine you scheduled from the Web UI delivers its results to your phone.

## Slash commands

12 commands work in Telegram. Type `/` and the bot suggests them:

| Command | What |
|---|---|
| `/status` | Provider, model, memory size, uptime |
| `/model` | Show / switch active LLM provider + model |
| `/soul` | Personality settings — trait sliders, name, language |
| `/skills` | List active skills |
| `/memory` | Recent memories — search prompt follows |
| `/threads` | List chat threads with last-message previews |
| `/stats` | Token / call counts |
| `/cron` | Routines — list, run-now, pause, resume |
| `/thinking` | Toggle thinking-block visibility in bot replies |
| `/doctor` | System health check |
| `/clear` | Wipe the active thread |
| `/help` | Reference of all the above |

These are routed to the same code as their CLI equivalents — no separate Telegram-only logic to maintain.

## Streaming replies

Telegram doesn't natively support streaming, so qwe-qwe uses **`editMessageText`** to repeatedly rewrite the bot's reply as the LLM streams. You see the reply growing in real time, with each tool call appearing as a separate edit.

For long replies (above Telegram's 4096-char message cap), qwe-qwe splits into multiple messages on paragraph boundaries.

## Image support

Send a photo to the bot — it gets attached as a vision input to the next turn, the same way the Web UI camera does. Works for:

- "Что на фото?" — read text, describe scene, identify products
- "Считай товары на полке" — count items
- "Прочитай этот документ" — OCR + structured response

Needs a vision-capable LLM (see [PROVIDERS.md](PROVIDERS.md)). Without one, the photo gets saved to `uploads/` and the agent falls back to OCR-via-shell.

## Voice messages

Send a Telegram voice message — qwe-qwe transcribes it through your configured STT (see [VOICE.md](VOICE.md)) and treats the transcript as your text input.

```
You (voice, 4s): "Сколько у меня встреч завтра?"
Bot: [STT] "Сколько у меня встреч завтра?"
     [tool_search("calendar")] [...]
     You have 3 meetings ...
```

The transcribed text shows in the bot's first reply so you can verify what STT heard. If it misheard, send a correction as a normal message.

## Routine delivery

Routines (see [ROUTINES.md](ROUTINES.md)) use `telegram_notify_owner(text)` to send their output to you:

```python
# Inside a routine:
telegram_notify_owner("Daily digest:\n• ...\n• ...")
```

No bot-token / chat-id wrangling — qwe-qwe knows who the owner is. One-line send, formatted message.

## Topics → threads

Telegram **supergroup topics** map 1:1 to qwe-qwe chat threads. If you've made the bot a member of a supergroup with topics enabled:

- Each topic becomes its own thread
- Messages in topic A stay in thread A
- Switching topics in Telegram = switching threads on the qwe-qwe side

Useful for shared bots in a team chat where different topics serve different purposes (one topic per project, one per role, etc.). Note: by default only the verified owner can chat with the bot. To allow team-wide use, see "Multi-user setup" below — it's available but requires opting in.

### Multi-user setup (advanced)

Default is **single-owner**. To allow multiple Telegram users to talk to the bot:

1. `/telegram allow_user <telegram_user_id>` — whitelist a user by their numeric ID
2. Each allowed user gets activation code; they activate the same way
3. Each user has their own owner-state in qwe-qwe; their messages are scoped to their threads

Security note: multi-user mode is **less safe** than single-owner. The bot's reply quality is the user's responsibility; one user can read another's threads if you mis-configure. Don't multi-user unless you really need it.

## Formatting

Bot replies use **MarkdownV2** with HTML fallback (Telegram's two markup modes). The agent's markdown output is converted: bold, italic, inline code, code blocks, links, lists.

Telegram doesn't support tables natively, so qwe-qwe converts markdown tables to fixed-width text blocks. Wide tables get truncated — for "show me data" workflows, return JSON or a screenshot from the Web UI.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `telegram_token` | — | Bot token (BotFather) |
| `telegram_owner_id` | — | Set by activation; read-only afterwards |
| `telegram_polling_interval_s` | `2` | How often qwe-qwe long-polls Telegram for updates |
| `telegram_reply_with_voice` | `0` | If `1`, TTS the agent's reply and send as voice message |
| `telegram_thinking_visible` | `0` | If `1`, include the agent's thinking block in bot replies |
| `telegram_topic_thread_mapping` | `1` | Topics → threads (vs everything-in-one-thread) |

Web UI: Settings → Telegram exposes all of these.

## Privacy

- **All chat content goes through Telegram's servers.** Their privacy policy applies; messages are encrypted in transit but not end-to-end.
- **Bot token is sensitive** — anyone with it can talk to your bot (subject to qwe-qwe's owner check). Store in the [vault](MEMORY.md#vault) if you don't want it in plain settings.
- **Voice / image content** goes to Telegram → qwe-qwe → your configured STT/vision provider. If those are cloud providers, that's where the data ends up.
- **No telemetry on Telegram traffic.** qwe-qwe doesn't count messages, doesn't report bot activity. Routine fires emit the same `turn_complete` event whether they came from Telegram or chat.

## Troubleshooting

**Bot doesn't respond** — check `/telegram` (CLI) for status. Common issues:
- Token wrong → re-paste from BotFather
- Polling stopped → restart with `/telegram start`
- Owner not set → `/telegram activate` and send the code

**"This bot is private"** — you're messaging from a different Telegram user ID than the activated owner. Either switch back to the activated account, or `/telegram revoke` + re-activate from the new account.

**Banned myself by mistake** — guessed the activation code wrong 3 times? Bummer. `/telegram unban <your_user_id>` clears it; you'd need access to the CLI / Web UI to do this.

**Voice messages don't transcribe** — STT misconfigured. See [VOICE.md](VOICE.md). For Telegram-specific debugging: voice messages come as `.oga` files which need ffmpeg or PyAV for decode.

**Replies cut off / weird formatting** — Telegram's MarkdownV2 has strict escaping rules; certain combinations of characters confuse it and qwe-qwe falls back to plain text. Open an issue with the problematic reply and we'll fix the escape pattern.

**Bot replies in the wrong language** — soul.language influences both bot replies and Web UI replies. Set it via `/soul language ru` (or whatever); applies everywhere.

## Cross-links

- [VOICE.md](VOICE.md) — STT for voice messages
- [ROUTINES.md](ROUTINES.md) — `telegram_notify_owner` for routine delivery
- [SOUL.md](SOUL.md) — language / formality preferences
- [PRIVACY.md](PRIVACY.md) — data inventory + telemetry
