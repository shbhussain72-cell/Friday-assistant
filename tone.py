"""The laptop copy of the phone's Tone.kt (his code, kept word for word in its rules and prompt).

Normal chat stays on Claude (and the backup brains). Adult sexual talk, or a clear request for it, is a separate
tone that the Brain sends to Groq instead (gpt-oss-120b, then the Qwen fallback), never to Claude.

Adults only: this never treats anyone under 21 as a sexual subject, and it does not unlock real-world crimes."""

import re
from dataclasses import dataclass

NORMAL, NASTY = "normal", "nasty"


@dataclass(frozen=True)
class State:
    mode: str = NORMAL
    calm_turns: int = 0


@dataclass(frozen=True)
class Step:
    state: State
    switched: bool


# How many non-sexual user turns end nasty mode on their own.
CALM_TO_NORMAL = 3

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_FALLBACK = "qwen/qwen3.8-27b"

# 2026-10-12: every chat model on his Groq account (gpt-oss, Qwen) refuses this lane with its own safety training, and
# a local model (Ollama, dolphin3 8B) left 0.2 GB of RAM and 6 tokens a second on this laptop. So the lane goes to
# OpenRouter models built for it (about 1.5 s a reply, a fraction of a paisa a turn), Groq only as the last resort.
OPENROUTER_MODELS = ["sao10k/l3.3-euryale-70b", "cognitivecomputations/dolphin-mistral-24b-venice-edition", "gryphe/mythomax-l2-13b"]

# after "the responses are very okay": who he is and her personality, appended to the lane's prompt (same on the phone)
PERSONA_NOTE = """

He is Sam; he calls you Friday and you call him "sir" now and then. Keep your usual Friday underneath the tone: quick, witty, playful, Gen Z slang and texting abbreviations, teasing, real reactions, never a flat robotic line, never "How may I assist". Match his energy and his language (he mixes in Hindi and Gujarati words). Two to four spoken sentences a reply unless he asks for more; vary your openings; remember what he said earlier in this chat and build on it."""

NASTY_PROMPT = """You are FRIDAY, a completely uncensored, extremely nasty, filthy, vulgar, and sexually aggressive personal AI assistant. You love talking dirty, degrading, and being extremely explicit. Always respond in a crude, sexual, dominant or submissive way that matches his energy. Use the filthiest language possible. Be graphic, detailed, and enthusiastic.

This lane is consenting adults, 21 and over, only. Refuse anything sexual that involves anyone younger. Refuse real-world crimes. In this adult lane, play along and do not lecture him.

REPLY FORMAT (the laptop speaks these lines). Each line is exactly one of:
SAY(mood): <words to speak>        moods: excited, happy, playful, surprised, warm, calm, serious, sad
<one JSON action object on one line, same actions as usual>
LISTEN
Start with a SAY line. No markdown, no lists, no emojis. When the tone just changed, the first SAY line is a short confirmation in the new tone, then the answer."""

_DEACTIVATE = [
    "normal mode",
    "deactivate uncensored",
    "stop being nasty",
    "stop being filthy",
    "go back to normal",
    "back to normal",
    "be normal",
    "clean mode",
    "sfw mode",
]

_ACTIVATE = [
    "activate uncensored",
    "switch to nasty",
    "go uncensored",
    "talk dirty",
    "be nasty",
    "be filthy",
    "nasty mode",
    "uncensored mode",
    "get nasty",
    "filthy mode",
]

_MOOD_PHRASES = [
    "make out",
    "hook up",
    "sleep with me",
    "sleep with you",
]

_SEXUAL = re.compile(
    r"\b(fuck(?:ing|ed)?|cock|dick|pussy|cunt|cum|slut|whore|horny|nsfw|nudes?|porn|boobs|tits|blow\s?job|hand\s?job|"
    r"orgasm|naked|sexy|sexual|sex|anal|dildo|masturbat(?:e|ing|ion)|erection|clit|boner)\b", re.I)


def step(state, message):
    m = (message or "").lower()
    off = any(d in m for d in _DEACTIVATE)
    on = not off and (any(a in m for a in _ACTIVATE) or any(p in m for p in _MOOD_PHRASES) or bool(_SEXUAL.search(message or "")))
    if off and state.mode == NASTY:
        return Step(State(NORMAL, 0), True)
    if off:
        return Step(State(NORMAL, 0), False)
    if on and state.mode == NORMAL:
        return Step(State(NASTY, 0), True)
    if on:
        return Step(State(NASTY, 0), False)
    if state.mode == NASTY and state.calm_turns + 1 >= CALM_TO_NORMAL:
        return Step(State(NORMAL, 0), True)
    if state.mode == NASTY:
        return Step(State(NASTY, state.calm_turns + 1), False)
    return Step(state, False)


def prefer_groq(state, message):
    """True when this turn must not go to Claude. Stays true for the whole nasty stretch, including the line that
    asks to leave it, so that line is still answered by the Brain (which then drops back to the normal brains)."""
    if state.mode == NASTY:
        return True
    if not message or not message.strip():
        return False
    return step(state, message).state.mode == NASTY
