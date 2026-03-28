"""llm.py — Ollama chat client for structured extraction.

Graceful degradation: returns None if Ollama is unavailable.
Callers never need to handle exceptions.
"""
import httpx

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
EXTRACT_MODEL   = "qwen3:8b"


def chat(
    messages: list[dict],
    timeout: float = 30.0,
    think: bool = False,
    json_format: bool = True,
) -> str | None:
    """Call Ollama /api/chat. Returns content string or None on failure.

    Args:
        messages:    OpenAI-style [{"role": ..., "content": ...}]
        timeout:     seconds before giving up
        think:       enable qwen3 chain-of-thought (slower, better reasoning)
        json_format: force Ollama to return valid JSON
    """
    body: dict = {
        "model":   EXTRACT_MODEL,
        "messages": messages,
        "stream":  False,
        "options": {"temperature": 0},
    }
    # think param only supported by native Ollama models (not GGUF imports)
    if think:
        body["think"] = True
    if json_format:
        body["format"] = "json"

    try:
        resp = httpx.post(OLLAMA_CHAT_URL, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception:
        return None
