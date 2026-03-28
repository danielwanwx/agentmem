"""Rule-based field extraction from markdown content. Zero LLM cost."""
import re
from typing import Any


def build_l1(fields: dict) -> str:
    """Build L1 injection string (~200 tok)."""
    parts = []
    if fields.get("title"):
        parts.append(f"### {fields['title']}")
    if fields.get("summary"):
        parts.append(fields["summary"])
    if fields.get("sections"):
        parts.append("Sections: " + ", ".join(fields["sections"]))
    if fields.get("key_facts"):
        facts = fields["key_facts"][:4]
        parts.append("Key facts:\n" + "\n".join(f"  - {f}" for f in facts))
    return "\n".join(parts)


def build_l2(fields: dict) -> str:
    """Build L2 injection string (~500 tok): L1 + decisions + code_sigs + metrics."""
    l1 = build_l1(fields)
    extras = []
    if fields.get("decisions"):
        extras.append("Key decisions:\n" + "\n".join(
            f"  - {d}" for d in fields["decisions"][:4]))
    if fields.get("code_sigs"):
        extras.append("Code/CLI:\n" + "\n".join(
            f"  {s}" for s in fields["code_sigs"][:4]))
    if fields.get("metrics"):
        extras.append("Metrics: " + " | ".join(fields["metrics"][:4]))
    return l1 + ("\n" + "\n".join(extras) if extras else "")


def extract_fields(content: str, title_hint: str = "") -> dict[str, Any]:
    """Rule-based extraction: zero LLM cost.
    Returns: title, summary, sections, key_facts, code_sigs, metrics, decisions
    """
    # Normalize literal \n escape sequences (e.g. from bash double-quoted strings)
    # so that "# Title\nBody text" is treated the same as a real two-line string.
    content = content.replace("\\n", "\n").replace("\\t", "\t")
    lines = content.splitlines()
    result = {
        "title": title_hint,
        "summary": "",
        "sections": [],
        "key_facts": [],
        "code_sigs": [],
        "metrics": [],
        "decisions": [],
    }
    if not content.strip():
        return result

    in_code_block = False
    first_para_lines = []
    past_title = False
    past_summary = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Code block tracking
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
            else:
                in_code_block = False
            continue

        if in_code_block:
            # First line of code block = signature
            if stripped:
                if stripped not in result["code_sigs"]:
                    result["code_sigs"].append(stripped[:120])
            continue

        # Title: first # heading
        if stripped.startswith("# "):
            if not result["title"]:
                result["title"] = stripped[2:].strip()
            past_title = True
            continue

        # Sections: ## headings
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            result["sections"].append(section)
            past_summary = True
            continue

        # Summary: first non-empty paragraph after title, before first ##
        if past_title and not past_summary:
            if stripped:
                first_para_lines.append(stripped)
            elif first_para_lines:
                past_summary = True

        # Key facts: "- **bold**: value" or "- **bold** value"
        if re.match(r"^[-*]\s+\*\*[^*]+\*\*[: ]", stripped):
            result["key_facts"].append(stripped.lstrip("-* "))

        # Decisions: lines starting with decision signals
        if re.match(r"^(Decision:|Chose|Recommend|→ Use |\*\*Decision\*\*)", stripped):
            result["decisions"].append(stripped[:200])

        # Metrics: lines with number + unit
        if re.search(r"\b\d+[\.,]?\d*\s*(ms|tok|%|GB|MB|KB|x|K|M|s\b)", stripped):
            if stripped not in result["metrics"]:
                result["metrics"].append(stripped[:150])

    result["summary"] = " ".join(first_para_lines)[:500]
    return result
