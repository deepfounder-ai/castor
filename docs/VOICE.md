# Voice — Live Voice Mode, STT, TTS

castor ships a full voice loop in the Web UI: **press the mic, talk, see the transcript fill in, the agent responds, hear the answer read back, mic re-opens automatically.** VAD handles turn-taking — no walkie-talkie button.

Voice features need **HTTPS** for browser mic/camera access. Run with `--ssl`:

```bash
castor --web --ssl --port 7861
# Web UI at https://localhost:7861
```

Self-signed certs are fine for localhost — your browser will warn once, accept and you're set.

## Live Voice Mode

Click the 🎤 mic icon next to the composer to open Voice Mode. The pipeline:

```
mic → VAD (browser-side) → speech chunk → STT (server)
    → text appears in composer → auto-submit
    → LLM streams reply → TTS chunks back → autoplay
    → mic re-opens (after TTS playback finishes)
```

VAD (Voice Activity Detection) decides when you stop talking. Settings → Voice → **VAD silence threshold** tunes how long the silence has to be (default 600 ms). Speak too fast and the agent cuts you off → raise it. Long pauses break utterances apart → lower it.

The transcript appears in the composer as you talk, so you can see what STT heard. If it misheard, **edit in place** before the auto-submit fires — there's a 1-second window where you can correct.

## STT — speech to text

Three modes (Settings → Voice → STT mode):

| Mode | What runs | Notes |
|---|---|---|
| **`auto`** | Local first, API fallback | Default. Tries `faster-whisper` on CPU; if unavailable, uses configured API. |
| **`local`** | `faster-whisper` only | All on-prem. Model name + device configurable. Slower on CPU; runs comfortably on a laptop. |
| **`api`** | Remote API only | OpenAI-format `/v1/audio/transcriptions`. Works with Groq's free tier — fastest realistic option. |

### Local — faster-whisper

```
CASTOR_STT_DEVICE=cpu               # or `cuda` if you've installed onnxruntime-gpu
Settings → Voice → STT model     # "Systran/faster-whisper-base" (default) etc.
```

Models live under `~/.cache/huggingface/hub/`. First-time use downloads the model (~150 MB for `base`, ~1.5 GB for `large-v3`). `base` is multilingual and good enough for most spoken commands; bump to `medium` or `large-v3` for noisy environments or rare accents.

### API — OpenAI-compatible

```
Settings → Voice → STT API URL    https://api.groq.com/openai/v1/audio/transcriptions
Settings → Voice → STT API key    gsk_...
Settings → Voice → STT API model  whisper-large-v3
```

Groq's Whisper endpoint is **free, very fast, multilingual**. Highly recommended for the API path — turnaround under a second for short utterances.

OpenAI's `whisper-1` works too if you have a key.

### FFmpeg fallback

faster-whisper wants ffmpeg for non-WAV inputs. If ffmpeg isn't installed, castor falls back to **PyAV** (pure-Python decoding) — `pip install av`. Doctor checks both.

## TTS — text to speech

castor auto-detects three TTS API shapes:

| Style | Endpoint pattern | Examples |
|---|---|---|
| **OpenAI** | `POST /v1/audio/speech` with `{model, voice, input}` | OpenAI, compatible local servers |
| **Voice cloning** | `POST /tts` with `voice_id` + `text` | Custom self-hosted (Fish Speech, XTTS) |
| **Fish Speech** | `POST /v1/tts` with `reference_id` | Fish Speech server |

Set the URL in Settings → Voice → TTS API URL, and the voice name/ID in Voice → TTS voice. Test playback with the **Preview voice** button.

### Streaming

If your TTS endpoint streams chunked audio, castor plays each chunk as it arrives — first audio in ~500 ms instead of waiting for the whole reply. Falls back to "wait for full file" if the endpoint doesn't stream.

### Free / local options

