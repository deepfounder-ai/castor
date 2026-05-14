# Camera — vision input via `camera_capture`

The agent can grab a still frame from your machine's camera, send it to a vision-capable LLM, and reason about what's in front of it. Two paths:

- **Web UI** — browser-side capture via `getUserMedia()`. Works on laptops, phones, tablets — any device with a camera and HTTPS.
- **Direct OpenCV** — server-side capture via OpenCV. Used by CLI / headless deployments and as the Web UI's fallback when no browser camera is connected.

Vision needs **HTTPS** in the browser (browsers block camera on `http://`). Run with `--ssl`:

```bash
castor --web --ssl --port 7861
```

## How the agent uses the camera

`camera_capture(prompt?)` is a **core tool** — always loaded, no `tool_search` needed.

```
You:    What am I holding in my hand?
Agent:  [camera_capture]
        → block below becomes "📷 frame captured"
        I see you're holding what looks like a USB-C cable —
        black, with a small power-delivery indicator near the plug.
```

The `prompt` parameter is optional. If supplied, the vision model gets it as the question; otherwise the agent inserts a default like "Describe what you see."

The tool **blocks** until the user-side capture happens (Web UI) or OpenCV grabs a frame (direct). The agent's turn waits — no `[camera in progress…]` ghost messages.

## PiP overlay (Web UI)

When the agent is using the camera, a small picture-in-picture overlay appears in the corner of the page showing your live camera feed. Three things this gives you:

- **Confirmation** the agent has access — no "what did it just see?" mystery
- **Framing** — adjust what's in front of the camera before sending
- **Capture-on-send** — the next message you send includes the current frame as an attachment

Click the overlay to toggle it open / closed. Persistent across turns within a thread.

## "Capture on send" — proactive frame attachment

For workflows where every message benefits from a frame (inventory counting, troubleshooting hardware, observing a process), enable **Settings → Camera → Auto-attach frame on send**:

```
You:    *types*  How many of these are on the shelf?
        *clicks Send — frame is captured + attached automatically*
Agent:  Looking at the shelf, I count 14 boxes in 3 rows...
```

Same WS pipeline as `camera_capture`, just initiated by the message rather than the tool call. Useful when the agent forgets to capture and you want to force-include the visual.

## Configuration

**Settings → Camera**:

| Setting | Default | What it does |
|---|---|---|
| `camera_resolution` | `auto` | `auto` / `480p` / `720p` / `1080p`. Higher = better detail, slower upload. `auto` picks 720p in browsers, native max in OpenCV. |
| `camera_quality` | `85` | JPEG quality (1-100). Lower = smaller payload, faster turn. 85 is the LLM-sweet-spot — readable, ~150 KB. |
| `camera_device_index` | `-1` (auto) | OpenCV device index. `-1` = auto-pick brightest of 0..3. Useful on multi-camera Windows boxes. |
| `camera_facing_mode` | `user` | Browser camera: `user` (front) / `environment` (back). Phones use this. |
| `camera_pip_visible` | `0` | Show PiP overlay by default in new threads. |
| `camera_auto_attach_on_send` | `0` | Capture-on-send mode. |

## Direct OpenCV (CLI / server-side)

If no browser camera is connected (`camera_capture` from the terminal CLI, or via Telegram which has no live camera), Castor falls back to direct OpenCV:

```python
# Roughly what runs server-side
cv2.VideoCapture(camera_device_index)
# Up to 30 retries with sensor warmup if the first frame is black
# (Windows DirectShow common gotcha — first frame is pitch-black while sensor wakes up)
```

The auto-detect picks the **brightest** of indexes 0-3, not the first non-zero. Cameras left disabled in BIOS often show up as index 0 returning pitch-black frames; the brightest-pick logic skips those.

OpenCV captures are persistent per-process — once `cv2.VideoCapture` opens, the next capture reuses it. Fast for repeat `camera_capture` calls (counting items one by one, etc.).

## Vision model compatibility

