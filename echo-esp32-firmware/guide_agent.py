"""
Step-by-step guide for everyday tasks, personalised by Hindsight memory.

"I want to make a cup of tea" starts a guide. Each touch of the glasses moves
it on: "done" for the next step, "help" or "again" to repeat with a cue,
"stop" to end. The first time, it walks through every generic step. After a
few sessions it remembers which step this person gets stuck on, which cue
got them unstuck, which steps they always manage alone, and how they like
things done, and the guide gets shorter and more personal.

    Hindsight      reflects over past sessions and returns, as structured
                   output, how each generic step went lately: does it alone,
                   needs a reminder, got stuck, or no info, with the cue that
                   helped and the memory it is based on. One summary of each
                   session is retained afterwards.
    this module    owns the template and the safety rules, and builds the
                   plan from that classification in code

Fail safe, not smart: if memory is unavailable or returns nothing usable, the
generic template is used as is. Memory can reword a step, attach a cue to it,
or let it be skipped; it cannot invent steps, reorder them, drop a safety
step, or mention medication.
"""

import re
import textwrap
import time
from datetime import datetime

import hindsight_memory

MAX_WORDS = 12

# Generic templates. `must` steps can be reworded but never skipped: they are
# the ones where getting it wrong hurts (hot water) or the task isn't done.
TASKS = {
    "make-tea": {
        "title": "Tea",
        "name": "making a cup of tea",
        "triggers": [r"\b(make|making|have|want|fancy|get)\b.{0,25}\btea\b",
                     r"\bcup of tea\b", r"\bcuppa\b"],
        "steps": [
            ("mug", "Take a mug from the cupboard.", False),
            ("fill", "Fill the kettle with water to the line.", False),
            ("switch", "Switch the kettle on.", False),
            ("boil", "Wait for the kettle to click off.", True),
            ("bag", "Put one tea bag in the mug.", False),
            ("pour", "Pour the hot water into the mug, slowly.", True),
            ("steep", "Wait three minutes, then take the tea bag out.", False),
            ("milk", "Add milk and sugar if you like.", False),
            ("enjoy", "Your tea is ready. Sit down and enjoy it.", True),
        ],
    },
    "lock-up": {
        "title": "Night",
        "name": "locking up for the night",
        "triggers": [r"\block (up|the doors?)\b", r"\b(going|get ready) (to|for) bed\b"],
        "steps": [
            ("back", "Lock the back door and try the handle.", True),
            ("windows", "Close the downstairs windows.", False),
            ("cooker", "Check the cooker knobs are all off.", True),
            ("front", "Lock the front door and try the handle.", True),
            ("keys", "Put your keys in the blue bowl.", False),
            ("lights", "Turn off the downstairs lights.", False),
            ("bed", "All done. Time for bed.", True),
        ],
    },
}

# Never let a generated step talk about these: that is for people, not glasses.
UNSAFE = re.compile(r"\b(pill|pills|tablet|tablets|medicine|medication|dose|insulin|"
                    r"inhaler|blood pressure|prescription|drug|drugs)\b", re.I)

CONTROL = {
    "next": re.compile(r"^\W*(done|next|ok(ay)?|yes|finished|got it|did it|and now)\b", re.I),
    "help": re.compile(r"\b(help|again|repeat|what|confused|stuck|don'?t know|where)\b", re.I),
    "stop": re.compile(r"\b(stop|cancel|quit|never ?mind|that'?s enough)\b", re.I),
}


# A task is only started on a request, so "we have tea at four" doesn't.
REQUEST = re.compile(r"\b(i want|i'?d like|i would like|i'?d love|help me|let'?s|can you|"
                     r"could you|how do i|i fancy|time for|time to|i need to)\b", re.I)


def detect_task(transcript):
    """'I'd love a cup of tea' -> 'make-tea', or None."""
    text = (transcript or "").lower()
    if not REQUEST.search(text):
        return None
    for key, task in TASKS.items():
        if any(re.search(p, text) for p in task["triggers"]):
            return key
    return None


