#!/usr/bin/env python3
"""
ECHO brain — the laptop side that makes the sensor node intelligent.

What one touch does:

  1. The board sends a photo, 5s of audio, and a list of the WiFi access
     points it can see (PROTOCOL.md sensor packet v2, msg_type 0x04).
  2. Whisper transcribes the audio locally.
  3. What happens next depends on what was said:

       "hello my name is Satyabrat"  -> enrol this face under that name
       "we are at the college lab"   -> name the place we're standing in
       "who is this guy"             -> identify + recall everything
       "when is my doctor's visit"   -> answer from long-term memory
       "I want to make a cup of tea" -> step-by-step guide (guide_agent.py);
                                        each touch: done / help / stop
       anything else                 -> identify, and log what was discussed

  4. The answer goes back to the board as display text (msg_type 0x03) and
     appears on the OLED, and simultaneously on http://localhost:8080.

Places are recognised by which WiFi routers are visible — no GPS needed,
and it works indoors where GPS would not. Key points are summarised by a
local Ollama model. Everything runs offline once the models are pulled.

With ECHO_HINDSIGHT=on, every conversation is also retained in a Hindsight
memory bank (text only — never faces, audio or WiFi data), and recalled the
next time that person appears or the wearer asks about the past. See
hindsight_memory.py. Off, or unreachable, the brain behaves exactly as before.

    python echo_brain.py
"""

import html
import json
import os
import re
import shutil
import socket
import struct
import sys
import threading
import textwrap
import time
import wave
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import guide_agent
import hindsight_memory
import llm_client

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

HOST, PORT = "0.0.0.0", 7890
WEB_PORT = 8080

MAGIC = b"ECH1"
MSG_SENSOR_V1 = 0x01
MSG_AUDIO_REPLY = 0x02
MSG_RICH_REPLY = 0x03      # display text + audio
MSG_SENSOR_V2 = 0x04       # image + audio + AP fingerprint
MSG_ERROR = 0xFF
MAX_FRAME_BYTES = 2 * 1024 * 1024

SAMPLE_RATE = 16000
CAPTURE_DIR = "captures"
FACES_DIR = "faces"
MEMORY_FILE = "memory.json"

WHISPER_MODEL = "base"
FACE_MODEL = "SFace"
FACE_DETECTOR = "opencv"
FACE_DISTANCE_MAX = 0.60

# Who is wearing the glasses — used in memory text ("Priya visited Margaret").
WEARER = os.environ.get("ECHO_WEARER_NAME", "the wearer")

# The OLED is 21 characters by 8 lines. Everything sent for display is
# composed against that budget — there is no scrolling on a 0.96" screen.
OLED_COLS, OLED_ROWS = 21, 8

# Two places count as the same spot when this much of their strongest-AP
# sets overlap. 0.35 is deliberately forgiving: routers come and go between
# scans, phones hotspotting nearby appear and vanish, and being told the
# wrong room is better than being told nothing.
PLACE_MATCH_THRESHOLD = 0.35

INTENT_WHO = [
    r"\bwho is (this|that|he|she|they|him|her)\b",
    r"\bwho'?s (this|that|he|she)\b",
    r"\bdo (i|we) know (this|him|her|them)\b",
    r"\bwho am i (talking|speaking) to\b",
    r"\bremind me who\b",
]
INTENT_NAME = [
    r"\bmy name is\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)",
    r"\bi am\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)",
    r"\bi'm\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)",
    r"\bthis is\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)\s+(?:here|speaking)?$",
    r"\bcall me\s+([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*)?)",
]
# A spoken question about the past or the wearer's own life. Checked only
# after the "who is this" patterns, and before the name patterns so that
# "where did I put my keys, I am worried" doesn't enrol someone called Worried.
INTENT_ASK = [
    r"^(?:um+ |uh+ |so |and |hey |echo,? )*(when|where|what|did|has|have|who|which|how)\b",
    r"\bremind me (?:what|when|where|about)\b",
    r"\bdo (?:i|we) have (?:an?|any)\b",
]
INTENT_PLACE = [
    r"\bwe(?:'re| are)? (?:at|in)\s+(?:the\s+)?([a-z][a-z0-9'\- ]{1,28})",
    r"\bthis (?:place )?is\s+(?:the\s+)?([a-z][a-z0-9'\- ]{1,28})\s*(?:lab|office|room|cafe|campus|home)\b",
    r"\bremember this place as\s+(?:the\s+)?([a-z][a-z0-9'\- ]{1,28})",
    r"\bi(?:'m| am) (?:at|in)\s+(?:the\s+)?([a-z][a-z0-9'\- ]{1,28})",
]

