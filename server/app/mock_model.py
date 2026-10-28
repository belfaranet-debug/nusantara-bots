"""MOCK model adapter for the Phase 3 app shell.

This is the clearly-labelled demo adapter used when no BYO-API is
configured in the browser. It generates deterministic text locally
from the character's declared role and skills. It makes NO network
call, NO provider call, and takes NO credential of any kind — its
signature is (character, user_text, room_mode, members) only.

When the user configures BYO-API, the BROWSER calls the model API
directly (see static/app.js); this module is then not used for
that reply, and the server never sees the browser's credentials.
"""

MOCK_LABEL = "MOCK model — demo reply, no real API call"


def mock_reply(character, user_text, room_mode="chat", members=None):
    """Return (reply_text, presence_events) for one user message.

    Presence events follow PRESENCE.md: typing, status, turn,
    optional handoff (meeting rooms), and done. Every event carries
    content — no empty confirmations.
    """
    members = members or []
    name = character.name
    role = character.role
    skills = ", ".join(character.skills) if character.skills else "no declared skills"
    if room_mode == "meeting" and len(members) > 1:
        idx = members.index(name) if name in members else 0
        nxt = members[(idx + 1) % len(members)]
        turn = {"type": "turn", "speaker": name, "next": nxt}
        handoff = {"type": "handoff", "from": name, "to": nxt,
                   "note": "Floor passes to %s after this turn." % nxt}
        extra = [turn, handoff]
    else:
        extra = [{"type": "turn", "speaker": name, "next": None}]
    events = [
        {"type": "typing", "character": name},
        {"type": "status", "character": name,
         "text": "Reading your message and recalling %s memory…" % character.memory_scope},
        {"type": "status", "character": name,
         "text": "Drafting a reply as %s…" % role},
    ] + extra + [
        {"type": "done", "character": name,
         "note": "Reply ready (%s). Result lives in this room's chat history." % MOCK_LABEL},
    ]
    text = (
        "[%s] %s here (%s; skills: %s). You said: %r. "
        "This is a demo reply — configure BYO-API in the panel above "
        "to get a real model reply from your own provider, sent "
        "directly from your browser." % (MOCK_LABEL, name, role, skills, user_text)
    )
    return text, events
