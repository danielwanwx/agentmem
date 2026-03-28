"""Tests for rule-based field extraction."""
from agent_memory.extract import extract_fields, build_l1, build_l2


class TestExtractFields:
    def test_extract_title_from_heading(self):
        fields = extract_fields("# My Document\n\nBody text here.")
        assert fields["title"] == "My Document"

    def test_extract_summary_first_paragraph(self):
        content = "# Title\n\nThis is the summary paragraph.\n\n## Section"
        fields = extract_fields(content)
        assert "summary paragraph" in fields["summary"]

    def test_extract_key_facts_bold_pattern(self):
        content = "- **timeout**: 30 seconds\n- **retries**: 3 times"
        fields = extract_fields(content)
        assert len(fields["key_facts"]) == 2

    def test_extract_decisions(self):
        content = "Decision: Use PostgreSQL instead of MySQL"
        fields = extract_fields(content)
        assert len(fields["decisions"]) >= 1
        assert "PostgreSQL" in fields["decisions"][0]

    def test_extract_code_sigs_from_code_block(self):
        content = "```python\ndef hello_world():\n    pass\n```"
        fields = extract_fields(content)
        assert len(fields["code_sigs"]) >= 1

    def test_extract_metrics(self):
        content = "Response time: 45ms\nMemory usage: 128MB"
        fields = extract_fields(content)
        assert len(fields["metrics"]) >= 1

    def test_extract_sections(self):
        content = "# Title\n\nSummary\n\n## Setup\n\nContent\n\n## Usage\n\nMore"
        fields = extract_fields(content)
        assert "Setup" in fields["sections"]
        assert "Usage" in fields["sections"]

    def test_empty_content(self):
        fields = extract_fields("")
        assert fields["title"] == ""
        assert fields["key_facts"] == []

    def test_title_hint_used_when_no_heading(self):
        fields = extract_fields("Just some text", title_hint="Hint Title")
        assert fields["title"] == "Hint Title"

    def test_escaped_newlines_normalized(self):
        content = "# Title\\nSummary text here"
        fields = extract_fields(content)
        assert fields["title"] == "Title"


class TestBuildL1:
    def test_includes_title_and_summary(self):
        fields = {"title": "My Doc", "summary": "A brief summary",
                  "sections": [], "key_facts": ["fact1"], "decisions": []}
        l1 = build_l1(fields)
        assert "My Doc" in l1
        assert "A brief summary" in l1

    def test_includes_key_facts(self):
        fields = {"title": "Doc", "summary": "", "sections": [],
                  "key_facts": ["fact1", "fact2"], "decisions": []}
        l1 = build_l1(fields)
        assert "fact1" in l1


class TestBuildL2:
    def test_includes_l1_plus_decisions(self):
        fields = {"title": "Doc", "summary": "Sum", "sections": [],
                  "key_facts": ["f1"], "decisions": ["Use X"],
                  "code_sigs": ["func()"], "metrics": ["10ms"]}
        l2 = build_l2(fields)
        assert "Use X" in l2
        assert "func()" in l2
        assert "10ms" in l2