Vision needs a multimodal LLM:

| Provider | Model |
|---|---|
| **OpenAI** | `gpt-4o`, `gpt-4o-mini`, `gpt-4.1` |
| **OpenRouter** | Many vision models — `anthropic/claude-3.5-sonnet`, `google/gemini-2.0-flash-exp`, etc. |
| **Groq** | `llama-3.2-90b-vision-preview`, `llama-3.2-11b-vision-preview` |
| **Local (LM Studio)** | Qwen2-VL, MiniCPM-V, Llava, Llama 3.2 Vision |
| **Local (Ollama)** | `llama3.2-vision`, `minicpm-v`, `bakllava` |

If your active model **isn't** multimodal, `camera_capture` falls back to "save the frame to uploads, give the model the path, ask the model to OCR / describe via shell tools" — slower and less accurate, but doesn't break the turn.

## Scenarios that just work

### Inventory count

```
You:    How many boxes are on the top shelf?
Agent:  [camera_capture prompt="Count boxes on the top shelf only"]
        → 8 boxes visible on the top shelf — 5 brown corrugated, 3 white.
You:    Remember the count and check again in 2 hours.
Agent:  [memory_save "Top-shelf inventory snapshot: 8 boxes at 2026-05-11 14:32"]
        [schedule_task "in 2h" ...] → Routine #7 scheduled.
```

### Live troubleshooting

```
You:    What's blinking red on the router?
Agent:  [camera_capture]
        → The LED labeled "INTERNET" is solid red. On most TP-Link routers
          that indicates WAN authentication failure — usually wrong PPPoE
          creds or your ISP suspended the line. Try ...
```

### Document on the table

Hold a printed document in front of the camera; the agent OCRs + interprets it. Cheaper than [knowledge ingest](KNOWLEDGE.md) for a one-shot read.

## Mobile

iOS Safari + Android Chrome both work. On phones the back camera (`environment`) is usually more useful — switch in Settings → Camera → Facing mode, or via the camera flip icon in the PiP overlay.

Frame upload happens over the same WebSocket as text, so a slow mobile network = slower captures. The 85% JPEG default keeps payloads in the 100-200 KB range; drop quality if you're on 3G.

## Privacy

- **Frames live on your machine.** Captures are written to `~/.castor/uploads/<timestamp>.jpg` and sent to your configured LLM. They're NOT uploaded to castor servers (there are none).
- **Vision LLM sees the frame.** If you use a cloud vision model (OpenAI, Groq, OpenRouter), the frame is sent there — their privacy policy applies. Use a local vision model (LM Studio Qwen2-VL, Ollama llama3.2-vision) for fully on-prem capture.
- **Uploads cleanup**: `~/.castor/uploads/` is swept at startup — frames older than 14 days are deleted. `uploads/kb/` (indexed knowledge sources) is exempt.
- **Telemetry**: per-turn count of `camera_capture` invocations only. Never the frame, never a description.

## Troubleshooting

**Browser asks for permission every time** — depends on browser; Chrome/Edge remember per-origin, Firefox per-session. Add the localhost cert to your trusted store to make it stickier.

**Frames are pitch-black on Windows** — common DirectShow + dual-camera gotcha. Castor retries up to 30 times with sensor warmup, but if every retry is still black: pick the right `camera_device_index` manually in Settings (try 1 if 0 returns black).

**No camera in OpenCV path** — `pip install opencv-python`. Doctor catches this. Headless servers without a physical camera can install it but `camera_capture` will fail with a clear error.

**PiP overlay won't show** — the WS event for PiP open is gated by `state.streaming`. Reload the page if it gets stuck.

## Cross-links

- [VOICE.md](VOICE.md) — both voice and camera need `--ssl` for browser permissions
- [KNOWLEDGE.md](KNOWLEDGE.md) — for documents you want to search later, ingest them via the knowledge base instead of camera
- [PRIVACY.md](PRIVACY.md) — full data inventory
