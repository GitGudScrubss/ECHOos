"""
Long-term episodic memory for the ECHO brain, backed by Hindsight.

The brain on its own forgets almost everything: per person it keeps eight
19-character bullets with no dates, and spoken questions about the past get
"I don't have a record of that". This module gives it a real memory that
grows with every conversation, so the same question gets a better answer on
day 20 than on day 1.

    retain   -> after each conversation, store what was said, who, where, when
    recall   -> fetch the facts relevant to a person, a place or a question
    reflect  -> ask Hindsight to reason over those facts ("how should I guide
                Margaret through making tea?")

Opt-in and fail-safe, in that order:

  * Nothing leaves the laptop unless ECHO_HINDSIGHT=on. Only text is sent:
    transcripts, names, place labels. Face images, face descriptors, audio
    and WiFi fingerprints never leave the device.
  * Every call has a deadline and returns None on any failure, so a dead
    network or a bad key degrades the brain to exactly its old behaviour.
    Retains run in the background and never block a touch cycle.

Config (environment):

    ECHO_HINDSIGHT        on | off (default off)
    HINDSIGHT_BASE_URL    default https://api.hindsight.vectorize.io
    HINDSIGHT_API_KEY     your key (or HINDSIGHT_API_KEY_FILE / a repo-root
                          HINDSIGHT_API_KEY.env containing just the key)
    HINDSIGHT_BANK        one bank per wearer, default echo-wearer
    ECHO_WEARER_NAME      used in the bank mission, default "the wearer"
"""

import contextlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://api.hindsight.vectorize.io"
HERE = os.path.dirname(os.path.abspath(__file__))

# Interactive calls happen while someone is standing in front of the wearer.
# Past these deadlines the answer is no longer useful, so we give up and fall
# back to what the device already knows.
RECALL_TIMEOUT = float(os.environ.get("HINDSIGHT_RECALL_TIMEOUT", "6"))
REFLECT_TIMEOUT = float(os.environ.get("HINDSIGHT_REFLECT_TIMEOUT", "15"))
# After a failed read, skip reads for this long so a dead network costs one
# timeout, not one per touch.
BACKOFF_SECONDS = 60.0
# Retains run in the background (or while seeding), where a batch of a few
# dozen memories can take a couple of minutes of LLM extraction server-side.
RETAIN_TIMEOUT = 300.0


def env_on(name, default="off"):
    return os.environ.get(name, default).strip().lower() in ("1", "on", "true", "yes")


def _read_key():
    """HINDSIGHT_API_KEY, else a key file holding either the bare key or a
    HINDSIGHT_API_KEY=... line. The file is gitignored (*.env)."""
    key = os.environ.get("HINDSIGHT_API_KEY", "").strip()
    if key:
        return key
    candidates = [os.environ.get("HINDSIGHT_API_KEY_FILE", ""),
                  os.path.join(HERE, "HINDSIGHT_API_KEY.env"),
                  os.path.join(HERE, "..", "HINDSIGHT_API_KEY.env")]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        name, _, value = line.partition("=")
                        if name.strip() == "HINDSIGHT_API_KEY":
                            return value.strip().strip('"\'')
                        continue
                    return line
    return ""


