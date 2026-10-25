# FRIDAY (laptop half)

An always-on voice assistant for Windows 11. You say "Friday" anywhere in a sentence and she listens, thinks with
Claude, answers out loud and can act on the PC: open apps, read and click any window, send WhatsApp messages,
handle reminders, read Gmail and Calendar, and pass tasks to an Android phone companion app.

This is a personal project published as-is. It was built for one person's laptop and habits, so expect to read
the code and change `config.json` before it runs for you. It is not a packaged product.

## How it works

| Part | File | What it does |
|---|---|---|
| Ears | `friday.py`, `audio.py`, `smart_turn.py`, `speaker.py` | Mic, echo cancellation, Silero VAD gate, Whisper large-v3-turbo on the Intel GPU (OpenVINO, faster-whisper as fallback), end-of-turn detection, voiceprint so the TV and other people are ignored |
| Brain | `friday.py` | A persistent `claude -p` stream on your Claude subscription (Haiku by default) that replies with spoken lines and JSON actions |
| Backup brain | `backup_brain.py`, `tone.py` | Free tiers (OpenRouter, Gemini, Groq, Mistral) when the Claude plan limit is reached |
| Voice | `friday.py` | Kokoro (offline ONNX) with moods; Edge TTS for other languages |
| Hands | `hands.py`, `wa.py` | Reads any window as text through Windows UI Automation and clicks, types and scrolls; WhatsApp Desktop automation |
| Phone link | `bridge.py`, `remote.py`, `phone_do.py`, `notify.py` | HTTP bridge on port 8765 (token-protected) for the Android app, a Cloudflare quick tunnel for access from anywhere, Phone Link notifications and calls |
| Google | `google_conn.py`, `google_mcp.py` | Gmail, Calendar and Drive with your own OAuth client, also exposed to the brain as an MCP server |
| Looking after things | `care.py`, `devices.py`, `watchdog.py` | Nightly self-review, device health and diagnostics, a scheduled watchdog that restarts her |
| Window | `panel.html`, `reminders.html`, `logo.py`, `progress.py` | Local control panel at `127.0.0.1:8765/panel`, reminders dashboard, tray icon and a "task ongoing" pill |

Safety rules are built into the prompts and code: one confirmation before anything is sent, posted, booked, paid
or deleted; never types passwords, OTPs or payment PINs; won't press Stop, Disconnect or Turn off unless asked.

## Requirements

- Windows 11, Python 3.12, a microphone. An Intel GPU is used for speech recognition if present.
- [Claude Code](https://claude.com/claude-code) installed and signed in (`claude` on PATH). The brain runs through it.
- Python packages (there's no requirements file yet): `numpy scipy sounddevice requests aiohttp rapidfuzz psutil
  pillow pystray keyboard pyperclip pywin32 uiautomation pycaw onnxruntime kokoro-onnx faster-whisper
  openvino-genai sherpa-onnx openwakeword edge-tts miniaudio pypdf python-docx python-pptx openpyxl`
- Models in `models/` (not in the repo): `kokoro-v1.0.onnx`, `voices-v1.0.bin`, and an OpenVINO export of
  Whisper large-v3-turbo in `whisper-large-v3-turbo-ov/`.

## Setup

1. Clone the repo and create a virtual environment with the packages above.
2. Put the models in `models/`.
3. Edit `config.json`: `user_name`, `people` (names the recogniser should expect), `vocabulary`, voices.
4. Optional: free API keys for the backup brain in `data/keys.txt`, one per line (recognised by prefix, or as
   `name=key` lines for `openrouter`, `gemini`, `groq`, `mistral`, `tavily`).
5. Optional: `python contacts_import.py` builds `data/contacts.json` from Phone Link's copy of your phone contacts.
6. Run `python friday.py` (or `pythonw friday.py` for no console window). A healthy start logs a `HEALTH ok:` line
   in `friday.log`.

Everything personal lives in `data/` (memory, history, contacts, keys, tokens), which is git-ignored. Keep it
that way if you fork this.

## Tools

- `voice_samples.py`, `kokoro_samples.py`, `voice_compare.py`: hear the candidate voices before picking one.
- `phone_do.py`: send one action to the phone through the running Friday, for testing.
- `S_MiniGPT_Roadmap.md`: a separate plan for training a small local language model.