NOT_NAMES = {
    "a", "an", "the", "here", "there", "fine", "good", "ok", "okay", "not",
    "going", "doing", "sorry", "very", "just", "so", "really", "back",
    "ready", "done", "sure", "happy", "tired", "hungry", "testing", "test",
    "talking", "speaking", "trying", "looking", "working", "thinking",
    "thanks", "thank", "glad", "great", "well", "still", "also", "now",
}

# ----------------------------------------------------------------------
# Memory — people, places, and what was said
# ----------------------------------------------------------------------

MEMORY = {"people": {}, "places": []}
MEM_LOCK = threading.Lock()


def load_memory():
    global MEMORY
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                MEMORY = json.load(f)
            MEMORY.setdefault("people", {})
            MEMORY.setdefault("places", [])
        except (OSError, ValueError) as e:
            print(f"  ! could not read {MEMORY_FILE} ({e}) — starting fresh")


def save_memory():
    tmp = MEMORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(MEMORY, f, indent=2, ensure_ascii=False)
        os.replace(tmp, MEMORY_FILE)      # atomic, so a crash can't corrupt it
    except OSError as e:
        print(f"  ! could not save memory: {e}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# Long-term memory (Hindsight). LTM.enabled is False unless ECHO_HINDSIGHT=on
# and a key is configured; every call below is then a no-op returning None.
# Never call it while holding MEM_LOCK — it is not reentrant and a network
# call must not block the save path.
LTM = hindsight_memory.LongTermMemory(wearer=WEARER)


def mentioned_people(text):
    """Enrolled people named in a transcript, so 'Daniel brought the dish
    back' is found when Daniel's face next appears."""
    with MEM_LOCK:
        names = list(MEMORY["people"])
    low = (text or "").lower()
    return [n for n in names if re.search(rf"\b{re.escape(n.lower())}\b", low)]


def remember(text, context, who=None, place=None, when=None):
    """Retain one line of the wearer's life, in the background."""
    people = ([who] if who else []) + [n for n in mentioned_people(text) if n != who]
    tags = [hindsight_memory.person_tag(p) for p in people]
    if place:
        tags.append(f"place:{hindsight_memory.slug(place)}")
    LTM.retain(text, context=context, tags=tags, when=when)


def human_when(iso):
    """'23 Aug, 5 days ago' — short enough for a 21-column display."""
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - then).days
    stamp = then.astimezone().strftime("%d %b")
    if days <= 0:
        return f"{stamp}, today"
    if days == 1:
        return f"{stamp}, yesterday"
    if days < 30:
        return f"{stamp}, {days}d ago"
    return stamp


# ----------------------------------------------------------------------
# Places, by WiFi fingerprint
# ----------------------------------------------------------------------

def jaccard(a, b):
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_place(bssids):
    """Which known place do these visible routers correspond to?"""
    best, best_score = None, 0.0
    with MEM_LOCK:
        for place in MEMORY["places"]:
            score = jaccard(bssids, place.get("bssids", []))
            if score > best_score:
                best, best_score = place, score
    if best and best_score >= PLACE_MATCH_THRESHOLD:
        return best["name"], best_score
    return None, best_score


def remember_place(name, bssids):
    """Name the spot we're standing in. Updating an existing place merges
    the router sets, so a place slowly learns all the APs around it."""
    with MEM_LOCK:
        for place in MEMORY["places"]:
            if place["name"].lower() == name.lower():
                merged = set(place.get("bssids", [])) | set(bssids)
                place["bssids"] = sorted(merged)
                place["updated"] = now_iso()
                save_memory()
                return "updated"
        MEMORY["places"].append({
            "name": name,
            "bssids": sorted(set(bssids)),
            "created": now_iso(),
        })
        save_memory()
    return "created"


# ----------------------------------------------------------------------
# Whisper
# ----------------------------------------------------------------------

_whisper, _whisper_failed = None, False


def get_whisper():
    global _whisper, _whisper_failed
    if _whisper is not None or _whisper_failed:
        return _whisper
    try:
        from faster_whisper import WhisperModel
        print(f"  loading whisper '{WHISPER_MODEL}' (first run downloads it)...")
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("  whisper ready")
    except ImportError:
        _whisper_failed = True
        print("  ! faster-whisper not installed:  pip install faster-whisper")
    except Exception as e:
        _whisper_failed = True
        print(f"  ! whisper failed to load: {e}")
    return _whisper


