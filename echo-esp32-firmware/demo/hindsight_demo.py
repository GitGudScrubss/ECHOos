"""
Hardware-free demo: the ECHO brain with and without Hindsight memory.

Runs the real echo_brain decision code (meet / ask / person_card) against
twenty days of Margaret's life (story.py), and prints what the 21x8 OLED on
her glasses would show. No ESP32, camera or microphone needed; faces are
taken as already recognised.

    python demo/hindsight_demo.py --seed      # one-off: load the 20-day story
    python demo/hindsight_demo.py             # same moments, memory OFF vs ON
    python demo/hindsight_demo.py --curve     # watch it learn: day 1, 5, 20
    python demo/hindsight_demo.py --reset     # delete the demo bank

Needs HINDSIGHT_API_KEY (or HINDSIGHT_API_KEY.env in the repo root) for the
ON side. An LLM (GROQ_API_KEY, or a local Ollama model) only shortens lines
for the tiny screen; with ECHO_LLM=off the facts are trimmed mechanically.
The demo uses its own bank and a throwaway local store, so it never touches
the wearer's real memory.
"""

import argparse
import copy
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("ECHO_WEARER_NAME", "Margaret")

import echo_brain as brain          # noqa: E402
import hindsight_memory             # noqa: E402
import llm_client                   # noqa: E402
import story                        # noqa: E402

DEFAULT_BANK = os.environ.get("HINDSIGHT_BANK", "echo-demo-margaret")

# What the device's own memory.json would hold after these twenty days: dates
# of first meeting and the last few 19-character bullets, nothing else.
LOCAL_BEFORE = {
    "Priya": {"first_met_place": "home", "encounters": 6,
              "key_points": ["glasses on charger", "Sunday visits", "Arjun school play",
                             "calendar on fridge", "Dad's birthday", "tomato soup basil",
                             "Brighton photo", "pharmacy changed"]},
    "Mr. Okafor": {"first_met_place": "home", "encounters": 3,
                   "key_points": ["waters tomatoes", "blue dish Thursday",
                                  "Ada to Leeds", "tomatoes ready"]},
}

# The moments the demo replays on day 20.
SCENES = [
    ("face", "Priya", "home", "Hi Mum, I'm here. Have you had lunch yet?"),
    ("ask", None, "home", "When is my appointment with the doctor?"),
    ("ask", None, "home", "Where did I leave my house keys?"),
    ("face", "Mr. Okafor", "home", "Afternoon Margaret, lovely day for it."),
    ("ask", None, "home", "Which pharmacy do my prescriptions come from now?"),
]

CURVE_DAYS = (1, 5, 20)
CURVE_SCENES = [SCENES[0], SCENES[1], SCENES[2]]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

W = brain.OLED_COLS


def box(text, title):
    lines = (text or "").split("\n")[:brain.OLED_ROWS]
    lines += [""] * (brain.OLED_ROWS - len(lines))
    out = [f" {title[:W + 1]:<{W + 1}}", "┌" + "─" * W + "┐"]
    out += ["│" + l[:W].ljust(W) + "│" for l in lines]
    out.append("└" + "─" * W + "┘")
    return out


def side_by_side(*boxes, gap="   "):
    return "\n".join(gap.join(row) for row in zip(*boxes))


def facts_footer(recalled, seconds, limit=3):
    if recalled is None:
        return f"   Hindsight: no answer ({brain.LTM.last_error or 'off'})"
    head = f"   Hindsight recalled {len(recalled)} fact(s) in {seconds:.1f}s"
    rows = [head] + [f"     · [{f['when']}] {f['text'].split(' | ')[0]}"[:110]
                     for f in recalled[:limit]]
    return "\n".join(rows)


def heading(scene):
    kind, who, place, said = scene
    if kind == "face":
        return f"{who} walks in and says: \"{said}\""
    return f"Margaret asks: \"{said}\""


# ---------------------------------------------------------------------------
# Running the real brain code
# ---------------------------------------------------------------------------

def setup_local_store(day, with_points=True):
    """A fresh throwaway memory.json, as the device would have it on `day`."""
    brain.MEMORY_FILE = os.path.join(tempfile.mkdtemp(prefix="echo-demo-"), "memory.json")
    brain.MEMORY = {"people": {}, "places": []}
    for name, rec in copy.deepcopy(LOCAL_BEFORE).items():
        rec["first_met"] = story.when(1).isoformat()
        if not with_points:
            rec["key_points"] = []
        brain.MEMORY["people"][name] = rec


