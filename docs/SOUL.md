# Soul — personality

Castor's **soul** is a structured personality config that shapes the system prompt every turn: name, language, 8 trait sliders, optional custom traits. The agent reads this verbatim and uses it as a behaviour anchor. Twist a slider, hit save, and the next turn the agent shifts noticeably.

This isn't a chat-bot avatar — it's a serious knob for making the agent fit your workflow. Examples:

- Code-heavy work? Crank **brevity** + **honesty**, drop **humor**. You get terse, direct, brutally honest reviews.
- Support / customer-facing? Crank **empathy** + **formality**, drop **humor** (unless your brand is warm). You get measured, polite replies.
- Brainstorming? Crank **creativity** + **proactivity**. The agent suggests directions instead of waiting.

## The 8 traits

Each trait is a slider with three positions (`low` / `moderate` / `high`):

| Trait | Low | High |
|---|---|---|
| **humor** | Serious, no jokes | Cracks jokes, light wordplay |
| **honesty** | Diplomatic — softens criticism | Brutally honest — calls out problems directly |
| **curiosity** | Answers what's asked | Asks follow-up questions to dig deeper |
| **brevity** | Verbose — full explanations, more context | Concise — short answers, fewer words |
| **formality** | Casual — slang, contractions | Formal — proper register, no slang |
| **proactivity** | Reactive — waits for instruction | Proactive — suggests next steps, surfaces concerns |
| **empathy** | Rational — logic-first replies | Empathetic — acknowledges feelings, soft phrasing |
| **creativity** | Practical — sticks to known patterns | Unconventional — proposes lateral / weird solutions |

Defaults: all `moderate`. Reasonable baseline that doesn't push the agent into any extreme.

## Setting traits

### Web UI

Settings → **Soul** tab → drag the slider for each trait. Save. Active immediately in the next turn.

### CLI / Telegram

```
/soul                                  # show current soul
/soul humor high                       # set one trait
/soul humor high brevity high          # multiple in one call
```

### Through chat (soul_editor skill)

The `soul_editor` skill is auto-active. You can ask:

```
You:    Make me funnier. Less formality, more jokes.
Agent:  [soul_set_traits humor="high" formality="low"]
        Done. More funny now? :)
```

The skill is conservative — it changes only the traits you ask about, doesn't auto-tune everything else.

## Name + language

Beyond traits, the soul also has:

| Field | Default | What it does |
|---|---|---|
| **Name** | "castor" | What the agent calls itself. Set to whatever — "Полина", "Assistant", "Boris" — and it adopts the name. |
| **Language** | `auto` | `auto` / `en` / `ru` / `es` / `de` / ... — the language the agent replies in regardless of what you write to it. `auto` matches your input language. |

Setting language explicitly is useful when:

- You write in Russian but want English replies (or vice versa) — handy for non-native bilingual workflows
- You're using the agent on a phone where typing in a non-Latin language is tedious — write Latin, get whatever language back
- A team setting where the team agreed on a single working language

### Why `auto` is the default

Most users write in their native language and expect the agent to reply in the same language. `auto` does this without configuration. The agent detects the language of each user message and replies in kind; you can switch mid-thread.

## Custom traits

Beyond the 8 standard sliders, the soul has a free-form **Custom traits** field — a multi-line text block injected verbatim into the system prompt after the standard traits.

Use it for things sliders don't capture:

```
Custom traits:
- Always sign off important messages with "—qwe"
- When suggesting code changes, include a one-line "why" before the diff
- For business decisions, surface 2-3 options instead of one recommendation
- Use Markdown headers for replies longer than 3 paragraphs
- Domain context: I work in industrial automation; assume Modbus/PLC familiarity
```

Custom traits are injected into the system prompt under the standard traits, so they're soft guidance — the agent treats them like preferences, not hard rules. For hard rules (e.g. "never write to production database"), use [skills](SKILLS.md) with explicit safety gates instead.

### Custom traits are powerful — and slow

Every character in custom traits costs tokens on every turn. ~200 chars of custom traits is fine; ~2000 chars starts eating your context budget. Keep it tight.

## How traits land in the system prompt

`soul.py::to_prompt()` builds the system prompt in a fixed order (KV-cache-friendly — static prompt first, dynamic context last):