def transcribe(wav_path):
    model = get_whisper()
    if model is None:
        return ""
    try:
        segments, _ = model.transcribe(wav_path, language="en", beam_size=1)
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"  ! transcription failed: {e}")
        return ""


# ----------------------------------------------------------------------
# LLM — key points and memory cards (llm_client: Groq or local Ollama)
# ----------------------------------------------------------------------

def _bullets(out, limit=3, width=19):
    bullets = []
    for line in (out or "").splitlines():
        line = line.strip().lstrip("-*•0123456789. ").strip()
        # Small models like to open with "Here are the bullets:".
        if not line or line.endswith(":") or line.lower().startswith("here are"):
            continue
        bullets.append(line[:width])
    return bullets[:limit]


def summarise(transcript, previously=None):
    """Two or three very short bullets that fit a 21-column display.

    `previously` is what long-term memory already holds about this person, so
    the bullets record what is new instead of repeating last week's."""
    if not transcript or len(transcript.split()) < 4:
        return []

    if llm_client.backend() is None:
        # No LLM: take the longest sentence as the gist. Crude but honest.
        parts = [p.strip() for p in re.split(r"[.!?]", transcript) if p.strip()]
        return [max(parts, key=len)[:60]] if parts else []

    history = ""
    if previously:
        history = ("Already known from earlier conversations (do not repeat "
                   "these, only note what is new):\n" +
                   "\n".join(f"- {p}" for p in previously[:6]) + "\n\n")
    prompt = (
        "Summarise what was discussed in this conversation snippet as at most "
        "3 bullet points. Each bullet MUST be under 19 characters — they are "
        "displayed on a tiny screen. No punctuation at the end. Reply with "
        "ONLY the bullets, one per line, no numbering, no preamble.\n\n"
        f"{history}Snippet: {transcript}"
    )
    return _bullets(llm_client.generate(prompt, max_tokens=80))


def wrap_rows(text, rows, width=OLED_COLS):
    """Word-wrap onto at most `rows` lines; mark a cut with '..'."""
    lines = textwrap.wrap(" ".join((text or "").split()), width)
    if len(lines) > rows:
        lines = lines[:rows]
        lines[-1] = lines[-1][: width - 2].rstrip() + ".."
    return lines


def memory_style(words, now=None):
    """Context for Hindsight reflect: who is asking, today's date (so "next
    Wednesday" resolves), and how much fits on the glasses."""
    now = now or datetime.now()
    return (f"Today is {now:%A %d %B %Y}. You are speaking to {WEARER} through "
            f"a tiny screen: reply in at most {words} words, plain and calm, "
            "addressing them as 'you'. Say dates as weekday and day, e.g. "
            "'Wednesday 28th at 10:30'. Most useful first: promises, plans "
            "and dates beat small talk. Use the newest facts: before "
            "mentioning a promise or plan, check whether a later memory says "
            "it was already done or has passed, and if so leave it out. No "
            "lists, no preamble.")


# ----------------------------------------------------------------------
# Faces
# ----------------------------------------------------------------------

def _deepface():
    try:
        from deepface import DeepFace
        return DeepFace
    except ImportError:
        print("  ! deepface not installed:  pip install deepface tf-keras")
        return None
    except Exception as e:
        print(f"  ! deepface import failed: {e}")
        return None


def _invalidate_face_cache():
    if not os.path.isdir(FACES_DIR):
        return
    for n in os.listdir(FACES_DIR):
        if n.endswith(".pkl"):
            try:
                os.remove(os.path.join(FACES_DIR, n))
            except OSError:
                pass


def enrolled_names():
    if not os.path.isdir(FACES_DIR):
        return []
    return sorted(d for d in os.listdir(FACES_DIR)
                  if os.path.isdir(os.path.join(FACES_DIR, d)))


def enroll_face(name, jpg_path):
    DeepFace = _deepface()
    if DeepFace is None:
        return False, "deepface not installed"
    try:
        faces = DeepFace.extract_faces(img_path=jpg_path,
                                       detector_backend=FACE_DETECTOR,
                                       enforce_detection=True)
        if not faces:
            return False, "no face in the photo"
    except Exception:
        return False, "no face in the photo"

    d = os.path.join(FACES_DIR, name)
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(jpg_path, os.path.join(d, f"{int(time.time())}.jpg"))
    _invalidate_face_cache()
    n = len([f for f in os.listdir(d) if f.endswith(".jpg")])
    return True, f"{n} photo(s) on file"