- **[Fish Speech](https://github.com/fishaudio/fish-speech)** — self-hosted neural TTS, fast on GPU, supports voice cloning from a 10-second sample
- **OpenAI** — `tts-1` and `tts-1-hd` voices (alloy, echo, fable, onyx, nova, shimmer)
- **OpenRouter** — wraps several TTS backends, OpenAI-compatible
- **No TTS** — leave the field empty; voice mode still works (you'll just read the reply instead of hearing it)

## Configuration summary

All in **Settings → Voice** (web) or `EDITABLE_SETTINGS` (programmatic):

| Setting | Default | What it does |
|---|---|---|
| `stt_mode` | `auto` | local / api / auto |
| `stt_local_model` | `Systran/faster-whisper-base` | HF model name |
| `stt_api_url` | — | OpenAI-compatible `/v1/audio/transcriptions` endpoint |
| `stt_api_key` | — | Bearer key for the API |
| `stt_api_model` | `whisper-1` | Model name as the API expects |
| `stt_vad_silence_ms` | `600` | How long silence before STT cuts off |
| `tts_api_url` | — | OpenAI-style `/v1/audio/speech` or `/tts` endpoint |
| `tts_api_key` | — | Bearer key |
| `tts_voice` | — | Voice name or reference_id |
| `tts_autoplay` | `1` | Auto-play TTS reply (otherwise tap-to-play) |

## Patterns

### "I want everything on-prem"

```
STT mode:  local
STT model: Systran/faster-whisper-medium
TTS:       Fish Speech on a separate GPU box, voice cloned from your own sample
```

Zero outbound HTTP for voice — useful for sensitive workflows. Latency: ~1.5 s STT + ~500 ms TTS first byte on a workstation GPU.

### "Free and fast"

```
STT mode:  api
STT URL:   https://api.groq.com/openai/v1/audio/transcriptions
STT model: whisper-large-v3
TTS URL:   (whatever — OpenAI's tts-1 is cheap; or leave empty for text-only)
```

Groq's Whisper is sub-second on free tier. Combined with a Groq Llama for chat, voice latency is competitive with proprietary cloud agents.

### "Voice in Telegram"

Telegram bot accepts voice messages — castor transcribes them through the same STT pipeline and treats them as text input. Replies are text by default; enable TTS in Settings → Telegram → Reply with voice to get audio back.

## Privacy

- **Local STT** + **no TTS API**: audio never leaves the machine. Recording happens in the browser; the file is POSTed to your own castor server; faster-whisper transcribes locally; the audio file is deleted after transcription.
- **API STT**: audio is sent to the configured endpoint (Groq, OpenAI, …). Their privacy policy applies.
- **TTS**: text is sent to the configured endpoint. Avoid sending sensitive replies through a TTS API; either turn TTS off or use a local one.

No voice data is included in telemetry. The only telemetry tied to voice is the per-turn count of STT/TTS invocations, with no content (see [PRIVACY.md](PRIVACY.md)).

## Troubleshooting

**Mic icon won't enable** — you're on HTTP. Browsers block mic on http://. Re-run with `--ssl`.

**STT returns empty / "no speech"** — VAD cut you off too early. Settings → Voice → VAD silence threshold, raise from 600 → 1000 ms.

**TTS doesn't play** — autoplay blocked by the browser. Click anywhere in the page once, the audio gate unlocks.

**faster-whisper crashes on import** — onnxruntime version mismatch. `pip install -U faster-whisper onnxruntime`. Doctor catches this.

**Wrong language detected** — `faster-whisper` auto-detects, sometimes wrong on the first short utterance. Settings → Voice → STT language: set explicitly (e.g. `ru`, `en`, `es`).

## Cross-links

- [PROVIDERS.md](PROVIDERS.md) — the chat LLM that handles the actual reply
- [CAMERA.md](CAMERA.md) — vision input runs through the same `--ssl` requirement
- [TELEGRAM.md](TELEGRAM.md) — voice messages from Telegram go through STT too