def slug(text):
    """'Dr. Rao' -> 'dr-rao', for tags like person:dr-rao."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "unknown"


def person_tag(name):
    return f"person:{slug(name)}"


def mission_for(wearer):
    return (
        f"You are the long-term memory of ECHO, smart glasses worn by {wearer}, "
        "who lives with memory loss. Keep track of the people in their life and "
        "how they know them, places, promises and plans, appointments, "
        "preferences and routines, and how they cope with everyday tasks: which "
        "step confused them and which cue helped. Answer in plain, short, calm "
        "sentences. One idea at a time. Never give medical advice; if something "
        "sounds medical, say who to ask. If the memory does not contain the "
        "answer, say so instead of guessing."
    )


def retain_mission_for(wearer):
    return (
        f"Extract durable facts about {wearer}'s life: who they met and their "
        "relationship, where, what was discussed, promises made by or to them, "
        "dates of upcoming events, preferences (food, drink, objects, routines), "
        "where objects were left, and task outcomes (which step of a task needed "
        "help, which prompt or cue worked, which steps they never need help "
        "with). Keep dates. Ignore filler and small talk."
    )


class LongTermMemory:
    """Thin, fail-safe wrapper around hindsight_client.Hindsight.

    The client runs its async code on a per-thread event loop, so each lane
    (background writes, interactive reads) owns one client on one dedicated
    worker thread. A slow retain therefore never delays a recall.
    """

    def __init__(self, bank=None, enabled=None, wearer=None,
                 base_url=None, api_key=None):
        self.enabled = env_on("ECHO_HINDSIGHT") if enabled is None else enabled
        self.bank = bank or os.environ.get("HINDSIGHT_BANK", "echo-wearer")
        self.wearer = wearer or os.environ.get("ECHO_WEARER_NAME", "the wearer")
        self.base_url = (base_url or os.environ.get("HINDSIGHT_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else _read_key()
        self.last_error = ""
        self._down_until = 0.0
        self._bank_ready = False
        self._bank_lock = threading.Lock()
        self._collecting = None          # list while inside collect()
        self._writer = None
        self._reader = None
        self._local = threading.local()

        if self.enabled and not self.api_key:
            self.last_error = "ECHO_HINDSIGHT=on but no HINDSIGHT_API_KEY"
            self.enabled = False

    # ---- plumbing ---------------------------------------------------------

    @property
    def available(self):
        return self.enabled

    def describe(self):
        if self.enabled:
            return f"Hindsight on (bank '{self.bank}' at {self.base_url})"
        return "Hindsight off" + (f" ({self.last_error})" if self.last_error else "")

    def _client(self, timeout, attempts):
        # Called only on a lane's own worker thread.
        c = getattr(self._local, "client", None)
        if c is None:
            from hindsight_client import Hindsight
            c = Hindsight(base_url=self.base_url, api_key=self.api_key,
                          timeout=timeout, max_attempts=attempts,
                          user_agent="echo-os/hindsight-memory")
            self._local.client = c
        return c

    def _lane(self, which):
        if which == "write":
            if self._writer is None:
                self._writer = ThreadPoolExecutor(1, thread_name_prefix="hs-write")
            return self._writer, RETAIN_TIMEOUT, 3
        if self._reader is None:
            self._reader = ThreadPoolExecutor(1, thread_name_prefix="hs-read")
        return self._reader, REFLECT_TIMEOUT, 1

    def _submit(self, which, fn):
        pool, http_timeout, attempts = self._lane(which)

        def job():
            client = self._client(http_timeout, attempts)
            if not self._ensure_bank(client):
                raise RuntimeError(self.last_error)
            return fn(client)

        return pool.submit(job)

    def _call(self, which, fn, deadline):
        """Run fn(client) on a lane. Returns its result, or None on timeout or
        any error (recorded in last_error). deadline=None waits for it."""
        if not self.enabled:
            return None
        if which == "read" and time.monotonic() < self._down_until:
            return None
        fut = self._submit(which, fn)
        try:
            result = fut.result(timeout=deadline)
            self._down_until = 0.0
            return result
        except FutureTimeout:
            self.last_error = f"timed out after {deadline:.0f}s"
        except Exception as e:                       # network, auth, API errors
            self.last_error = f"{type(e).__name__}: {e}"[:200]
        if which == "read":
            self._down_until = time.monotonic() + BACKOFF_SECONDS
        return None

    def _ensure_bank(self, client):
        with self._bank_lock:
            if self._bank_ready:
                return True
            try:
                client.create_bank(
                    self.bank,
                    name=f"ECHO - {self.wearer}",
                    mission=mission_for(self.wearer),
                    retain_mission=retain_mission_for(self.wearer),
                    disposition_skepticism=3,
                    disposition_literalism=4,
                    disposition_empathy=5,
                )
                self._bank_ready = True
            except Exception as e:
                self.last_error = f"create_bank failed: {type(e).__name__}: {e}"[:200]
            return self._bank_ready

    # ---- retain -----------------------------------------------------------

    def retain(self, content, context=None, tags=None, when=None,
               document_id=None, block=False):
        """Store one memory. Non-blocking by default: returns True once queued
        (or once stored, if block=True). Returns False if memory is off."""
        if not self.enabled or not content:
            return False
        item = {"content": content}
        if context:
            item["context"] = context
        if tags:
            item["tags"] = list(tags)
        if when is not None:
            item["timestamp"] = _iso(when)
        if document_id:
            item["document_id"] = document_id

        if self._collecting is not None:
            self._collecting.append(item)
            return True

        def write(client):
            client.retain_batch(self.bank, [item])
            return True

        if block:
            return bool(self._call("write", write, RETAIN_TIMEOUT))
        fut = self._submit("write", write)
        fut.add_done_callback(self._note_failure)
        return True

    def _note_failure(self, fut):
        e = fut.exception()
        if e is not None:
            self.last_error = f"retain failed: {type(e).__name__}: {e}"[:200]

    @contextlib.contextmanager
    def collect(self):
        """Batch every retain() inside the block into one retain_batch call
        (used when seeding a story: one request instead of thirty)."""
        self._collecting = []
        try:
            yield
        finally:
            items, self._collecting = self._collecting, None
            if items and self.enabled:
                self._call("write", lambda c: c.retain_batch(self.bank, items), None)

    def flush(self, timeout=RETAIN_TIMEOUT):
        """Wait for queued background retains (used before exiting)."""
        if self._writer is not None:
            self._writer.submit(lambda: None).result(timeout=timeout)

    def close(self):
        """Flush pending writes and close each lane's client on its own thread."""
        def shut():
            c = getattr(self._local, "client", None)
            if c is not None:
                with contextlib.suppress(Exception):
                    c.close()
                self._local.client = None
        for pool in (self._writer, self._reader):
            if pool is not None:
                with contextlib.suppress(Exception):
                    pool.submit(shut).result(timeout=RETAIN_TIMEOUT)
                pool.shutdown(wait=False)
        self._writer = self._reader = None

    # ---- recall / reflect -------------------------------------------------

    def recall(self, query, tags=None, max_tokens=900, budget="low",
               timeout=RECALL_TIMEOUT, tags_match="any", query_timestamp=None):
        """Facts relevant to query, most relevant first, as [{text, when}],
        or None if memory is off or unreachable. query_timestamp anchors
        relative phrases like "yesterday" (defaults to now)."""
        def read(client):
            r = client.recall(self.bank, query, max_tokens=max_tokens,
                              budget=budget, tags=tags, tags_match=tags_match,
                              query_timestamp=_iso(query_timestamp) if query_timestamp else None)
            # Results are not guaranteed to arrive in relevance order.
            ranked = sorted(r.results or [], key=_score, reverse=True)
            return [_fact(res) for res in ranked]
        return self._call("read", read, timeout)

    def reflect(self, query, tags=None, context=None, budget="low",
                timeout=REFLECT_TIMEOUT, tags_match="any"):
        """Hindsight reasons over the bank (guided by its mission) and answers.
        Returns {"text", "facts"} — facts being the memories it based the
        answer on, as [{text, when}] — or None."""
        def think(client):
            r = client.reflect(self.bank, query, budget=budget, context=context,
                               tags=tags, tags_match=tags_match, include_facts=True)
            text = (r.text or "").strip()
            if not text:
                return None
            used = getattr(r.based_on, "memories", None) or []
            return {"text": text, "facts": [_fact(m) for m in used]}
        return self._call("read", think, timeout)

    def delete_bank(self):
        ok = self._call("write", lambda c: (c.delete_bank(self.bank), True)[1], None)
        self._bank_ready = False
        return bool(ok)


# ---------------------------------------------------------------------------

def _iso(when):
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.isoformat()
    return str(when)


def _score(result):
    scores = getattr(result, "scores", None)
    return getattr(scores, "final", None) or 0.0


def _fact(item):
    """A recall result or reflect fact -> {text, when}. Hindsight appends
    ' | When: ... | Involving: ...' to fact text; we keep the sentence."""
    when = getattr(item, "occurred_start", None) or getattr(item, "mentioned_at", None)
    return {"text": (item.text or "").split(" | ")[0].strip(), "when": _short_date(when)}


def _short_date(value):
    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d %b")
    except (TypeError, ValueError):
        return ""