def identify_face(jpg_path):
    DeepFace = _deepface()
    if DeepFace is None:
        return None, "deepface not installed"
    if not enrolled_names():
        return None, "nobody enrolled yet"
    try:
        results = DeepFace.find(img_path=jpg_path, db_path=FACES_DIR,
                                model_name=FACE_MODEL,
                                detector_backend=FACE_DETECTOR,
                                enforce_detection=False, silent=True)
    except Exception as e:
        return None, f"lookup failed: {e}"

    frames = [df for df in results if df is not None and not df.empty]
    if not frames:
        return None, "no match"

    df = frames[0]
    col = next((c for c in df.columns if "distance" in c.lower()), None)
    if col is None:
        return None, "unexpected deepface result"

    best = df.sort_values(col).iloc[0]
    dist = float(best[col])
    if dist > FACE_DISTANCE_MAX:
        return None, f"match too weak ({dist:.2f})"
    return os.path.basename(os.path.dirname(str(best["identity"]))), dist


# ----------------------------------------------------------------------
# Intent
# ----------------------------------------------------------------------

def clean_name(raw):
    """Turn a captured phrase into a name, or None if it clearly isn't one.

    The first word decides it. 'I am fine thanks' must NOT enrol a person —
    filtering stop-words out of the middle would leave 'Thanks' and quietly
    create a bogus identity, so a non-name in first position rejects the
    whole match. Trailing junk is trimmed instead ('Bob and then' -> 'Bob')."""
    words = raw.strip().split()
    if not words or words[0] in NOT_NAMES:
        return None
    kept = []
    for w in words:
        if w in NOT_NAMES:
            break
        kept.append(w)
    if not kept:
        return None
    return " ".join(w.capitalize() for w in kept)


def detect_intent(transcript):
    """Returns (intent, argument). Order matters: an explicit question about
    identity beats a name pattern, so 'who is this, is it Satyabrat' asks
    rather than enrols."""
    if not transcript:
        return "identify", None
    text = transcript.lower()

    for p in INTENT_WHO:
        if re.search(p, text):
            return "who", None
    task = guide_agent.detect_task(transcript)
    if task:
        return "guide", task
    for p in INTENT_ASK:
        if re.search(p, text):
            return "ask", transcript.strip()
    for p in INTENT_PLACE:
        m = re.search(p, text)
        if m:
            name = clean_name(m.group(1))
            if name:
                return "place", name
    for p in INTENT_NAME:
        m = re.search(p, text)
        if m:
            name = clean_name(m.group(1))
            if name:
                return "name", name
    return "identify", None


# ----------------------------------------------------------------------
# Display composition — hard budget of 21 x 8
# ----------------------------------------------------------------------

def fit(line, width=OLED_COLS):
    line = " ".join(line.split())
    return line if len(line) <= width else line[: width - 1] + "."


def compose(lines):
    return "\n".join(fit(l) for l in lines[:OLED_ROWS] if l is not None)


def person_card(name, place_now, reminder=None):
    """Everything the device knows about this person, squeezed onto 8 lines.

    `reminder` is what long-term memory (Hindsight) says matters about them.
    When present it replaces the local 'Talked about' bullets, which only
    ever hold the last eight 19-character fragments with no dates."""
    with MEM_LOCK:
        rec = MEMORY["people"].get(name, {})
        met_place = rec.get("first_met_place")
        met_when = rec.get("first_met")
        points = list(rec.get("key_points", []))
        seen = rec.get("encounters", 0)

    lines = [f"This is {name}"]

    if met_place:
        lines.append(f"Met at {met_place}")
    if met_when:
        w = human_when(met_when)
        if w:
            lines.append(w + (f" - {seen}x" if seen > 1 else ""))
    if place_now and place_now != met_place:
        lines.append(f"Now: {place_now}")

    if reminder:
        lines.extend(wrap_rows(reminder, OLED_ROWS - len(lines)))
    elif points:
        lines.append("Talked about:")
        for p in points[-3:]:
            lines.append(f"- {p}")
    return compose(lines)


def answer_card(answer):
    """The wearer asked about the past. Answer only from memory, or say so."""
    if not answer:
        return compose(["I don't have a", "record of that.", "",
                        "Ask someone you", "trust to check."])
    return compose(["From your memory:"] + wrap_rows(answer, OLED_ROWS - 1))


# ----------------------------------------------------------------------
# Live page state
# ----------------------------------------------------------------------

STATE = {
    "headline": "Waiting for the first touch",
    "detail": "Touch the sensor and say: hello my name is Satyabrat",
    "transcript": "", "image": None, "cycle": 0, "when": "",
    "kind": "idle", "oled": "", "recall": "",
}
STATE_LOCK = threading.Lock()


