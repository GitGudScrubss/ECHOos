"""
Local bridge between the C++ cognitive core (echo-demo --hindsight) and
Hindsight. It owns the API key and the hindsight-client SDK, through the same
fail-safe wrapper the ESP32 brain uses (echo-esp32-firmware/hindsight_memory.py),
so the C++ side never sees a credential.

    POST /retain  {"text", "context", "when"}   store one memory (background)
    POST /answer  {"question", "now"}           {"answer": "..."} or {"answer": null}
    GET  /health

/answer asks Hindsight reflect for structured output {found, answer} and
returns an answer only when found is true, so "memory has nothing" comes back
as null and the core abstains instead of repeating a polite non-answer.

Listens on 127.0.0.1 only.

    python scripts/hindsight_bridge.py [--port 8799] [--bank echo-wearer]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "echo-esp32-firmware"))

import hindsight_memory  # noqa: E402

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
    },
    "required": ["found", "answer"],
}

LTM = None
WEARER = "the wearer"


def when_from(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc)


def answer(question, now):
    today = now.astimezone()
    r = LTM.reflect(
        f"{WEARER} asks: {question}",
        context=(f"Today is {today:%A %d %B %Y}. Reply for {WEARER}, read aloud by "
                 "their glasses: at most 30 words, calm, addressing them as "
                 "'you', dates as weekday and day. Be exact with times: when "
                 "memories give more than one (an appointment and a pick-up), "
                 "say which is which. Set found to false, and "
                 "answer to an empty string, unless the memories actually "
                 "contain the answer."),
        schema=ANSWER_SCHEMA)
    data = (r or {}).get("data") or {}
    text = str(data.get("answer") or "").strip()
    return text if data.get("found") is True and text else None


class Handler(BaseHTTPRequestHandler):
    server_version = "echo-hindsight-bridge"

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(n, 64 * 1024)) or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": LTM.enabled, "memory": LTM.describe()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        data = self._body()
        if self.path == "/retain":
            text = str(data.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text required"})
            LTM.retain(text, context=str(data.get("context") or "") or None,
                       when=when_from(data.get("when")))
            print(f"  retain: {text[:90]}")
            return self._json(202, {"queued": True})
        if self.path == "/answer":
            question = str(data.get("question") or "").strip()
            if not question:
                return self._json(400, {"error": "question required"})
            text = answer(question, when_from(data.get("now")))
            print(f"  answer: {question!r} -> {text!r}"
                  + (f"  ({LTM.last_error})" if text is None and LTM.last_error else ""))
            return self._json(200, {"answer": text})
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass                                  # one line per call above is enough


def main():
    global LTM, WEARER
    p = argparse.ArgumentParser(description="Hindsight bridge for echo-demo --hindsight")
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--bank", default=os.environ.get("HINDSIGHT_BANK", "echo-wearer"))
    a = p.parse_args()

    WEARER = os.environ.get("ECHO_WEARER_NAME", "the wearer")
    # Running the bridge is the opt-in; it still needs a key to do anything.
    LTM = hindsight_memory.LongTermMemory(bank=a.bank, enabled=True, wearer=WEARER)
    print(f"{LTM.describe()}\nlistening on http://127.0.0.1:{a.port}")
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        LTM.close()


if __name__ == "__main__":
    main()
