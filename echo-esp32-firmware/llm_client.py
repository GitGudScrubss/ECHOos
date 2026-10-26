"""
One small text-generation call for the ECHO brain and the guide agent.

    Groq    if GROQ_API_KEY is set (fast, free tier; model GROQ_MODEL,
            default openai/gpt-oss-120b, as the hackathon brief recommends)
    Ollama  otherwise, whichever small model is installed locally

No tool/function calling is used: models are asked for plain text or a JSON
object, and every caller must cope with None (no LLM reachable) or output it
cannot parse. That is the "handle function calling errors" advice from the
brief, applied by not depending on them at all.
"""

import json
import os
import re
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODELS = ["llama3.2", "llama3.2:3b", "qwen2.5:3b", "phi3.5", "gemma2:2b", "llama3"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

_ollama_model, _ollama_checked = None, False


def get_ollama_model():
    """Pick whichever small model is actually installed."""
    global _ollama_model, _ollama_checked
    if _ollama_checked:
        return _ollama_model
    _ollama_checked = True
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            tags = json.load(r)
        installed = [m["name"] for m in tags.get("models", [])]
        if not installed:
            print("  ! ollama is running but has no models:  ollama pull llama3.2")
            return None
        for want in OLLAMA_MODELS:
            for have in installed:
                if have == want or have.startswith(want + ":"):
                    _ollama_model = have
                    print(f"  ollama model: {have}")
                    return _ollama_model
        _ollama_model = installed[0]
        print(f"  ollama model: {_ollama_model} (fallback)")
    except (urllib.error.URLError, OSError, ValueError):
        print("  ! ollama not reachable — LLM steps fall back to plain rules")
    return _ollama_model


def backend():
    if os.environ.get("GROQ_API_KEY"):
        return f"groq:{GROQ_MODEL}"
    model = get_ollama_model()
    return f"ollama:{model}" if model else None


def generate(prompt, system=None, max_tokens=200, temperature=0.2,
             timeout=45, want_json=False):
    """Returns the model's text, or None if no LLM is reachable."""
    if os.environ.get("GROQ_API_KEY"):
        return _groq(prompt, system, max_tokens, temperature, timeout, want_json)
    return _ollama(prompt, system, max_tokens, temperature, timeout, want_json)


def generate_json(prompt, system=None, max_tokens=500, timeout=60):
    """generate() and parse the first JSON object in the reply, or None."""
    text = generate(prompt, system, max_tokens=max_tokens, timeout=timeout,
                    want_json=True)
    return parse_json_object(text)


def parse_json_object(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _ollama(prompt, system, max_tokens, temperature, timeout, want_json):
    model = get_ollama_model()
    if model is None:
        return None
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens}}
    if system:
        body["system"] = system
    if want_json:
        body["format"] = "json"
    try:
        return _post(f"{OLLAMA_URL}/api/generate", body, {}, timeout).get("response", "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  ! ollama call failed: {e}")
        return None


def _groq(prompt, system, max_tokens, temperature, timeout, want_json):
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    body = {"model": GROQ_MODEL, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    try:
        out = _post(GROQ_URL, body, headers, timeout)
        return out["choices"][0]["message"].get("content") or ""
    except urllib.error.HTTPError as e:
        # json_object mode can fail with "failed_generation" on a malformed
        # reply; one plain retry is cheaper than giving up.
        if want_json and e.code == 400:
            body.pop("response_format", None)
            try:
                out = _post(GROQ_URL, body, headers, timeout)
                return out["choices"][0]["message"].get("content") or ""
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                pass
        print(f"  ! groq call failed: HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        print(f"  ! groq call failed: {e}")
    return None
