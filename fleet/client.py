"""
Fleet client - every model call in the pipeline goes through here
=================================================================
    call(role, system, user, images=None, ...)   -> the model's reply text
    llm(role)                                    -> drop-in for the old OllamaLLM / OllamaVisionLLM
    embed(texts)                                 -> embedding vectors (bge-m3)
    pmap(fn, items, role)                        -> fn over items in parallel, results in order

What it adds around a plain HTTP call:
- the role's model from the active profile, falling back to the next model
  when one is not installed or keeps failing; ":cloud" models are refused;
- the model server the role is pinned to (adapters/model_servers.py), else
  any server that has the model installed;
- a cap on requests in flight per role (the GPUs queue the rest);
- a response cache keyed by the exact request (model + prompt + image bytes +
  options): re-scans of unchanged files and repeated prompts cost nothing;
- one row per call in model_calls (role, model, server, time, tokens, cached)
  for the usage dashboard and the benchmark;
- keep-alive: in "resident" mode models stay loaded; in "staged" mode they
  are released a little after their stage ends.

Windows compatible.
"""

import base64
import contextvars
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from fleet import profile as profiles

current_run: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("fleet_run", default=None)
# Set by the scan worker: a function that says whether the user cancelled this scan.
cancel_check: contextvars.ContextVar[Optional[Callable[[], bool]]] = contextvars.ContextVar("fleet_cancel", default=None)


class ScanCancelled(Exception):
    """The user cancelled the scan; raised before the next model call so a long stage stops quickly."""


def _check_cancelled() -> None:
    check = cancel_check.get()
    if check is not None and check():
        raise ScanCancelled("Cancelled by the user.")

_installed: Dict[str, Tuple[float, List[str]]] = {}
_installed_lock = threading.Lock()
_sems: Dict[Tuple[str, str], threading.BoundedSemaphore] = {}
_sem_lock = threading.Lock()
_broken: Dict[Tuple[str, str], float] = {}       # (server, model) -> time it last failed hard
CACHE_ENABLED = True
THINKING_FAMILIES = ("qwen3", "gpt-oss", "deepseek-r1", "gemma4", "magistral")


class ModelUnavailable(ConnectionError):
    """No configured model for a role is reachable (a transient failure - the scan is retried)."""


# ---------------------------------------------------------------------------
# Servers and models
# ---------------------------------------------------------------------------

def _http():
    from nodes.llm_client import http
    return http


def installed_models(server: Dict[str, Any], max_age: float = 60.0) -> List[str]:
    with _installed_lock:
        hit = _installed.get(server["url"])
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
    from adapters import model_servers
    try:
        models = model_servers.list_models(server, timeout=5)
    except Exception:  # noqa: BLE001
        models = []
    with _installed_lock:
        _installed[server["url"]] = (time.time(), models)
    return models


def _matches(installed: str, wanted: str) -> bool:
    return installed == wanted or (":" not in wanted and installed.split(":")[0] == wanted) \
        or (wanted.endswith(":latest") and installed == wanted[:-7])


