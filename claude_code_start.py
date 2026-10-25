"""Claude Code SessionStart hook: tells a new Claude Code session about Friday feedback he hasn't had worked on.

He asked Friday (phone, 13 Oct 03:33) to start on his feedback as soon as Claude Code starts on the laptop; the
backup brain promised it and nothing happened. Feedback from both devices lands in data/feedback.md ("- [ ] ..."),
and the nightly review's code wishes in data/wishes.md (open = no "[done" note). This prints the items not shown
to a session before (data/claude_code_seen.json) as additionalContext, plus how many older ones are still open.
Prints nothing when there's nothing new, so other sessions stay clean. Never fails the session.
"""
import json
import os
import sys

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SEEN = os.path.join(DATA, "claude_code_seen.json")


def _lines(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f]
    except OSError:
        return []


def open_items():
    fb = [l[6:].strip() for l in _lines("feedback.md") if l.startswith("- [ ] ")]
    wishes = [l[2:].strip() for l in _lines("wishes.md") if l.startswith("- ") and "[done" not in l]
    return fb, wishes


def main():
    fb, wishes = open_items()
    try:
        with open(SEEN, encoding="utf-8") as f:
            seen = set(json.load(f))
    except (OSError, ValueError):
        seen = set()
    new_fb = [x for x in fb if x not in seen]
    new_wishes = [x for x in wishes if x not in seen]
    if not new_fb and not new_wishes:
        return
    older = len(fb) - len(new_fb) + len(wishes) - len(new_wishes)
    text = ["FRIDAY (his voice assistant, D:\\dev\\Friday + D:\\dev\\Friday-Android) has new feedback he wants worked on "
            "when Claude Code starts (his ask, 13 Oct). Show him this list in a few lines and ask him to confirm it "
            "(his rule: confirm the feedback, then start); once he confirms, work through them following the Friday "
            "memories (evidence from the logs first, install on the phone only when it's idle). If his first message "
            "is about something else, mention this in one line and do his task."]
    if new_fb:
        text.append("New feedback (data/feedback.md):")
        text += [f"- {x}" for x in new_fb]
    if new_wishes:
        text.append("New code wishes from her nightly review (data/wishes.md):")
        text += [f"- {x}" for x in new_wishes]
    if older:
        text.append(f"({older} older items are still open in those files.)")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(text)}}))
    if "--dry-run" not in sys.argv:
        with open(SEEN, "w", encoding="utf-8") as f:
            json.dump(sorted(seen | set(new_fb) | set(new_wishes)), f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken hook must never get in the way of his session
        pass
