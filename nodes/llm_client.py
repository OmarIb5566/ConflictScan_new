import os
import requests
import json
import time
import re

def local_url(url: str) -> str:
    """On Windows 'localhost' tries IPv6 first and costs ~2 s per request; use the IPv4 loopback."""
    return url.replace("://localhost", "://127.0.0.1")


def ollama_base(url: str = None) -> str:
    url = (url or os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    for suffix in ("/api/chat", "/api/generate"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return local_url(url)


OLLAMA_URL = ollama_base() + "/api/chat"
# One keep-alive connection pool for every model call (no TCP handshake per call).
http = requests.Session()
http.mount("http://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32))
MODEL_NAME = os.environ.get("CONFLICTSCAN_TEXT_MODEL", "gemma4:e4b")

# Ollama falls back to a small default context window when none is requested
# and silently drops the START of an over-long prompt - i.e. the system
# instructions. Every call asks for an explicit window large enough for the
# longest prompt this pipeline builds.
NUM_CTX = int(os.environ.get("CONFLICTSCAN_NUM_CTX", "16384"))
NUM_PREDICT = int(os.environ.get("CONFLICTSCAN_NUM_PREDICT", "8192"))


def ollama_options(temperature: float, **extra) -> dict:
    return {"temperature": temperature, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX, **extra}


def _dedupe(value, counter):
    """Remove repeated elements from every list (a looping model repeats list entries verbatim)."""
    if isinstance(value, dict):
        return {k: _dedupe(v, counter) for k, v in value.items()}
    if isinstance(value, list):
        out, seen = [], set()
        for v in value:
            sig = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
            if sig in seen:
                counter[0] += 1
                continue
            seen.add(sig)
            out.append(_dedupe(v, counter))
        return out
    return value


def salvage_truncated_json(text: str):
    """
    A JSON object cut off part-way (output cap, or stopped because it was looping):
    keep everything up to the last complete value, close the open brackets, and
    drop repeated list entries. Returns the object with "_truncated": True and
    "_repeated": n, or None when nothing usable can be recovered.
    """
    text = text or ""
    start = text.find("{")
    if start < 0:
        return None
    stack, in_str, esc = [], False, False
    cuts = []                       # (position just before a ',', closers needed there)
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                return None         # the object closed - not a truncation, leave it to the normal parser
            cuts.append((i + 1, "".join(reversed(stack))))
        elif ch == ",":
            cuts.append((i, "".join(reversed(stack))))
    # Prefer a cut between whole elements of the top object or its lists, so a
    # half-written item is dropped rather than kept with missing fields.
    shallow = [c for c in cuts if len(c[1]) <= 2]
    for pos, closers in list(reversed(shallow[-200:])) + list(reversed(cuts[-50:])):
        try:
            value = json.loads(text[start:pos] + closers)
        except ValueError:
            continue
        if not isinstance(value, dict) or not value:
            return None
        counter = [0]
        value = _dedupe(value, counter)
        value["_truncated"] = True
        value["_repeated"] = counter[0]
        return value
    return None


class OllamaLLM:
    def __init__(self, model: str = MODEL_NAME, url: str = OLLAMA_URL):
        self.model = model
        self.url = url

    def run(self, system: str, user: str, temperature: float = 0.2, max_retries: int = 3) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "stream": False,
            "format": "json",  # Force JSON format
            "options": ollama_options(temperature),
        }

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 5 * attempt
                    print(f"[llm] Retry {attempt}/{max_retries} after {wait_time}s...")
                    time.sleep(wait_time)

                print(f"[llm] Calling LLM...")
                response = http.post(
                    self.url,
                    headers={"Content-Type": "application/json"},
                    data=json.dumps(payload),
                    timeout=600  # 10 minutes
                )

                response.raise_for_status()
                resp_json = response.json()

                # Debug print
                print(f"[llm] Raw response keys: {resp_json.keys()}")

                content = resp_json.get("message", {}).get("content", "")

                if not content or len(content) == 0:
                    print(f"[llm] Empty response from LLM")
                    print(f"Full response: {resp_json}")
                    raise ValueError("LLM returned empty content")

                print(f"[llm] LLM responded ({len(content)} chars)")

                # Try to parse JSON
                parsed = self._extract_json(content)
                print(f"[llm] JSON parsed successfully")
                return parsed

            except Exception as e:
                print(f"[llm] Error (attempt {attempt + 1}): {str(e)[:200]}")
                if attempt == max_retries:
                    raise Exception(f"LLM failed after {max_retries} retries: {e}")

        raise Exception("LLM max retries exceeded")

    def _extract_json(self, text: str) -> dict:
        """Extract JSON from LLM response - handles partial/malformed JSON"""
        text = text.strip()

        # Try direct parse
        try:
            return json.loads(text)
        except:
            pass

        # Extract from markdown
        if '```json' in text or '```' in text:
            pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
            matches = re.findall(pattern, text, re.DOTALL)
            if matches:
                try:
                    return json.loads(matches[0])
                except:
                    pass

        # Find first complete JSON object
        brace_start = text.find('{')
        if brace_start != -1:
            brace_count = 0
            for i in range(brace_start, len(text)):
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_str = text[brace_start:i+1]
                        try:
                            return json.loads(json_str)
                        except:
                            # Try fixing common issues
                            json_str = json_str.replace('\n', ' ')
                            json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)  # Remove trailing commas
                            try:
                                return json.loads(json_str)
                            except:
                                pass

        # A reply cut off at the output cap (often a model stuck repeating an
        # item): keep every complete element of its opening list instead of
        # throwing the whole reply away. Marked so callers can report it.
        salvaged = salvage_truncated_json(text)
        if salvaged is not None:
            return salvaged

        # Last resort: an empty object. Callers read the keys they expect with
        # .get() and fall back on their own, so an empty dict degrades cleanly
        # whatever the caller asked for.
        print("[llm] Could not parse JSON - returning empty result")
        return {}

    def test_connection(self) -> bool:
        """Test if Ollama is running and model is loaded"""
        try:
            # List models
            response = http.get(self.url.replace("/api/chat", "/api/tags"), timeout=5)
            if response.status_code == 200:
                models = response.json().get("models", [])
                print(f"[llm] Available models: {[m['name'] for m in models]}")

                # Check if our model exists
                model_exists = any(self.model in m['name'] for m in models)
                if not model_exists:
                    print(f"[llm] Model {self.model} not found!")
                    print(f"[llm] Pull it with: ollama pull {self.model}")
                    return False

                print(f"[llm] Ollama is running, {self.model} is available")
                return True
            return False
        except Exception as e:
            print(f"[llm] Ollama connection failed: {e}")
            print(f"[llm] Start Ollama with: ollama serve")
            return False

llm_client = OllamaLLM()
