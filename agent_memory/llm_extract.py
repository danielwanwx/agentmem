"""llm_extract.py — LLM-based field extraction via Ollama qwen3:8b.

Replaces rule-based extract_fields() for documents where rules fail:
  - table-first markdown (ticket templates)
  - YAML frontmatter
  - Chinese prose
  - mixed-format docs

Fallback: if Ollama unavailable or response malformed → extract_fields().
code_sigs and metrics always come from rules (LLM doesn't do better here).
"""
import json
from typing import Any

from .llm import chat
from .extract import extract_fields

# Truncate content sent to LLM — first 3000 chars covers title/summary/key sections
# for most docs. Saves tokens and keeps latency low.
MAX_CHARS = 3000

_PROMPT = """\
Extract structured information from the document below.
Return ONLY a JSON object with exactly these fields:

{{
  "title":     "<one-line title>",
  "summary":   "<2-3 sentences: what this document is about>",
  "key_facts": ["<fact 1>", "<fact 2>", ...],
  "decisions": ["<decision or recommendation 1>", ...]
}}

Rules:
- summary: plain prose, NOT a table row, NOT YAML, NOT a header. 2-3 sentences max.
- key_facts: up to 6 items. Each is a self-contained technical fact, config value, or insight.
- decisions: up to 4 items. Only include explicit decisions/recommendations. Empty array if none.
- Respond in the same language as the document (Chinese doc → Chinese summary).

Document:
{content}"""


def llm_extract(content: str, title_hint: str = "",
                timeout: float = 30.0) -> dict[str, Any] | None:
    """Extract fields using LLM. Returns dict or None if extraction fails.

    None means caller should fallback to rule-based extraction.
    timeout: increase for batch jobs where Ollama is under load (default 30s).
    """
    truncated = content[:MAX_CHARS].strip()
    if not truncated:
        return None

    # /no_think disables qwen3/3.5 chain-of-thought (works for both native and GGUF)
    messages = [{"role": "user", "content": "/no_think\n" + _PROMPT.format(content=truncated)}]
    raw = chat(messages, timeout=timeout, think=False)
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    title   = str(data.get("title", "") or title_hint or "").strip()
    summary = str(data.get("summary", "") or "").strip()

    # Reject if summary is still table-like or too short
    if len(summary) < 20 or summary.startswith("|") or summary.startswith("---"):
        return None

    key_facts = [str(f).strip() for f in data.get("key_facts", []) if str(f).strip()]
    decisions = [str(d).strip() for d in data.get("decisions", []) if str(d).strip()]

    return {
        "title":     title or title_hint,
        "summary":   summary,
        "key_facts": key_facts[:6],
        "decisions": decisions[:4],
        "sections":  [],   # not needed from LLM
        "code_sigs": [],   # filled by rules below
        "metrics":   [],   # filled by rules below
    }


def extract_with_llm(content: str, title_hint: str = "") -> dict[str, Any]:
    """Primary extraction entry point: LLM + rule fallback.

    Always returns a valid fields dict (never raises).
    - LLM handles: title, summary, key_facts, decisions
    - Rules handle: code_sigs, metrics (LLM adds no value here)
    - If LLM fails: full rule-based fallback
    """
    rule_fields = extract_fields(content, title_hint=title_hint)

    llm_fields = llm_extract(content, title_hint=title_hint)
    if llm_fields is None:
        # LLM unavailable or failed — use rules as-is
        return rule_fields

    # Merge: LLM wins on summary/key_facts/decisions/title
    # Rules win on code_sigs/metrics
    llm_fields["code_sigs"] = rule_fields.get("code_sigs", [])
    llm_fields["metrics"]   = rule_fields.get("metrics", [])
    llm_fields["sections"]  = rule_fields.get("sections", [])

    # Prefer LLM title but fall back to rule title if LLM produced empty
    if not llm_fields["title"]:
        llm_fields["title"] = rule_fields.get("title", title_hint)

    return llm_fields
