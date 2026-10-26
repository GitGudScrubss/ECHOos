"""
Twenty days in the life of Margaret Ellis, 78, who has early-stage
Alzheimer's and lives alone at 14 Maple Court with a daily visit from family
or her neighbour. Synthetic, but written the way her glasses would hear it:
first person, half-sentences, small talk mixed with the things that matter.

Each event is (day, kind, who, place, text). Day 20 is "today" in the demo;
timestamps are back-dated from now so Hindsight sees a real timeline.

    kind = conversation   someone talked with Margaret (what the brain retains
                          after recognising their face)
           note           Margaret said something to the glasses herself;
                          `who` is a person the note mentions, used for tags
"""

import re
from datetime import datetime, timedelta, timezone

WEARER = "Margaret"
TODAY = 20

EVENTS = [
    # ---- week 1 -------------------------------------------------------------
    (1, "conversation", "Priya", "home",
     "Hi Mum, it's Priya. I've put the new glasses on the charger by your bed. "
     "I'll come every Sunday afternoon now that the job in Pune is finished."),
    (1, "conversation", "Priya", "home",
     "Arjun's school play is on Friday the 6th at four o'clock, he's playing the "
     "lion. I'll pick you up at half past three."),
    (2, "conversation", "Mr. Okafor", "home",
     "Morning Margaret, it's Daniel from number 12. I'll water your tomato plants "
     "while the heatwave lasts, I have the spare key you gave me."),
    (3, "conversation", "Dr. Rao", "City Neuro Clinic",
     "Your memory scores are stable since March. I'd like to see you again in "
     "three weeks, on Wednesday the 28th at 10:30, and bring your medication list."),
    (3, "note", None, "City Neuro Clinic",
     "The receptionist at the clinic is called Joy, she has the red glasses."),
    (5, "conversation", "Priya", "home",
     "I've written your appointments on the calendar on the fridge. Dad's "
     "birthday would have been on the 11th, we could go to the garden at St. "
     "Mary's together if you'd like."),
    (6, "conversation", "Arjun", "school hall",
     "Grandma you came! Did you see me roar? Mum says you can have the lion "
     "mask, I made it myself."),

    # ---- week 2 -------------------------------------------------------------
    (8, "conversation", "Mr. Okafor", "home",
     "Thank you for the casserole, I'll bring the blue dish back on Thursday. "
     "My granddaughter Ada starts university in Leeds next month."),
    (8, "note", "Arjun", "home",
     "I like my tea with milk and one sugar, in the yellow mug Arjun painted."),
    (10, "conversation", "Sunita", "home",
     "Hello Margaret, I'm Sunita, the community nurse. I'll come on Tuesdays to "
     "check your blood pressure. Today it was a little high, 150 over 90."),
    (11, "conversation", "Priya", "St. Mary's garden",
     "Dad loved these roses. Next Sunday I'll bring tomato soup, the one you "
     "like with basil, and we can sort the old photographs."),
    (13, "note", "Mr. Okafor", "home",
     "Daniel brought the blue casserole dish back. I put it in the bottom cupboard."),

    # ---- week 3 -------------------------------------------------------------
    (15, "conversation", "Priya", "home",
     "Here's the soup, Mum. We found the photo of you and Dad at Brighton in "
     "1971. Arjun has a cold so he stayed home."),
    (15, "conversation", "Priya", "home",
     "Remember the pharmacy changed: prescriptions now come from Boots on "
     "Station Road, not the one by the post office. They deliver on Mondays."),
    (17, "conversation", "Sunita", "home",
     "Blood pressure is better today, 135 over 85. Keep walking to the corner "
     "shop in the mornings, it's doing you good."),
    (17, "conversation", "Mr. Okafor", "home",
     "The tomatoes are ready, I put six on your kitchen windowsill. Ada sent a "
     "postcard from Leeds, she loves it."),
    (18, "note", None, "home",
     "I left my house keys in the blue bowl by the front door."),
    (18, "conversation", "Priya", "phone",
     "Mum, I'll take you to Dr. Rao on Wednesday, I'll be there at ten. Don't "
     "forget the medication list, it's in the folder on the fridge."),
    (19, "conversation", "Arjun", "phone",
     "Grandma my cold is gone. Can we make the lemon biscuits on Sunday? I "
     "want to bring some to school for Mr. Hale."),
]


def slug(text):
    # Same rule as hindsight_memory.slug, so tags line up with the brain's.
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def when(day, now=None):
    """Back-date a story day so that day TODAY is now (late afternoon)."""
    now = now or datetime.now(timezone.utc)
    base = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return base - timedelta(days=TODAY - day)


def as_memory(event):
    """The text, tags and context the brain would retain for this event."""
    day, kind, who, place, text = event
    if kind == "conversation":
        content = f"{who} talked with {WEARER} at {place}. {who} said: {text}"
    else:
        content = f"{WEARER} said: {text}"
    tags = []
    if who:
        tags.append(f"person:{slug(who)}")
    if place:
        tags.append(f"place:{slug(place)}")
    return content, kind, tags


def events_until(day):
    return [e for e in EVENTS if e[0] <= day]
