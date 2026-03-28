from dataclasses import dataclass


@dataclass
class SearchResult:
    id: int                        # doc_id or rowid from sessions
    type: str                      # 'document' | 'session'
    l1: str                        # title + summary (~200 tok)
    l2: str                        # l1 + key_facts + decisions + code_sigs (~500 tok)
    raw: str                       # full raw_content (lazy — may be empty)
    score: float
    priority: str                  # P0 / P1 / P2
    source: str                    # 'hook' | 'explicit' | 'session_extract'

    def to_inject_block(self, tier: str = "l2") -> str:
        """Format as injection block for system-reminder."""
        if tier not in ("l1", "l2"):
            raise ValueError(f"tier must be 'l1' or 'l2', got {tier!r}")
        content = self.l2 if tier == "l2" else self.l1
        label = f"[{self.type.upper()}:{self.priority}]"
        return f"{label}\n{content}"
