import requests
import json
import time
import re

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "gemma4:e4b"

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
            "options": {
                "temperature": temperature,
                "num_predict": 8192  # Increased token limit
            }
        }

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 5 * attempt
                    print(f"[llm] Retry {attempt}/{max_retries} after {wait_time}s...")
                    time.sleep(wait_time)
                
                print(f"[llm] Calling LLM...")
                response = requests.post(
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
        
        # Last resort: an empty object. Callers read the keys they expect with
        # .get() and fall back on their own, so an empty dict degrades cleanly
        # whatever the caller asked for.
        print("[llm] Could not parse JSON - returning empty result")
        return {}

    def test_connection(self) -> bool:
        """Test if Ollama is running and model is loaded"""
        try:
            # List models
            response = requests.get("http://localhost:11434/api/tags", timeout=5)
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