"""Tests for extract.py conversation parsing."""

import json
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from extract import extract_conversation, extract_text_content, format_conversation_for_analysis


def make_jsonl(messages):
    """Write messages as JSONL to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for msg in messages:
        f.write(json.dumps(msg) + "\n")
    f.close()
    return Path(f.name)


BASIC_SESSION = [
    {
        "type": "user",
        "timestamp": "2026-03-01T10:00:00Z",
        "message": {
            "content": [{"type": "text", "text": "Fix the bug in main.py"}],
        },
    },
    {
        "type": "assistant",
        "timestamp": "2026-03-01T10:00:05Z",
        "sessionId": "test-session-1",
        "version": "1.0",
        "slug": "fix-bug",
        "cwd": "/tmp/test",
        "gitBranch": "main",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "content": [
                {"type": "text", "text": "Let me read the file first."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/test/main.py"}},
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
            },
        },
    },
    {
        "type": "user",
        "timestamp": "2026-03-01T10:00:10Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "123",
                    "content": "def main():\n    print('hello')\n",
                    "is_error": False,
                },
            ],
        },
    },
    {
        "type": "assistant",
        "timestamp": "2026-03-01T10:00:15Z",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "content": [
                {"type": "text", "text": "Fixed the bug."},
            ],
            "usage": {"input_tokens": 150, "output_tokens": 30},
        },
    },
]


def test_extract_basic_conversation():
    fp = make_jsonl(BASIC_SESSION)
    try:
        convo = extract_conversation(fp)
        meta = convo["metadata"]
        stats = convo["stats"]

        assert meta["session_id"] == "test-session-1"
        assert meta["slug"] == "fix-bug"
        assert meta["model"] == "claude-sonnet-4-20250514"
        assert meta["cwd"] == "/tmp/test"
        assert meta["git_branch"] == "main"

        assert stats["message_count"] == 4
        assert stats["errors"] == 0
        assert stats["rejections"] == 0
        assert stats["total_input_tokens"] == 450  # 100+200+150
        assert stats["total_output_tokens"] == 80   # 50+30
        assert stats["tool_usage"]["Read"] == 1
    finally:
        fp.unlink()


def test_extract_errors_and_rejections():
    session = [
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:00Z",
            "message": {
                "content": [{"type": "text", "text": "do something"}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:10Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": "The user rejected this tool call",
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:20Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": "File not found: /tmp/missing.py",
                    },
                ],
            },
        },
    ]
    fp = make_jsonl(session)
    try:
        convo = extract_conversation(fp)
        assert convo["stats"]["errors"] == 2
        assert convo["stats"]["rejections"] == 1
    finally:
        fp.unlink()


def test_parser_rejected_is_not_user_rejection():
    """'rejected by the parser' is an error but NOT a user rejection."""
    session = [
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": "The argument was rejected by the parser",
                    },
                ],
            },
        },
    ]
    fp = make_jsonl(session)
    try:
        convo = extract_conversation(fp)
        assert convo["stats"]["errors"] == 1
        assert convo["stats"]["rejections"] == 0
    finally:
        fp.unlink()


def test_user_rejected_is_rejection():
    """'The user rejected this tool call' is both an error and a rejection."""
    session = [
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": "The user rejected this tool call",
                    },
                ],
            },
        },
    ]
    fp = make_jsonl(session)
    try:
        convo = extract_conversation(fp)
        assert convo["stats"]["errors"] == 1
        assert convo["stats"]["rejections"] == 1
    finally:
        fp.unlink()


def test_extract_text_content_string():
    assert extract_text_content("hello") == "hello"


def test_extract_text_content_blocks():
    blocks = [
        {"type": "text", "text": "First part"},
        {"type": "text", "text": "Second part"},
    ]
    result = extract_text_content(blocks)
    assert "First part" in result
    assert "Second part" in result


def test_extract_text_content_tool_use():
    blocks = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
    ]
    result = extract_text_content(blocks)
    assert "Bash" in result
    assert "ls -la" in result


def test_extract_text_content_tool_result_error():
    blocks = [
        {"type": "tool_result", "is_error": True, "content": "Permission denied"},
    ]
    result = extract_text_content(blocks)
    assert "ERROR" in result
    assert "Permission denied" in result


def test_format_conversation():
    fp = make_jsonl(BASIC_SESSION)
    try:
        convo = extract_conversation(fp)
        text = format_conversation_for_analysis(convo)
        assert "fix-bug" in text
        assert "claude-sonnet-4-20250514" in text
        assert "[USER]" in text
        assert "[ASSISTANT]" in text
    finally:
        fp.unlink()


def test_extract_empty_file():
    fp = make_jsonl([])
    try:
        convo = extract_conversation(fp)
        assert convo["stats"]["message_count"] == 0
        assert convo["stats"]["errors"] == 0
    finally:
        fp.unlink()


def test_extract_malformed_jsonl():
    """Malformed lines are skipped gracefully."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    f.write("not valid json\n")
    f.write("{}\n")
    f.write(json.dumps(BASIC_SESSION[0]) + "\n")
    f.close()
    fp = Path(f.name)
    try:
        convo = extract_conversation(fp)
        # Only the valid user message is parsed
        assert convo["stats"]["message_count"] == 1
    finally:
        fp.unlink()


def test_duration_calculation():
    fp = make_jsonl(BASIC_SESSION)
    try:
        convo = extract_conversation(fp)
        assert convo["stats"]["duration_minutes"] is not None
        # 15 seconds between first and last message
        assert convo["stats"]["duration_minutes"] == 0.2  # 15s = 0.25min rounded to 0.2
    finally:
        fp.unlink()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
