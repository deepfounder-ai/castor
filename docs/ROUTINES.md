# Routines — scheduled tasks the agent runs without you

A **routine** is a chat thread with a schedule attached. At each firing, the agent runs through its thread context as if you'd just sent the prompt — full tool access, memory, the lot — and appends the new user-turn + assistant-reply to the same thread.

Three reasons this design works better than a "cron + script" approach:

1. **You can debug a routine by talking to it.** If yesterday's run went wrong, scroll back, add a correction in the thread ("don't include weekend days next time"), and the next firing reads it as context.
2. **Tool access is full.** The routine can read memory, browse, run shell, send Telegram messages, call MCP servers — whatever the agent can do live, a routine can do automated.
3. **One routine = one thread = one timeline you can audit.** No separate logs to grep.

## Creating a routine

### Web UI

Web UI → **Routines** tab → **New routine** modal:

- **Prompt** — what the agent should do at each firing. Treat it like a chat message you're sending into the future.
- **Frequency picker** — day chips (Mon-Sun) + time picker, OR a free-form schedule string.
- **Telegram delivery** (optional) — pipe the reply to your Telegram via the verified-owner channel.

Hit Create. The routine starts running on the next firing slot.

### CLI

```
/cron new "daily 09:00" "Read overnight S&P futures and post a 3-line digest to Telegram."
```

```
/cron list                              # show all routines
/cron run <id>                          # fire now, out-of-schedule
/cron pause <id> / resume <id>          # skip firings without deleting
/cron rm <id>                           # delete (cascades the thread)
/cron edit <id> schedule "every 4h"     # change schedule
```

### Inside chat — agent creates a routine

The agent has `schedule_task` after `tool_search("schedule")`. So you can:

```
You:    Every Monday at 9 AM check the electricity bill
        and notify me on Telegram if it's above 5000.
Agent:  [tool_search("schedule")]
        [schedule_task schedule="mon 09:00" thread_prompt="..."]
        → Routine #5 created.
```

## Schedule syntax

Natural-language, no 5-field cron required:

| Input | Meaning |
|---|---|
| `"in 5m"` | Run once 5 minutes from now |
| `"in 2h"` | Run once 2 hours from now |
| `"every 30m"` | Repeat every 30 minutes |
| `"every 2h"` | Repeat every 2 hours |
| `"every 2 days 09:00"` | Repeat every other day at 09:00 |
| `"daily 09:00"` | Every day at 09:00 |
| `"weekdays 09:00"` | Mon-Fri at 09:00 |
| `"weekends 10:00"` | Sat-Sun at 10:00 |
| `"mon,wed,fri 14:30"` | Those days at 14:30 |
| `"14:30"` | Once today/tomorrow at 14:30 (whichever comes first) |

Time is your local timezone.

## What a routine looks like running

Open the routine's thread (one click from the card). Each firing appends:

```
[scheduled run · 2026-05-11 09:00]
User: Read overnight S&P futures ...
Assistant: [tool calls...] [reply...]
```

You see the tool calls expanded, the thinking block, the final reply — same fidelity as a live chat. Costs the same in LLM tokens. Lives in the same thread context, which means:

- **Memory works.** The routine remembers what it learned in earlier firings.
- **Mid-thread corrections accumulate.** Add a user message between firings ("ignore the futures on FOMC days") and the next firing reads it.
- **History is auditable.** Every run is a turn in the thread; nothing is hidden.

## Status badges

Each routine card has a four-state badge:

- **🟢 running** — currently executing a firing
- **🟢 active** — scheduled, idle, will fire on the next slot
- **🔴 last run failed** — most recent firing errored. Click into the thread to see the failure tool calls / error message.
- **⏸ paused** — schedule skipped, will resume when you toggle

Failures don't auto-disable the routine — Castor assumes you want to know about transient errors but keep trying. If a routine fails 10 times in a row, the badge stays red but firings continue. Pause it manually if you want it stopped.

## Tools available inside routines

Everything from a live chat, plus one extra:

- **`telegram_notify_owner(text)`** — single-call send to the verified Telegram owner. No bot-token / chat-id wrangling — castor knows who you are. Common pattern: every routine ends with `telegram_notify_owner("daily digest: ...")`.

Things to know:

- **No interactive tools.** `camera_capture` and `canvas_prompt` block waiting for the user. In a routine those will time out and return error markers. Use them only in live chats.
- **Routines respect the `confirm=true` gate** for serial writes (hardware safety). A routine that writes to a PLC must include `confirm=true` in the call — there's no separate "auto-confirm in routines" override, because routines write to actuators is exactly when you want the safety gate most.
- **Routines can spawn sub-tasks.** `spawn_task` is available — spin off a separate-thread investigation that doesn't pollute the routine's main thread.

