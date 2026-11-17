"""
Ollama Vision Client - ConflictScan / RFI Automation
=====================================================
Multimodal sibling of nodes/llm_client.py. Same request/retry/JSON-parsing
behaviour, except each call also attaches one or more images (as base64) to
the user message, for a vision-capable Ollama model (e.g. gemma4:e4b).

Also provides transcribe(): plain-text OCR of a scanned page, used when a
non-drawing PDF (BOQ, specification, letter, schedule) has no text layer -
most real RFI attachments are scans.
"""

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional, Union

from nodes.llm_client import OllamaLLM, OLLAMA_URL, ollama_options

VISION_MODEL_NAME = os.environ.get("CONFLICTSCAN_VISION_MODEL", "gemma4:e4b")

# Preferred OCR models, best first. Override with CONFLICTSCAN_OCR_MODEL.
# qwen2.5vl keeps table structure and reads Arabic best of the small local
# models tried; glm-ocr is faster and reads English well.
OCR_MODEL_PREFERENCE = ["qwen2.5vl:7b", "qwen2.5vl", "glm-ocr"]

_OCR_SYSTEM = (
    "You transcribe scanned construction documents (BOQs, specifications, schedules, letters, "
    "forms). Output plain text only. Write every table row on one line with its cells separated "
    "by ' | ', in reading order. Keep Arabic text in Arabic and English in English exactly as "
    "printed; keep item numbers, codes, quantities, units and references exactly. The page may be "
    "rotated - read it in its correct orientation. Do not summarise, translate, explain or repeat "
    "lines. Ignore stamps, signatures and logos."
)


def collapse_repeats(text: str, max_line: int = 600) -> str:
    """
    Undo the degenerate loops small OCR models fall into: a phrase repeated
    back-to-back, or the same line emitted over and over. Collapses each run to
    a single occurrence and caps runaway lines.
    """
    if not text:
        return ""
    # A run needs 4+ whitespace-separated copies of a 2+ character phrase, so
    # numbers ("1000") and legitimately repeated table cells survive.
    out = re.sub(r"(\S.{0,60}?\S)(?:\s+\1){3,}", r"\1", text)
    lines, seen = [], {}
    for line in out.splitlines():
        line = line.rstrip()
        key = re.sub(r"\s+", " ", line).strip().lower()
        if key:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 2:
                continue
        lines.append(line[:max_line])
    return "\n".join(lines).strip()


class OllamaVisionLLM(OllamaLLM):
    def __init__(self, model: str = VISION_MODEL_NAME, url: str = OLLAMA_URL):
        super().__init__(model=model, url=url)
        self._ocr_model: Optional[str] = None

    def run(
        self,
        system: str,
        user: str,
        images: List[Union[str, Path]],
        temperature: float = 0.2,
        max_retries: int = 3,
        model: Optional[str] = None,
    ) -> dict:
        """
        Same contract as OllamaLLM.run, plus *images*: a list of file paths to
        PNG/JPEG tiles. Each is base64-encoded and attached to the user
        message, per Ollama's multimodal chat format. *model* overrides the
        vision model for this one call.
        """
        images_b64 = [self._encode_image(p) for p in images]

        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user, "images": images_b64},
            ],
            "stream": False,
            "format": "json",
            "options": ollama_options(temperature),
        }

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 5 * attempt
                    print(f"[vision] Retry {attempt}/{max_retries} after {wait_time}s...")
                    time.sleep(wait_time)

                print(f"[vision] Calling vision LLM ({len(images_b64)} image(s))...")
                import requests

                response = requests.post(
                    self.url,
                    headers={"Content-Type": "application/json"},
                    data=json.dumps(payload),
                    timeout=600,
                )
                response.raise_for_status()
                resp_json = response.json()

                content = resp_json.get("message", {}).get("content", "")
                if not content:
                    print("[vision] Empty response from vision LLM")
                    raise ValueError("Vision LLM returned empty content")

                print(f"[vision] Vision LLM responded ({len(content)} chars)")
                parsed = self._extract_json(content)
                print("[vision] JSON parsed successfully")
                return parsed

            except Exception as e:
                print(f"[vision] Error (attempt {attempt + 1}): {str(e)[:200]}")
                if attempt == max_retries:
                    raise Exception(f"Vision LLM failed after {max_retries} retries: {e}")

        raise Exception("Vision LLM max retries exceeded")

    # -----------------------------------------------------------------------
    # OCR
    # -----------------------------------------------------------------------

    def ocr_model(self) -> str:
        """The OCR model to use: env override, else the first preferred model installed, else the vision model."""
        if self._ocr_model:
            return self._ocr_model
        override = os.environ.get("CONFLICTSCAN_OCR_MODEL")
        if override:
            self._ocr_model = override
            return override
        installed: List[str] = []
        try:
            import requests
            tags_url = self.url.replace("/api/chat", "/api/tags")
            installed = [m.get("name", "") for m in requests.get(tags_url, timeout=5).json().get("models", [])]
        except Exception:
            pass
        for preferred in OCR_MODEL_PREFERENCE:
            match = next((name for name in installed if name == preferred or name.startswith(preferred + ":")), None)
            if match:
                self._ocr_model = match
                break
        else:
            self._ocr_model = self.model
        print(f"[vision] OCR model: {self._ocr_model}")
        return self._ocr_model

    def page_kind_model(self) -> str:
        """
        Model for the quick 'is this scanned page a drawing or a document?' check.
        A general document-vision model (qwen2.5vl) answers it reliably; a pure
        OCR model cannot classify, so the main vision model is the fallback.
        """
        override = os.environ.get("CONFLICTSCAN_PAGE_KIND_MODEL")
        if override:
            return override
        ocr = self.ocr_model()
        return ocr if ocr.startswith("qwen") else self.model

    def transcribe(self, image: Union[str, Path], max_retries: int = 1) -> str:
        """Plain-text transcription of one scanned page image ('' on failure)."""
        import requests

        model = self.ocr_model()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _OCR_SYSTEM},
                {"role": "user", "content": "Transcribe this page.", "images": [self._encode_image(image)]},
            ],
            "stream": False,
            "options": ollama_options(0.0, num_predict=3000, repeat_penalty=1.2, repeat_last_n=256),
        }
        if model == self.model:
            # The general vision model only answers reliably in JSON mode.
            payload["format"] = "json"
            payload["messages"][0]["content"] += ' Return JSON: {"lines": ["<one transcribed line>", ...]}.'

        for attempt in range(max_retries + 1):
            try:
                response = requests.post(self.url, json=payload, timeout=600)
                response.raise_for_status()
                content = response.json().get("message", {}).get("content", "") or ""
                if payload.get("format") == "json":
                    lines = self._extract_json(content).get("lines") or []
                    content = "\n".join(str(line) for line in lines)
                return collapse_repeats(content)
            except Exception as exc:
                print(f"[vision] OCR attempt {attempt + 1} failed: {str(exc)[:200]}")
        return ""

    @staticmethod
    def _encode_image(path: Union[str, Path]) -> str:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")


vision_llm_client = OllamaVisionLLM()