def set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)
        STATE["when"] = datetime.now().strftime("%H:%M:%S")


def get_state():
    with STATE_LOCK:
        return dict(STATE)


# ----------------------------------------------------------------------
# Wire protocol
# ----------------------------------------------------------------------

def recv_exactly(sock, n):
    chunks, remaining = [], n
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise ConnectionError(f"peer closed with {remaining} of {n} left")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_len(sock, label):
    (n,) = struct.unpack(">I", recv_exactly(sock, 4))
    if n > MAX_FRAME_BYTES:
        raise ValueError(f"{label}={n} exceeds cap")
    return n


def read_fingerprint(sock):
    """ap_count(u16), then bssid[6] + rssi(int8) + ssid_len(u8) + ssid."""
    (count,) = struct.unpack(">H", recv_exactly(sock, 2))
    if count > 64:
        raise ValueError(f"ap_count={count} implausible")
    aps = []
    for _ in range(count):
        rec = recv_exactly(sock, 8)
        bssid = ":".join(f"{b:02x}" for b in rec[:6])
        (rssi,) = struct.unpack(">b", rec[6:7])
        ssid_len = rec[7]
        ssid = recv_exactly(sock, ssid_len).decode("utf-8", "replace") if ssid_len else ""
        aps.append({"bssid": bssid, "rssi": rssi, "ssid": ssid})
    return aps


def save_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def send_rich_reply(conn, text, pcm=b""):
    """magic, 0x03, text_len, text, sample_rate, audio_len, audio."""
    blob = text.encode("utf-8")[:500]
    if not pcm:
        pcm = b"\x00\x00" * int(SAMPLE_RATE * 0.2)
    conn.sendall(
        MAGIC
        + struct.pack(">B", MSG_RICH_REPLY)
        + struct.pack(">I", len(blob)) + blob
        + struct.pack(">I", SAMPLE_RATE)
        + struct.pack(">I", len(pcm)) + pcm
    )


def banner(headline, detail=""):
    bar = "=" * 64
    print(f"\n{bar}\n  {headline}")
    if detail:
        print(f"  {detail}")
    print(f"{bar}\n")


# ----------------------------------------------------------------------
# Memory-backed decisions (shared by the touch cycle and demo/)
# ----------------------------------------------------------------------

def today():
    return datetime.now().strftime("%A %d %B %Y")


def recall_note(recalled):
    """One line per recalled fact for the :8080 page, which has room the
    OLED doesn't."""
    if recalled is None:
        return "" if not LTM.enabled else f"Hindsight unavailable: {LTM.last_error}"
    return "\n".join((f"[{f['when']}] " if f["when"] else "") + f["text"]
                     for f in recalled[:6])


def meet(who, place, transcript, intent="identify", when=None):
    """A known face is in front of the wearer. Ask memory what matters about
    them right now, note what is new in this conversation, retain it, and
    return (oled_text, new_points, facts_memory_used)."""
    brief = LTM.reflect(
        f"{who} has just walked up to {WEARER}. What should {WEARER} remember "
        f"about {who} right now: who they are to {WEARER}, and any promise, plan "
        "or date involving them?",
        tags=[hindsight_memory.person_tag(who)], context=memory_style(16, when))
    recalled = brief["facts"] if brief else None
    previously = [f["text"] for f in recalled or []]

    points = [] if intent == "who" else summarise(transcript, previously)
    with MEM_LOCK:
        rec = MEMORY["people"].setdefault(who, {})
        rec.setdefault("first_met", now_iso())
        rec.setdefault("first_met_place", place or "somewhere")
        rec["encounters"] = rec.get("encounters", 0) + 1
        rec["last_seen"] = now_iso()
        if points:
            kp = rec.setdefault("key_points", [])
            for p in points:
                if p not in kp:
                    kp.append(p)
            del kp[:-8]        # keep the 8 most recent
        save_memory()

    # The whole transcript goes to memory, not just the 19-char bullets: it is
    # the only record of what was actually said.
    if transcript and intent != "who":
        remember(f"{who} talked with {WEARER}" + (f" at {place}" if place else "") +
                 f". {who} said: {transcript}", "conversation", who=who, place=place,
                 when=when)
    return person_card(who, place, brief and brief["text"]), points, recalled


def ask(question, when=None):
    """The wearer asked about their own past ("when is my doctor's visit?").
    Returns (oled_text, facts_memory_used). With memory off or unreachable
    the card says plainly that there is no record, rather than guessing."""
    answer = LTM.reflect(f"{WEARER} asks: {question}", context=memory_style(22, when))
    if not answer:
        return answer_card(None), None
    return answer_card(answer["text"]), answer["facts"]