## Patterns

### Daily digest to Telegram

```
"daily 08:00"
Prompt:
  Read overnight news from RBC and Bloomberg (use brave_search if needed,
  then http_request to fetch). Summarize 3 most-important items,
  2 sentences each. Send to Telegram via telegram_notify_owner.
```

### Hardware monitoring

```
"every 5m"
Prompt:
  Read GPS coordinates from serial port COM4 (NMEA $GPRMC, baud 9600).
  If we're within 200m of warehouse (52.0123, 4.5678), telegram_notify_owner
  "Approaching warehouse". Otherwise stay silent.
```

### Inbox triage

```
"weekdays 09:00"
Prompt:
  Open my Gmail via the visible browser. Read subjects of unread messages.
  Triage:
    - if sender is in my known-clients memory, summarize the email
    - if it looks like a newsletter, mark as read but don't notify
    - if urgent (red flag, contract, deadline), telegram_notify_owner
  Don't move or delete anything — just read + classify.
```

### Periodic database health check

```
"every 1h"
Prompt:
  Run shell: psql -c "SELECT count(*) FROM orders WHERE status='pending';"
  If > 1000, telegram_notify_owner with the count. Otherwise log to memory.
```

### Once-off reminder

```
"in 2h"
Prompt:
  Remind me to take the cake out of the oven. telegram_notify_owner now.
```

Yes, you can use the agent as a smart timer. It's overkill for that one task, but if the reminder is conditional ("only if it's past 6 PM") or composite ("remind me + check the calendar + book a slot"), routines beat a timer.

## Debug-via-dialogue

The most novel design choice. A routine that's misbehaving:

```
1. Open the routine's thread (one click from the card)
2. Read the last firing — see the tool calls, the bad output
3. Type a correction as a regular user message:
   "Hey — the digest shouldn't include closed-stock notices. Skip those."
4. Save. Next firing reads your correction as context.
5. Watch the next firing handle the case correctly.
```

No prompt editing. No "where's the cron script". Just talk to the routine.

## Skipping / pausing

- **Pause** — toggle the ⏸ button. Firings skip; routine stays in the list.
- **Resume** — toggle again. Next firing happens on the next slot (not "make up missed firings").
- **Run now** — ▶ button fires immediately, out-of-schedule. Useful for testing.

## Storage

Routines live in the `scheduled_tasks` SQLite table:

| Column | Notes |
|---|---|
| `id` | Surrogate PK |
| `schedule` | The natural-language string (re-parsed at each firing) |
| `thread_id` | The chat thread this routine runs in |
| `prompt` | What the agent receives at each firing |
| `paused`, `last_run`, `last_error` | Bookkeeping |

Delete the routine, delete the thread, and the thread's message history all cascade together — no orphans.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `cron_check_interval_s` | `30` | How often the scheduler wakes to look for due firings |
| `cron_max_concurrent` | `3` | Max routines running at once. Higher = more parallelism but more LLM load. |
| `cron_timeout_s` | `1800` | Per-firing hard timeout. After this the firing is marked failed. |

## Privacy

- All routines run on your machine. The schedule is local, the prompts are local, the thread history is local.
- Each firing uses your configured LLM — if that's a cloud provider, the prompt + tool calls + the routine's thread context go to that provider for that turn. Use a local LLM for sensitive routines.
- Telemetry doesn't include routine prompts or outputs. Only `tool_calls_count` and `tool_errors_count` in the `turn_complete` event.

## Troubleshooting

**Routine didn't fire** — `cron_check_interval_s` is 30s by default; a routine scheduled for 09:00 might fire at 09:00:29. Check `last_run` on the card. If still empty after a minute, the scheduler crashed — restart castor or check `logs/castor.log`.

**`telegram_notify_owner` returns "not verified"** — you haven't completed Telegram setup. See [TELEGRAM.md](TELEGRAM.md).

**Routine fires twice** — possible double-start on rapid restart. Pause + Resume to reset state, or delete + recreate.

**Routine "succeeds" but does nothing visible** — the agent might have decided the work was unnecessary. Open the thread, read the firing — soul rules (especially "don't act without reason") sometimes short-circuit. Tighten the prompt: be explicit about what the routine should DO at each firing.

## Cross-links

- [TELEGRAM.md](TELEGRAM.md) — `telegram_notify_owner` and Telegram delivery
- [SKILLS.md](SKILLS.md) — `schedule_task` tool comes from the routines / scheduler skill
- [BROWSER.md](BROWSER.md) — visible browser sessions are NOT recommended in routines (no interactive screen); use headless for scrape-style routines
