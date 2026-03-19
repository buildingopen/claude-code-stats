import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from generate_findings import extract_summary, main, OUTPUT_FILE, PATTERNS_DIR


class TestExtractSummary:
    def test_empty_input(self):
        assert extract_summary("") == ""

    def test_normal_content(self):
        content = "Line 1\nLine 2\nLine 3"
        assert extract_summary(content) == content

    def test_preserves_all_content(self):
        content = "Summary here\nMore summary\n## Per-Session Details\nDetail 1\nDetail 2"
        result = extract_summary(content)
        assert "Summary here" in result
        assert "Detail 1" in result
        assert "Detail 2" in result

    def test_no_truncation(self):
        content = "\n".join(f"Line {i}" for i in range(200))
        result = extract_summary(content)
        assert "Line 199" in result
        assert "truncated" not in result


class TestGenerateFindings:
    def test_generate_findings_creates_file(self, tmp_path, monkeypatch):
        """Test that main() creates FINDINGS.md by reading from patterns/*.md."""
        import generate_findings

        # Point PATTERNS_DIR and OUTPUT_FILE to tmp_path
        patterns_dir = tmp_path / "patterns"
        patterns_dir.mkdir()
        output_file = tmp_path / "FINDINGS.md"

        monkeypatch.setattr(generate_findings, "PATTERNS_DIR", patterns_dir)
        monkeypatch.setattr(generate_findings, "OUTPUT_FILE", output_file)

        # Create a sample pattern file matching one of the PATTERN_ORDER entries
        (patterns_dir / "error_taxonomy.md").write_text("Error stats: 5 total errors")

        main()

        assert output_file.exists()
        content = output_file.read_text()
        assert "Transcript Analysis Findings" in content
        assert "Error Taxonomy" in content
        assert "Error stats: 5 total errors" in content
        # Patterns without files get "Not yet generated" message
        assert "Not yet generated" in content

    def test_no_code_fences_in_output(self, tmp_path, monkeypatch):
        """Pattern .md files already contain proper markdown, no code fences needed."""
        import generate_findings

        patterns_dir = tmp_path / "patterns"
        patterns_dir.mkdir()
        output_file = tmp_path / "FINDINGS.md"

        monkeypatch.setattr(generate_findings, "PATTERNS_DIR", patterns_dir)
        monkeypatch.setattr(generate_findings, "OUTPUT_FILE", output_file)

        # Create pattern files with markdown content (no code fences in source)
        (patterns_dir / "error_taxonomy.md").write_text("# Errors\n\n| Type | Count |\n|------|-------|\n| Timeout | 5 |")
        (patterns_dir / "session_outcomes.md").write_text("# Outcomes\n\n- SUCCESS: 10\n- FAILURE: 2")

        main()

        content = output_file.read_text()
        # Output must not wrap pattern content in code fences
        assert "```\n# Errors" not in content
        assert "```\n# Outcomes" not in content