# The running step-by-step guide, if any. Touches go to it until it finishes,
# is stopped, or has been left alone for GUIDE_IDLE seconds. Only the accept
# loop touches these, so no lock.
GUIDE = None
GUIDE_LAST_TOUCH = 0.0
GUIDE_IDLE = 10 * 60


def guide_active():
    global GUIDE
    if GUIDE is None or GUIDE.finished:
        return False
    if time.time() - GUIDE_LAST_TOUCH > GUIDE_IDLE:
        GUIDE.finish(stopped=True)            # abandoned; memory notes that too
        GUIDE = None
        return False
    return True


def guide_touch(intent, task, transcript):
    """Start a guide or move the running one on. Returns (oled_text,
    headline, detail, page_note)."""
    global GUIDE, GUIDE_LAST_TOUCH
    GUIDE_LAST_TOUCH = time.time()
    if intent == "guide" and not guide_active():
        GUIDE = guide_agent.GuideSession(task, LTM, WEARER)
        GUIDE.plan()
        n = len([s for s in GUIDE.steps if s["id"] != "intro"])
        head = f"Guide: {GUIDE.task['name']}"
        detail = f"{n} steps, " + ("personalised from memory" if GUIDE.source == "memory"
                                   else "generic (no history yet)")
        note = ((GUIDE.memory + "\n\n") if GUIDE.memory else "") + guide_agent.describe_plan(GUIDE)
        return GUIDE.card(), head, detail, note

    result = GUIDE.handle(guide_agent.control_word(transcript))
    head = f"Guide: {GUIDE.task['name']}"
    if result in ("done", "stopped"):
        text = GUIDE.card() if result == "done" else compose(["Guide stopped.", "", "Touch and ask", "any time."])
        return text, head, f"Guide {result}", GUIDE.summary
    step = GUIDE.current()
    detail = ("Help: " if result == "help" else "Step: ") + step["say"]
    return GUIDE.card(helping=result == "help"), head, detail, step["why"]


# ----------------------------------------------------------------------
# One touch cycle
# ----------------------------------------------------------------------