def run_scene(scene, ltm, day=story.TODAY):
    """Returns (oled_text, recalled_facts, seconds)."""
    brain.LTM = ltm
    # On the glasses, meet() retains each new conversation. The demo replays
    # the same moments on every run, so it reads memory but never writes it.
    brain.remember = lambda *args, **kwargs: None
    kind, who, place, said = scene
    t = time.time()
    if kind == "face":
        text, _points, recalled = brain.meet(who, place, said, when=story.when(day))
    else:
        text, recalled = brain.ask(said, when=story.when(day))
    return text, recalled, time.time() - t


def make_ltm(bank, enabled=True):
    return hindsight_memory.LongTermMemory(bank=bank, enabled=enabled, wearer=story.WEARER)


def seed(ltm, events):
    with ltm.collect():
        for i, event in enumerate(events):
            content, context, tags = story.as_memory(event)
            ltm.retain(content, context=context, tags=tags,
                       when=story.when(event[0]),
                       document_id=f"margaret-day{event[0]:02d}-{i:02d}")
    return ltm.last_error


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_compare(bank, sides):
    off, on = make_ltm(bank, enabled=False), make_ltm(bank)
    if "on" in sides and not on.enabled:
        print(f"! {on.describe()} — showing memory OFF only.\n")
        sides = ["off"]

    print(f"Margaret Ellis, 78, early-stage Alzheimer's. Day {story.TODAY} of the story.")
    print(f"Memory bank: {bank}    LLM: {llm_client.backend() or 'none (rule-based)'}\n")
    for n, scene in enumerate(SCENES, 1):
        print(f"── {n}. {heading(scene)}")
        boxes, footer = [], ""
        if "off" in sides:
            setup_local_store(story.TODAY)
            text, _, _ = run_scene(scene, off)
            boxes.append(box(text, "memory OFF (today)"))
        if "on" in sides:
            setup_local_store(story.TODAY)
            text, recalled, secs = run_scene(scene, on)
            boxes.append(box(text, "memory ON (Hindsight)"))
            footer = facts_footer(recalled, secs)
        print(side_by_side(*boxes))
        if footer:
            print(footer)
        print()
    on.close()


def mode_curve(bank):
    """Replay the story into a fresh bank and ask the same things on day 1,
    day 5 and day 20. Only memory changes between the columns."""
    ltm = make_ltm(bank)
    if not ltm.enabled:
        sys.exit(f"! {ltm.describe()}")
    print(f"Fresh bank '{bank}'. Replaying Margaret's life and asking the same "
          "questions as it grows.\n")
    ltm.delete_bank()

    columns = {i: [] for i in range(len(CURVE_SCENES))}
    done = 0
    for day in CURVE_DAYS:
        new = [e for e in story.EVENTS if done < e[0] <= day]
        t = time.time()
        err = seed(ltm, new)
        total = len(story.events_until(day))
        print(f"  day {day:>2}: retained {len(new)} new memories ({total} total) "
              f"in {time.time() - t:.0f}s" + (f"  ! {err}" if err else ""))
        done = day
        for i, scene in enumerate(CURVE_SCENES):
            setup_local_store(day, with_points=False)
            text, _, _ = run_scene(scene, ltm, day=day)
            columns[i].append(box(text, f"day {day} · {total} memories"))
    print()
    for i, scene in enumerate(CURVE_SCENES):
        print(f"── {heading(scene)}")
        print(side_by_side(*columns[i]))
        print()
    ltm.close()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bank", default=DEFAULT_BANK)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--seed", action="store_true", help="load the 20-day story into the bank")
    g.add_argument("--reset", action="store_true", help="delete the demo bank")
    g.add_argument("--curve", action="store_true", help="learning curve: day 1, 5, 20")
    g.add_argument("--off", action="store_true", help="memory OFF column only")
    g.add_argument("--on", action="store_true", help="memory ON column only")
    a = p.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if a.seed or a.reset:
        ltm = make_ltm(a.bank)
        if not ltm.enabled:
            sys.exit(f"! {ltm.describe()}")
        if a.reset:
            print("deleted" if ltm.delete_bank() else f"! {ltm.last_error}")
        else:
            t = time.time()
            err = seed(ltm, story.EVENTS)
            print(f"retained {len(story.EVENTS)} memories into '{a.bank}' in "
                  f"{time.time() - t:.0f}s" + (f"  ! {err}" if err else ""))
        ltm.close()
    elif a.curve:
        mode_curve(f"{a.bank}-curve")
    else:
        mode_compare(a.bank, ["off"] if a.off else ["on"] if a.on else ["off", "on"])


if __name__ == "__main__":
    main()