def resolve(role: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Candidate (model, server) pairs for a role, best first."""
    from adapters import model_servers
    cfg = profiles.role(role)
    if not cfg:
        return []
    servers = model_servers.servers()
    pinned = [s for s in servers if role in (s.get("roles") or [])]
    free = [s for s in servers if not s.get("roles")]
    pool = pinned + free or servers
    out = []
    for model in [cfg["model"], *cfg.get("fallback", [])]:
        if not model or model.endswith(":cloud"):
            continue
        for s in pool:
            names = installed_models(s)
            match = next((n for n in names if _matches(n, model)), None)
            if match and time.time() - _broken.get((s["url"], match), 0) > 120:
                out.append((match, s))
    if not out and pool:            # nothing reported as installed (server down?) - try the primary anyway
        out.append((cfg["model"], pool[0]))
    return out


def resolved_model(role: str) -> Optional[str]:
    cands = resolve(role)
    return cands[0][0] if cands else None


def _sem(role: str, server: Dict[str, Any]) -> threading.BoundedSemaphore:
    cfg = profiles.role(role) or {}
    key = (server["url"], role)
    with _sem_lock:
        if key not in _sems:
            _sems[key] = threading.BoundedSemaphore(max(1, int(cfg.get("parallel", 1))))
        return _sems[key]


# ---------------------------------------------------------------------------
# Cache and call log
# ---------------------------------------------------------------------------

def _file_digest(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(model: str, payload: Dict[str, Any], image_digests: Sequence[str]) -> str:
    stable = {k: payload.get(k) for k in ("messages", "format", "options", "think", "input")}
    for m in stable.get("messages") or []:
        m.pop("images", None)
    raw = json.dumps({"model": model, "p": stable, "img": list(image_digests)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[str]:
    if not CACHE_ENABLED:
        return None
    try:
        import storage
        from db import ai_cache
        from sqlalchemy import select, update
        with storage.tx() as conn:
            value = conn.execute(select(ai_cache.c.value).where(ai_cache.c.key == key)).scalar()
            if value is not None:
                conn.execute(update(ai_cache).where(ai_cache.c.key == key)
                             .values(hits=ai_cache.c.hits + 1, last_used_at=storage.now()))
            return value
    except Exception:  # noqa: BLE001 - the cache is an optimisation, never a failure
        return None


def _cache_put(key: str, role: str, model: str, value: str) -> None:
    if not CACHE_ENABLED:
        return
    try:
        import storage
        from db import ai_cache
        from sqlalchemy import insert
        stamp = storage.now()
        with storage.tx() as conn:
            conn.execute(insert(ai_cache).values(key=key, role=role, model=model, value=value, created_at=stamp,
                                                 last_used_at=stamp, hits=0))
    except Exception:  # noqa: BLE001 - e.g. two workers cached the same answer at once
        pass


def _log_call(role, model, server, ms, prompt_tokens=None, output_tokens=None, images=0, cached=False,
              ok=True, error=None) -> None:
    try:
        import storage
        from db import model_calls
        from sqlalchemy import insert
        with storage.tx() as conn:
            conn.execute(insert(model_calls).values(
                id=uuid.uuid4().hex[:16], at=storage.now(), run_id=current_run.get(), role=role, model=model,
                server=(server or {}).get("name"), ms=int(ms), prompt_tokens=prompt_tokens,
                output_tokens=output_tokens, images=images, cached=cached, ok=ok,
                error=(str(error)[:300] if error else None)))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

def _options(temperature: float, extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from nodes.llm_client import ollama_options
    return ollama_options(temperature, **(extra or {}))


_open_responses: Dict[int, Any] = {}      # thread id -> the streamed response it is reading


def _post(server: Dict[str, Any], payload: Dict[str, Any], timeout: float = 900) -> Dict[str, Any]:
    http = _http()
    if server.get("kind") == "openai":
        msgs = []
        for m in payload["messages"]:
            if m.get("images"):
                content = [{"type": "text", "text": m["content"]}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in m["images"]]
                msgs.append({"role": m["role"], "content": content})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        body = {"model": payload["model"], "messages": msgs, "temperature": payload["options"].get("temperature", 0.1),
                "max_tokens": payload["options"].get("num_predict", 4096)}
        if payload.get("format") == "json":
            body["response_format"] = {"type": "json_object"}
        r = http.post(server["url"] + "/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        return {"content": data["choices"][0]["message"].get("content") or "",
                "prompt_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                "truncated": data["choices"][0].get("finish_reason") == "length"}
    body = {**payload, "stream": True}
    r = http.post(server["url"] + "/api/chat", json=body, timeout=timeout, stream=True)
    if r.status_code == 400 and "think" in body:
        if "think" in r.text.lower():
            body = {k: v for k, v in body.items() if k != "think"}
            r = http.post(server["url"] + "/api/chat", json=body, timeout=timeout, stream=True)
    r.raise_for_status()
    _open_responses[threading.get_ident()] = r
    try:
        return _read_stream(r)
    finally:
        _open_responses.pop(threading.get_ident(), None)


LOOP_WINDOW = 1200      # chars: a reply whose last LOOP_WINDOW chars already appeared twice is looping
LOOP_CHECK_EVERY = 1500


def is_looping(text: str) -> bool:
    """True when the reply's tail is a verbatim repeat of earlier output, seen at least twice before."""
    if len(text) < LOOP_WINDOW * 3:
        return False
    tail = text[-LOOP_WINDOW:]
    return text.count(tail, 0, len(text) - LOOP_WINDOW) >= 2


def _read_stream(r) -> Dict[str, Any]:
    """Collect an Ollama streamed chat reply; stop it early when the model is stuck repeating itself."""
    parts: List[str] = []
    size, next_check, last = 0, LOOP_CHECK_EVERY, {}
    looped = False
    try:
        for line in r.iter_lines():
            if not line:
                continue
            last = json.loads(line)
            if last.get("error"):
                raise RuntimeError(last["error"])
            piece = (last.get("message") or {}).get("content") or ""
            if piece:
                parts.append(piece)
                size += len(piece)
            if last.get("done"):
                break
            if size >= next_check:
                next_check = size + LOOP_CHECK_EVERY
                if is_looping("".join(parts)):
                    looped = True
                    break
    finally:
        r.close()                  # closing the stream also stops generation on the server
    return {"content": "".join(parts), "prompt_tokens": last.get("prompt_eval_count"),
            "output_tokens": last.get("eval_count") or (size // 4 if looped else None),
            "truncated": looped or last.get("done_reason") == "length", "looped": looped}


CANCEL_POLL_SECONDS = 1.0


def _post_cancellable(server: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    _post on a helper thread while this one watches for a cancel. A single call
    can run for minutes (a long prompt, a busy model server); checking only
    between calls left 'Cancel scan' waiting for it. On cancel the stream is
    closed - which also stops generation on the server - and ScanCancelled is
    raised at once.
    """
    check = cancel_check.get()
    if check is None:
        return _post(server, payload)
    box: Dict[str, Any] = {}
    done = threading.Event()

    def work():
        box["thread"] = threading.get_ident()
        try:
            box["out"] = _post(server, payload)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["err"] = exc
        finally:
            done.set()

    threading.Thread(target=work, daemon=True, name="fleet-request").start()
    while not done.wait(CANCEL_POLL_SECONDS):
        if check():
            r = _open_responses.pop(box.get("thread"), None)
            if r is not None:
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass
            raise ScanCancelled("Cancelled by the user.")
    if "err" in box:
        raise box["err"]
    return box["out"]


def _cancellable_sleep(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        _check_cancelled()
        time.sleep(min(CANCEL_POLL_SECONDS, max(0.0, end - time.time())))


def _acquire(sem: threading.BoundedSemaphore) -> None:
    while not sem.acquire(timeout=CANCEL_POLL_SECONDS):
        _check_cancelled()


def call(role: str, system: str, user: str, images: Optional[Sequence[str]] = None, temperature: float = 0.1,
         fmt: Optional[str] = "json", options: Optional[Dict[str, Any]] = None, max_retries: int = 2,
         model: Optional[str] = None, cache: Optional[bool] = None) -> str:
    """One chat request for `role`; returns the reply text (raises ModelUnavailable when nothing answers)."""
    cfg = profiles.role(role)
    if cfg is None:
        raise ModelUnavailable(f"The active AI profile has no model for '{role}'.")
    prof = profiles.active()
    images = list(images or [])
    digests = [_file_digest(p) for p in images]
    encoded: Optional[List[str]] = None
    cacheable = cfg.get("cache", True) if cache is None else cache
    cacheable = cacheable and temperature <= 0.3
    candidates = [(model, resolve(role)[0][1])] if model and resolve(role) else resolve(role)
    last_error: Optional[BaseException] = None

    _check_cancelled()
    for model_name, server in candidates:
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "options": _options(temperature, {**(cfg.get("options") or {}), **(options or {})}),
            "keep_alive": -1 if prof["mode"] == "resident" else cfg.get("keep_alive", "3m"),
        }
        if fmt:
            payload["format"] = fmt
        if cfg.get("think") is False and any(model_name.startswith(f) for f in THINKING_FAMILIES):
            payload["think"] = False
        key = cache_key(model_name, json.loads(json.dumps(payload)), digests) if cacheable else None
        if key:
            hit = _cache_get(key)
            if hit is not None:
                _log_call(role, model_name, server, 0, images=len(images), cached=True)
                return hit
        if images and encoded is None:
            encoded = []
            for p in images:
                with open(p, "rb") as f:
                    encoded.append(base64.b64encode(f.read()).decode("ascii"))
        if images:
            payload["messages"][1]["images"] = encoded

        for attempt in range(max_retries + 1):
            _check_cancelled()
            if attempt:
                _cancellable_sleep(min(20, 4 * attempt))
            started = time.time()
            try:
                sem = _sem(role, server)
                _acquire(sem)
                try:
                    started = time.time()
                    out = _post_cancellable(server, payload)
                finally:
                    sem.release()
                content = out["content"]
                if not content.strip():
                    raise ValueError("empty reply")
                truncated = bool(out.get("truncated"))
                _log_call(role, model_name, server, (time.time() - started) * 1000, out.get("prompt_tokens"),
                          out.get("output_tokens"), len(images),
                          error=("stopped: the model was repeating itself" if out.get("looped")
                                 else "cut off at the output limit") if truncated else None)
                if key and not truncated:          # a cut-off reply is never replayed from the cache
                    _cache_put(key, role, model_name, content)
                return content
            except ScanCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _log_call(role, model_name, server, (time.time() - started) * 1000, images=len(images), ok=False,
                          error=exc)
                print(f"[fleet] {role} via {model_name}@{server['name']} failed (attempt {attempt + 1}): {str(exc)[:160]}")
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 404:           # model not found on that server - no point retrying it
                    break
        _broken[(server["url"], model_name)] = time.time()
    raise ModelUnavailable(f"No model answered for '{role}': {last_error}")


# ---------------------------------------------------------------------------
# Drop-in replacement for the old clients
# ---------------------------------------------------------------------------

class RoleLLM:
    """Same methods the nodes already use on OllamaLLM / OllamaVisionLLM, bound to one fleet role."""

    def __init__(self, role: str):
        self.role = role
        from nodes.llm_client import OllamaLLM
        self._json = OllamaLLM()._extract_json

    @property
    def model(self) -> str:
        return resolved_model(self.role) or ""

    def run(self, system: str, user: str, temperature: float = 0.2, max_retries: int = 2,
            images: Optional[Sequence[str]] = None, model: Optional[str] = None,
            options: Optional[Dict[str, Any]] = None) -> dict:
        text = call(self.role, system, user, images=images, temperature=temperature, max_retries=max_retries,
                    model=model, options=options)
        return self._json(_strip_thinking(text))

    def text(self, system: str, user: str, temperature: float = 0.2, **kw) -> str:
        return _strip_thinking(call(self.role, system, user, temperature=temperature, fmt=None, **kw))

    def transcribe(self, image, max_retries: int = 1) -> str:
        """Plain-text transcription of one scanned page ('' on failure) - the OCR role."""
        from nodes.vision_client import _OCR_SYSTEM, collapse_repeats
        try:
            content = call(self.role, _OCR_SYSTEM, "Transcribe this page.", images=[str(image)], temperature=0.0,
                           fmt=None, options={"num_predict": 3000, "repeat_penalty": 1.2, "repeat_last_n": 256},
                           max_retries=max_retries)
        except ModelUnavailable as exc:
            print(f"[fleet] OCR failed: {exc}")
            return ""
        return collapse_repeats(_strip_thinking(content))

    def ocr_model(self) -> str:
        return resolved_model("ocr") or ""

    def page_kind_model(self) -> str:
        return resolved_model("page_triage") or ""


def _strip_thinking(text: str) -> str:
    import re
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


_clients: Dict[str, RoleLLM] = {}


def llm(role: str) -> RoleLLM:
    if role not in _clients:
        _clients[role] = RoleLLM(role)
    return _clients[role]


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def embed(texts: Sequence[str], role: str = "embedder", batch: int = 32) -> List[Optional[List[float]]]:
    """Vectors for texts (None for any that could not be embedded). Cached per text."""
    texts = [str(t or "")[:4000] for t in texts]
    out: List[Optional[List[float]]] = [None] * len(texts)
    cands = resolve(role)
    if not cands:
        return out
    model, server = cands[0]
    todo = []
    keys = []
    for i, t in enumerate(texts):
        k = hashlib.sha256(json.dumps({"m": model, "t": t}).encode("utf-8")).hexdigest()
        keys.append(k)
        hit = _cache_get(k)
        if hit is not None:
            out[i] = json.loads(hit)
        elif t.strip():
            todo.append(i)
    for start in range(0, len(todo), batch):
        idx = todo[start:start + batch]
        started = time.time()
        try:
            with _sem(role, server):
                r = _http().post(server["url"] + "/api/embed", json={"model": model, "input": [texts[i] for i in idx],
                                                                     "keep_alive": "10m"}, timeout=300)
            r.raise_for_status()
            vecs = r.json().get("embeddings") or []
            _log_call(role, model, server, (time.time() - started) * 1000, images=0)
        except Exception as exc:  # noqa: BLE001
            _log_call(role, model, server, (time.time() - started) * 1000, ok=False, error=exc)
            print(f"[fleet] embedding failed: {str(exc)[:160]}")
            continue
        for i, v in zip(idx, vecs):
            out[i] = v
            _cache_put(keys[i], role, model, json.dumps(v))
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Parallel fan-out
# ---------------------------------------------------------------------------

def parallelism(role: Optional[str]) -> int:
    if not role:
        return max(1, profiles.active()["parallel"])
    cfg = profiles.role(role) or {}
    return max(1, int(cfg.get("parallel", 1)))


def pmap(fn: Callable[[Any], Any], items: Iterable[Any], role: Optional[str] = None,
         workers: Optional[int] = None) -> List[Any]:
    """fn over items with up to `workers` (default: the role's parallel setting) at once; results in order.

    Each task runs in a copy of the caller's context, so the scan's live log,
    output folders and call log keep working inside the threads.
    """
    items = list(items)
    n = min(len(items), workers or parallelism(role))
    if n <= 1:
        return [fn(it) for it in items]
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"fleet-{role or 'x'}") as ex:
        futures = [ex.submit(contextvars.copy_context().run, fn, it) for it in items]
        try:
            return [f.result() for f in futures]
        except BaseException:
            for f in futures:          # e.g. the scan was cancelled: do not start what is still queued
                f.cancel()
            raise