def handle_cycle(conn, cycle):
    if recv_exactly(conn, 4) != MAGIC:
        print("  ! bad magic — not an ECHO client")
        return
    (msg_type,) = struct.unpack(">B", recv_exactly(conn, 1))
    if msg_type not in (MSG_SENSOR_V1, MSG_SENSOR_V2):
        print(f"  ! unexpected msg_type 0x{msg_type:02x}")
        conn.sendall(MAGIC + struct.pack(">B", MSG_ERROR))
        return

    image = recv_exactly(conn, read_len(conn, "image_len"))
    audio = recv_exactly(conn, read_len(conn, "audio_len"))
    aps = read_fingerprint(conn) if msg_type == MSG_SENSOR_V2 else []

    secs = len(audio) / (SAMPLE_RATE * 2)
    print(f"  image {len(image):,}B   audio {len(audio):,}B ({secs:.1f}s)   {len(aps)} AP(s)")

    os.makedirs(CAPTURE_DIR, exist_ok=True)
    jpg = os.path.join(CAPTURE_DIR, f"cycle_{cycle:03d}.jpg")
    wav = os.path.join(CAPTURE_DIR, f"cycle_{cycle:03d}.wav")
    with open(jpg, "wb") as f:
        f.write(image)
    save_wav(wav, audio)

    bssids = [a["bssid"] for a in aps]
    place, score = match_place(bssids)
    if place:
        print(f"  place: {place} (fingerprint match {score:.0%})")
    elif bssids:
        print(f"  place: unknown (best match {score:.0%})")

    transcript = transcribe(wav)
    print(f'  heard: "{transcript}"' if transcript else "  heard: (nothing usable)")

    intent, arg = detect_intent(transcript)
    print(f"  intent: {intent}" + (f" -> {arg}" if arg else ""))

    # ---- name this place ----
    if intent == "place":
        if not bssids:
            text = compose(["Cannot save place", "No WiFi scan", "from the board"])
            kind, head = "error", "No WiFi scan to fingerprint"
        else:
            what = remember_place(arg, bssids)
            remember(f"{WEARER} named a place '{arg}' on {today()}.", "place", place=arg)
            text = compose([f"Place {what}:", arg, f"{len(bssids)} routers seen"])
            kind, head = "enrolled", f"Place {what}: {arg}"
        banner(head)
        send_rich_reply(conn, text)
        set_state(kind=kind, headline=head, detail=f"{len(bssids)} access points fingerprinted",
                  transcript=transcript, image=jpg, cycle=cycle, oled=text)
        return

    # ---- enrol a person ----
    if intent == "name":
        ok, info = enroll_face(arg, jpg)
        if ok:
            with MEM_LOCK:
                rec = MEMORY["people"].setdefault(arg, {})
                rec.setdefault("first_met", now_iso())
                rec.setdefault("first_met_place", place or "somewhere")
                rec["encounters"] = rec.get("encounters", 0) + 1
                rec.setdefault("key_points", [])
                save_memory()
            remember(f"{WEARER} met {arg} for the first time" +
                     (f" at {place}" if place else "") + f" on {today()}. They said: {transcript}",
                     "introduction", who=arg, place=place)
            text = compose([f"Hello {arg}", "Saved your face", f"Met at {place}" if place else "Place unknown"])
            banner(f"Nice to meet you, {arg}", info)
            set_state(kind="enrolled", headline=f"Nice to meet you, {arg}",
                      detail=f"Enrolled — {info}" + (f", at {place}" if place else ""),
                      transcript=transcript, image=jpg, cycle=cycle, oled=text)
        else:
            text = compose([f"Heard: {arg}", "But no face seen", "Look at camera"])
            banner(f"Heard the name {arg}, but could not enrol", info)
            set_state(kind="error", headline=f"Couldn't enrol {arg}", detail=info,
                      transcript=transcript, image=jpg, cycle=cycle, oled=text)
        send_rich_reply(conn, text)
        return

    # ---- step-by-step guide: start one, or let a running one take the touch ----
    if intent == "guide" or guide_active():
        text, head, detail, note = guide_touch(intent, arg, transcript)
        banner(head, detail)
        send_rich_reply(conn, text)
        set_state(kind="guide", headline=head, detail=detail, recall=note,
                  transcript=transcript, image=jpg, cycle=cycle, oled=text)
        return

    # ---- a question about the past: answer from memory, no face needed ----
    if intent == "ask":
        text, recalled = ask(arg)
        head = "From memory" if recalled else "No record of that"
        banner(head, arg)
        send_rich_reply(conn, text)
        set_state(kind="memory" if recalled else "unknown", headline=head,
                  detail=f'Asked: "{arg}"', recall=recall_note(recalled),
                  transcript=transcript, image=jpg, cycle=cycle, oled=text)
        return

    # ---- identify (explicit question, or just a normal capture) ----
    who, info = identify_face(jpg)

    if who:
        text, points, recalled = meet(who, place, transcript, intent)
        banner(f"This is {who}", f"distance {info:.2f}" if isinstance(info, float) else str(info))
        if points:
            print("  key points: " + " | ".join(points))
        send_rich_reply(conn, text)
        set_state(kind="known", headline=f"This is {who}",
                  detail=(f"Recognised at {place}. " if place else "") +
                         (f"Noted: {', '.join(points)}" if points else "No new key points"),
                  recall=recall_note(recalled),
                  transcript=transcript, image=jpg, cycle=cycle, oled=text)
        return

    # No known face, but something was said ("I left my keys in the blue
    # bowl"). Usually the wearer; it could be a stranger, so it's labelled as
    # heard rather than attributed.
    if transcript and len(transcript.split()) >= 4:
        remember(f"Heard near {WEARER}" + (f" at {place}" if place else "") +
                 f": {transcript}", "overheard", place=place)

    known = enrolled_names()
    text = compose([
        "Unknown person",
        f"At {place}" if place else None,
        "Ask them to say:",
        "my name is ___",
    ])
    banner("I don't know this person yet",
           f"{info} — " + (f"known: {', '.join(known)}" if known else "nobody enrolled yet"))
    send_rich_reply(conn, text)
    set_state(kind="unknown", headline="I don't know this person yet",
              detail=f"{info}. " + (f"Known: {', '.join(known)}" if known else "Nobody enrolled yet"),
              transcript=transcript, image=jpg, cycle=cycle, oled=text)