def control_word(transcript):
    """What a touch during a guide means: next / help / stop. Silence or
    anything unrecognised counts as 'next' (they touched to move on)."""
    text = (transcript or "").strip()
    for word in ("stop", "help", "next"):
        if CONTROL[word].search(text):
            return word
    return "next"


def task_tag(key):
    return f"task:{key}"


def words(text):
    return len((text or "").split())


def clean(text):
    return " ".join(str(text or "").split())


class GuideSession:
    """One run of one task. plan() once, then current() / advance(); at the
    end, finish() retains what happened so the next run is better."""

    def __init__(self, task_key, ltm, wearer="the wearer", now=None):
        self.key = task_key
        self.task = TASKS[task_key]
        self.ltm = ltm
        self.wearer = wearer
        self.now = now or datetime.now()
        self.template = {sid: (text, must) for sid, text, must in self.task["steps"]}
        self.steps = []           # [{"id", "say", "cue", "why"}]
        self.memory = None        # reflect text used for this plan
        self.facts = []
        self.source = "template"
        self.index = 0
        self.help_at = {}         # step id -> times help was asked
        self.started = time.time()
        self.finished = False
        self.summary = ""         # what finish() retained

    # ---- planning ---------------------------------------------------------

    def plan(self):
        """Ask Hindsight how each generic step went in past sessions, as
        structured output, then build today's plan from that in code."""
        ids = list(self.template)
        steps = "\n".join(f"- {sid}: {text}" for sid, (text, _m) in self.template.items())
        schema = {
            "type": "object",
            "properties": {
                "has_history": {"type": "boolean"},
                "summary": {"type": "string"},
                "greeting": {"type": "string"},
                "steps": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "status": {"type": "string", "enum": [
                            "does_alone", "needs_reminder", "needed_help", "no_info"]},
                        "say": {"type": "string"},
                        "cue": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["id", "status"],
                }},
            },
            "required": ["has_history", "steps"],
        }
        insight = self.ltm.reflect(
            f"I am about to guide {self.wearer} through {self.task['name']}, one "
            f"step at a time. The generic steps are:\n{steps}\n\n"
            f"Using the guide logs and anything {self.wearer} has said about it, "
            "classify every step by how it went in the MOST RECENT sessions: "
            "does_alone (recent logs say they did it without help), "
            "needs_reminder (they do it but recently needed a prompt), "
            "needed_help (they recently got stuck), or no_info. For "
            "needs_reminder and needed_help give in 'cue' the exact prompt or "
            "fact that helped (e.g. where something is kept), never an invented "
            f"one. 'say' rewrites the step in at most {MAX_WORDS} words using "
            "their own things and preferences from memory (their mug, how they "
            "take it), else leave it empty. 'evidence' quotes the memory. "
            "'summary' is one sentence for a carer. 'greeting' is at most "
            f"{MAX_WORDS} words, warm, using their name. has_history is false "
            "if there are no logs of this task.",
            context=f"Today is {self.now:%A %d %B %Y}.",
            schema=schema)
        data = insight and insight.get("data")
        planned = self.validate(data) if data and data.get("has_history") else None
        if planned:
            self.steps, self.source = planned, "memory"
            self.memory, self.facts = data.get("summary") or insight["text"], insight["facts"]
        else:
            self.steps = [self._generic(sid) for sid in self.template]
        return self.steps

    def validate(self, data):
        """Turn Hindsight's per-step classification into a plan, keeping only
        what the template allows. None if there is nothing usable."""
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            return None
        verdicts = {}
        for raw in data["steps"]:
            if isinstance(raw, dict) and raw.get("id") in self.template:
                verdicts.setdefault(raw["id"], raw)
        if not verdicts:
            return None

        out = []
        greeting = clean(data.get("greeting"))
        if greeting and words(greeting) <= MAX_WORDS and not UNSAFE.search(greeting):
            out.append({"id": "intro", "say": greeting, "cue": "", "why": ""})
        for sid, (text, must) in self.template.items():
            v = verdicts.get(sid, {})
            status = v.get("status", "no_info")
            if status == "does_alone" and not must:
                continue                       # they don't need telling any more
            say, cue = clean(v.get("say")), clean(v.get("cue"))
            if not say or words(say) > MAX_WORDS or UNSAFE.search(say):
                say = text
            helped = status in ("needs_reminder", "needed_help")
            if not helped or words(cue) > MAX_WORDS or UNSAFE.search(cue) \
                    or cue.lower().rstrip(".") in say.lower():
                cue = ""                       # only cues memory says worked
            out.append({"id": sid, "say": say, "cue": cue,
                        "why": clean(v.get("evidence")) if status != "no_info" else ""})
        return out

    def _generic(self, sid):
        return {"id": sid, "say": self.template[sid][0], "cue": "", "why": ""}

    # ---- running ----------------------------------------------------------

    def current(self):
        return self.steps[self.index] if not self.finished else None

    def handle(self, word):
        """Apply a control word. Returns 'step', 'help', 'done' or 'stopped'."""
        if word == "stop":
            self.finish(stopped=True)
            return "stopped"
        if word == "help":
            sid = self.current()["id"]
            self.help_at[sid] = self.help_at.get(sid, 0) + 1
            return "help"
        self.index += 1
        if self.index >= len(self.steps):
            self.finish()
            return "done"
        return "step"

    def card(self, cols=21, rows=8, helping=False):
        """The OLED text for the current step. A step memory flagged as hard
        shows its cue straight away; asking for help shows it again, or
        repeats the step if there is no cue."""
        if self.finished:
            return "\n".join([f"{self.task['title']} - done", "", "Well done!"])
        step = self.current()
        real = [s for s in self.steps if s["id"] != "intro"]
        n = real.index(step) + 1 if step in real else 0
        head = f"{self.task['title']} {n}/{len(real)}" if n else self.task["title"]
        if helping:
            body = step["cue"] or f"Again: {step['say']}"
        else:
            body = f"{step['say']} {step['cue']}".strip()
        lines = [head] + textwrap.wrap(body, cols)[: rows - 2] + ["Touch when done"]
        return "\n".join(lines)

    def finish(self, stopped=False, when=None):
        """Retain one summary of this session, in the background."""
        if self.finished:
            return None
        self.finished = True
        done = [s["id"] for s in self.steps[: self.index]]
        helped = []
        for sid, n in self.help_at.items():
            text = self.template.get(sid, ("the greeting", False))[0].rstrip(".").lower()
            cue = self._cue(sid)
            helped.append(f"'{text}' ({n}x" + (f", the cue '{cue}' was shown" if cue else "") + ")")
        closing = list(self.template)[-1]
        alone = [self.template[sid][0].rstrip(".").lower() for sid in done
                 if sid in self.template and sid not in self.help_at and sid != closing]
        mins = max(1, round((time.time() - self.started) / 60))
        summary = (
            f"Guide log for {self.wearer}, {self.task['name']} on "
            f"{(when or self.now):%A %d %B}: "
            + ("stopped early. " if stopped else f"finished in about {mins} min. ")
            + (f"Needed help with {'; '.join(helped)}. " if helped else "Needed no help. ")
            + (f"Did these alone: {'; '.join(alone)}." if alone else "")
        )
        self.summary = summary
        self.ltm.retain(summary, context="task outcome", tags=[task_tag(self.key)],
                        when=when or self.now)
        return summary

    def _cue(self, sid):
        for s in self.steps:
            if s["id"] == sid:
                return s["cue"]
        return ""


def describe_plan(session):
    """Plain-text plan, for logs and the demo."""
    lines = []
    real = 0
    for s in session.steps:
        if s["id"] != "intro":
            real += 1
        tag = "  " if s["id"] == "intro" else f"{real:>2}"
        line = f"{tag}. {s['say']}"
        if s["cue"]:
            line += f"  [cue: {s['cue']}]"
        lines.append(line)
    return "\n".join(lines)


def start(transcript, ltm, wearer):
    """Convenience for the brain: a planned session for this request, or None."""
    key = detect_task(transcript)
    if key is None:
        return None
    session = GuideSession(key, ltm or hindsight_memory.LongTermMemory(enabled=False), wearer)
    session.plan()
    return session