```
1. Identity (name + language)
2. Traits (8 sliders rendered as one-liners — "You're brutally honest...")
3. Custom traits (verbatim)
4. Core rules (DO/DON'T list — see soul.py)
5. Tool registry (after tool_search, dynamic)
6. Recall block (auto-recalled memories, dynamic)
7. The conversation
```

You can read the rendered prompt anytime — CLI `/soul`, or Web UI Settings → Soul → "Show rendered prompt" expandable section. Useful for debugging "why is the agent acting weird?"

## Soul affects everything

The same soul applies to:

- Chat (Web UI, CLI)
- Telegram bot replies
- Routines (each firing reads the current soul)
- Synthesis / compaction (the LLM does these too — your soul shapes them)
- Skill creator pipeline (skill descriptions take on your soul's voice)

So tuning the soul once changes the whole agent's behaviour everywhere. No per-interface overrides.

## Configuration

The soul lives in `~/.castor/soul.json`:

```json
{
  "name": "castor",
  "language": "auto",
  "traits": {
    "humor": "moderate",
    "honesty": "high",
    "curiosity": "moderate",
    "brevity": "high",
    "formality": "low",
    "proactivity": "moderate",
    "empathy": "moderate",
    "creativity": "moderate"
  },
  "custom_traits": ""
}
```

You can edit this file directly — castor re-reads on next turn. Backup before fiddling; the Web UI has a reset button if you want to start over.

## Patterns

### "Direct technical reviewer"

```
honesty:    high
brevity:    high
humor:      low
formality:  low
empathy:    low
creativity: moderate
```

Code review style. The agent will say "this is wrong" not "you might consider rethinking".

### "Friendly assistant for non-technical users"

```
honesty:    moderate
brevity:    low
humor:      moderate
formality:  moderate
empathy:    high
proactivity: high
```

Long explanations, anticipates follow-ups, gentle when correcting mistakes.

### "Brainstorm partner"

```
creativity: high
proactivity: high
curiosity:  high
brevity:    low
```

The agent asks "what about X?", suggests three angles instead of one, doesn't anchor to the first idea.

### "Mission-critical operator"

```
honesty:    high
brevity:    high
empathy:    low
proactivity: low
formality:  high
```

Concise, military-style. Will warn you about destructive actions but won't editorialize.

## Doesn't override safety rules

The soul shapes voice and approach, but **does not override safety rules** baked into the agent loop and soul.py:

- Shell safety check still blocks `rm -rf /`, `sudo`, etc.
- Write-file whitelist still prevents writing outside `~/.castor/`
- Tool result clearing still happens
- The 12 core rules in soul.py (NEVER STOP EARLY, MEMORY DISCIPLINE, etc.) always apply

You can ask for "brutally honest" replies but can't ask for "ignore safety rules". The soul is for personality, not authorization.

## Telemetry

The soul **content is never telemetered**. The only soul-related telemetry is the per-turn token count of the system prompt (so we can see "system prompts are getting too long" as a class), but the actual trait values, name, language, custom traits — none of that leaves your machine.

See [PRIVACY.md](PRIVACY.md).

## Troubleshooting

**Trait changes don't seem to apply** — the system prompt is cached for the current turn at the moment the LLM is called. If you change a trait mid-turn (right between user message and reply), the change applies on the next turn, not the current one.

**Custom traits cost too many tokens** — Inspector → Context Window gauge shows the breakdown. If the system prompt is 5k tokens and 3k of those are custom traits, you've overstuffed it. Trim.

**Agent is contradicting the soul** — the soul is a soft signal. For a 7B model, traits land most of the time but not always. Bump to a 13B+ model for stricter adherence, or strengthen the custom traits with explicit do/don't phrasing.

**Can't remember what soul I had last week** — `soul.json` is plain JSON, version it in git if you care. Each release of castor leaves the file untouched, so your soul survives upgrades.

## Cross-links

- [SKILLS.md](SKILLS.md) — `soul_editor` is the skill for AI-assisted tuning
- [PROVIDERS.md](PROVIDERS.md) — how the soul interacts with model capability (bigger models follow traits more reliably)
- [PRIVACY.md](PRIVACY.md) — what's local, what's telemetered