# ----------------------------------------------------------------------
# Live page
# ----------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ECHO</title>
<meta http-equiv="refresh" content="2">
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0b0d10; color:#e8eaed;
         font-family:system-ui,-apple-system,Segoe UI,sans-serif; }}
  .wrap {{ display:flex; gap:5vw; align-items:center; flex-wrap:wrap;
          justify-content:center; padding:5vh 4vw; max-width:1200px; }}
  .main {{ text-align:center; max-width:620px; }}
  .headline {{ font-size:clamp(2rem,6vw,4.4rem); font-weight:650;
               line-height:1.1; margin:0 0 .35em; color:{color}; }}
  .detail {{ font-size:clamp(.95rem,1.8vw,1.3rem); opacity:.75; margin:0 0 1.2em; }}
  .said {{ font-size:clamp(.9rem,1.5vw,1.1rem); opacity:.55;
           font-style:italic; margin:0 0 1.4em; }}
  img {{ max-width:min(420px,70vw); border-radius:14px; border:1px solid #23262b; }}
  .screen {{ background:#04121a; border:2px solid #1d2a33; border-radius:10px;
             padding:16px 18px; font-family:ui-monospace,Consolas,monospace;
             font-size:1.05rem; line-height:1.45; color:#7ef7d0;
             white-space:pre; text-align:left; box-shadow:0 0 40px #0a3a4a55; }}
  .cap {{ font-size:.75rem; letter-spacing:.08em; text-transform:uppercase;
          opacity:.4; margin:0 0 .6em; }}
  .foot {{ margin-top:1.4em; font-size:.8rem; opacity:.4;
           font-variant-numeric:tabular-nums; }}
  .recall {{ margin-top:1.2em; max-width:420px; font-size:.85rem; line-height:1.45;
             white-space:pre-wrap; text-align:left; color:#c9b8ff; opacity:.85; }}
</style></head>
<body><div class="wrap">
  <div class="main">
    <p class="headline">{headline}</p>
    <p class="detail">{detail}</p>
    {said}
    {img}
    <p class="foot">cycle {cycle} &middot; {when}</p>
  </div>
  <div>
    <p class="cap">what the glasses show</p>
    <div class="screen">{oled}</div>
    {recall}
  </div>
</div></body></html>"""

COLORS = {"idle": "#8b95a1", "enrolled": "#6ee7a8", "known": "#7cc4ff",
          "unknown": "#ffcc66", "error": "#ff8a8a", "memory": "#c9b8ff",
          "guide": "#ffd59e"}


class PageHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        st = get_state()
        if self.path.startswith("/photo"):
            p = st.get("image")
            if p and os.path.exists(p):
                data = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)
            return

        said = (f'<p class="said">&ldquo;{html.escape(st["transcript"])}&rdquo;</p>'
                if st.get("transcript") else "")
        img = (f'<img src="/photo?c={st["cycle"]}" alt="last capture">'
               if st.get("image") else "")
        oled = html.escape(st.get("oled") or "").ljust(1) or "(nothing yet)"
        recall = (f'<p class="cap" style="margin-top:1.4em">Hindsight recalled</p>'
                  f'<div class="recall">{html.escape(st["recall"])}</div>'
                  if st.get("recall") else "")

        body = PAGE.format(
            color=COLORS.get(st.get("kind", "idle"), "#e8eaed"),
            headline=html.escape(st["headline"]), detail=html.escape(st["detail"]),
            said=said, img=img, oled=oled, recall=recall,
            cycle=st["cycle"], when=st["when"] or "—",
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ----------------------------------------------------------------------

def main():
    os.makedirs(FACES_DIR, exist_ok=True)
    load_memory()

    page_server = HTTPServer(("0.0.0.0", WEB_PORT), PageHandler)
    threading.Thread(target=page_server.serve_forever, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(4)

    people = enrolled_names()
    places = [p["name"] for p in MEMORY["places"]]
    print(f"ECHO brain listening on {HOST}:{PORT}")
    print(f"Display page:  http://localhost:{WEB_PORT}")
    print(f"People:        {', '.join(people) if people else 'nobody yet'}")
    print(f"Places:        {', '.join(places) if places else 'none yet'}")
    print(f"Memory:        {LTM.describe()}")
    print()
    print("Say while touching:")
    print("   'hello my name is Satyabrat'   -> remembers the face")
    print("   'we are at the college lab'    -> names this place")
    print("   'who is this guy'              -> shows who + where + what you discussed")
    print("   'when is my doctor's visit'    -> answers from long-term memory")
    print()

    cycle = 0
    while True:
        conn, addr = srv.accept()
        cycle += 1
        print(f"[cycle {cycle}] from {addr[0]}")
        try:
            handle_cycle(conn, cycle)
        except (ConnectionError, ValueError, struct.error) as e:
            print(f"  ! cycle failed: {e}")
        except Exception as e:
            print(f"  ! unexpected error: {e}")
        finally:
            conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        LTM.close()
