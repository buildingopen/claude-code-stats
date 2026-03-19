#!/usr/bin/env python3
"""
Analyze Claude Code JSONL sessions for retry loops and wasted effort.

Patterns detected:
1. Same tool called 3+ times in a row with similar inputs (retry loops)
2. Read -> Edit -> Error repeated (edit-fail loops)
3. Bash commands that fail and get retried with minor variations
4. WebSearch/WebFetch called multiple times for similar queries
5. Agent code immediately rejected by user
"""

import json
import os
import sys
from pathlib import Path
from collections import defaultdict
from difflib import SequenceMatcher
from datetime import datetime

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, MIN_SESSION_SIZE, MAX_SESSIONS, output_path as _output_path, REJECTION_PATTERNS
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, MIN_SESSION_SIZE, MAX_SESSIONS, output_path as _output_path, REJECTION_PATTERNS
PROJECTS_DIR = CLAUDE_PROJECTS_DIR
MIN_SIZE = MIN_SESSION_SIZE
SIMILARITY_THRESHOLD = 0.6  # For detecting similar inputs


def find_sessions(projects_dir: Path, min_size: int, max_sessions: int) -> list[Path]:
    """Find the most recent JSONL sessions above min_size, excluding subagent files."""
    sessions = []
    for jsonl in projects_dir.rglob("*.jsonl"):
        # Skip subagent files
        if "subagent" in str(jsonl):
            continue
        if jsonl.stat().st_size >= min_size:
            sessions.append(jsonl)

    # Sort by modification time, most recent first
    sessions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[:max_sessions]


def parse_session(path: Path) -> list[dict]:
    """Parse a JSONL file into a list of message objects."""
    messages = []
    with open(path, "r", errors="replace") as f:
        for line_num, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                obj["_line_num"] = line_num
                messages.append(obj)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    return messages


def extract_tool_calls(messages: list[dict]) -> list[dict]:
    """Extract sequential tool calls with their results from messages."""
    tool_calls = []
    # Map tool_use_id to tool call info
    pending = {}

    for msg in messages:
        msg_type = msg.get("type")
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue

        usage = msg.get("message", {}).get("usage", {})
        timestamp = msg.get("timestamp", "")

        if msg_type == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    call = {
                        "name": block.get("name", "unknown"),
                        "input": block.get("input", {}),
                        "id": tool_id,
                        "result_error": None,
                        "result_text": "",
                        "usage": usage,
                        "timestamp": timestamp,
                        "line_num": msg.get("_line_num", 0),
                    }
                    pending[tool_id] = call
                    tool_calls.append(call)

        elif msg_type == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id", "")
                    is_error = block.get("is_error", False)
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        texts = []
                        for item in result_content:
                            if isinstance(item, dict):
                                texts.append(item.get("text", ""))
                        result_text = "\n".join(texts)
                    elif isinstance(result_content, str):
                        result_text = result_content
                    else:
                        result_text = str(result_content)

                    if tool_id in pending:
                        pending[tool_id]["result_error"] = is_error
                        pending[tool_id]["result_text"] = result_text[:500]

    return tool_calls


def similar(a: str, b: str) -> float:
    """Return similarity ratio between two strings."""
    if not a or not b:
        return 0.0
    # Truncate for performance
    a = a[:500]
    b = b[:500]
    return SequenceMatcher(None, a, b).ratio()


def get_tool_signature(call: dict) -> str:
    """Create a signature string for a tool call for similarity comparison."""
    name = call["name"]
    inp = call["input"]
    if name == "Bash":
        return inp.get("command", "")
    elif name == "Read":
        return inp.get("file_path", "")
    elif name == "Edit":
        return f"{inp.get('file_path', '')}|{inp.get('old_string', '')[:100]}"
    elif name == "Write":
        return inp.get("file_path", "")
    elif name == "Grep":
        return f"{inp.get('pattern', '')}|{inp.get('path', '')}|{inp.get('glob', '')}"
    elif name == "Glob":
        return f"{inp.get('pattern', '')}|{inp.get('path', '')}"
    elif name in ("WebSearch", "WebFetch"):
        if name == "WebSearch":
            return inp.get("query", "")
        return inp.get("url", "")
    else:
        return json.dumps(inp, sort_keys=True)[:200]


def estimate_tokens(calls: list[dict]) -> int:
    """Estimate tokens wasted in a sequence of tool calls."""
    total = 0
    for call in calls:
        u = call.get("usage", {})
        total += u.get("input_tokens", 0) + u.get("output_tokens", 0)
        total += u.get("cache_creation_input_tokens", 0)
    # If no usage data, estimate based on count
    if total == 0:
        total = len(calls) * 2000  # rough estimate
    return total


def detect_consecutive_retries(tool_calls: list[dict]) -> list[dict]:
    """Pattern 1: Same tool called 3+ times in a row with similar inputs."""
    findings = []
    i = 0
    while i < len(tool_calls):
        run = [tool_calls[i]]
        j = i + 1
        while j < len(tool_calls):
            if tool_calls[j]["name"] != tool_calls[i]["name"]:
                break
            sig_i = get_tool_signature(tool_calls[i])
            sig_j = get_tool_signature(tool_calls[j])
            if similar(sig_i, sig_j) >= SIMILARITY_THRESHOLD:
                run.append(tool_calls[j])
                j += 1
            else:
                break

        if len(run) >= 3:
            findings.append({
                "pattern": "consecutive_retry",
                "tool": run[0]["name"],
                "count": len(run),
                "calls": run,
                "signature": get_tool_signature(run[0])[:120],
                "had_errors": sum(1 for c in run if c["result_error"]),
                "estimated_tokens": estimate_tokens(run),
            })
        i = j if j > i + 1 else i + 1

    return findings


def detect_edit_fail_loops(tool_calls: list[dict]) -> list[dict]:
    """Pattern 2: Read -> Edit -> Error repeated."""
    findings = []
    i = 0
    while i < len(tool_calls) - 1:
        # Look for Edit calls that error, possibly preceded by Read
        if tool_calls[i]["name"] == "Edit" and tool_calls[i]["result_error"]:
            loop_calls = [tool_calls[i]]
            target_file = tool_calls[i]["input"].get("file_path", "")
            j = i + 1
            while j < len(tool_calls):
                c = tool_calls[j]
                # Part of the loop if it's Read or Edit on same file
                if c["name"] == "Read" and c["input"].get("file_path", "") == target_file:
                    loop_calls.append(c)
                    j += 1
                elif c["name"] == "Edit" and c["input"].get("file_path", "") == target_file:
                    loop_calls.append(c)
                    j += 1
                    if not c["result_error"]:
                        break  # Loop broken by successful edit
                else:
                    break

            edit_count = sum(1 for c in loop_calls if c["name"] == "Edit")
            error_count = sum(1 for c in loop_calls if c["name"] == "Edit" and c["result_error"])
            if edit_count >= 2 and error_count >= 2:
                findings.append({
                    "pattern": "edit_fail_loop",
                    "file": target_file,
                    "total_attempts": edit_count,
                    "errors": error_count,
                    "calls": loop_calls,
                    "resolved": any(c["name"] == "Edit" and not c["result_error"] for c in loop_calls),
                    "estimated_tokens": estimate_tokens(loop_calls),
                })
            i = j
        else:
            i += 1

    return findings


def detect_bash_retries(tool_calls: list[dict]) -> list[dict]:
    """Pattern 3: Bash commands that fail and get retried with minor variations."""
    findings = []
    i = 0
    while i < len(tool_calls):
        c = tool_calls[i]
        if c["name"] != "Bash" or not c["result_error"]:
            i += 1
            continue

        # Look ahead for similar bash commands
        base_cmd = c["input"].get("command", "")
        run = [c]
        j = i + 1
        while j < len(tool_calls):
            nxt = tool_calls[j]
            if nxt["name"] != "Bash":
                # Allow non-Bash calls in between (like Read to check something)
                if j - i < 8:  # don't look too far
                    j += 1
                    continue
                break
            nxt_cmd = nxt["input"].get("command", "")
            if similar(base_cmd, nxt_cmd) >= 0.4:
                run.append(nxt)
                j += 1
            else:
                break

        if len(run) >= 3:
            findings.append({
                "pattern": "bash_retry",
                "command_base": base_cmd[:150],
                "count": len(run),
                "calls": run,
                "errors": sum(1 for r in run if r["result_error"]),
                "resolved": not run[-1]["result_error"],
                "estimated_tokens": estimate_tokens(run),
            })
            i = j
        else:
            i += 1

    return findings


def detect_search_retries(tool_calls: list[dict]) -> list[dict]:
    """Pattern 4: WebSearch/WebFetch called multiple times for similar queries."""
    findings = []
    search_calls = [(idx, c) for idx, c in enumerate(tool_calls) if c["name"] in ("WebSearch", "WebFetch")]

    i = 0
    while i < len(search_calls):
        idx_i, call_i = search_calls[i]
        sig_i = get_tool_signature(call_i)
        cluster = [call_i]

        j = i + 1
        while j < len(search_calls):
            idx_j, call_j = search_calls[j]
            sig_j = get_tool_signature(call_j)
            if similar(sig_i, sig_j) >= SIMILARITY_THRESHOLD:
                cluster.append(call_j)
                j += 1
            else:
                break

        if len(cluster) >= 3:
            findings.append({
                "pattern": "search_retry",
                "tool": cluster[0]["name"],
                "query": sig_i[:120],
                "count": len(cluster),
                "calls": cluster,
                "estimated_tokens": estimate_tokens(cluster),
            })
        i = j if j > i + 1 else i + 1

    return findings


def detect_user_rejections(tool_calls: list[dict]) -> list[dict]:
    """Pattern 5: Agent generating code that gets immediately rejected by user."""
    findings = []
    rejections = []

    for c in tool_calls:
        result_lower = c.get("result_text", "").lower()
        if c["result_error"] and any(pat in result_lower for pat in REJECTION_PATTERNS):
            rejections.append(c)

    # Group consecutive rejections
    if len(rejections) >= 2:
        i = 0
        while i < len(rejections):
            cluster = [rejections[i]]
            j = i + 1
            while j < len(rejections):
                # Check if rejections are close together in the tool_calls sequence
                line_diff = abs(rejections[j]["line_num"] - rejections[j - 1]["line_num"])
                if line_diff < 30:
                    cluster.append(rejections[j])
                    j += 1
                else:
                    break

            if len(cluster) >= 2:
                findings.append({
                    "pattern": "user_rejection",
                    "count": len(cluster),
                    "tools": [c["name"] for c in cluster],
                    "calls": cluster,
                    "estimated_tokens": estimate_tokens(cluster),
                })
            i = j if j > i + 1 else i + 1

    return findings


def format_finding(finding: dict, session_slug: str, session_file: str) -> str:
    """Format a single finding into readable text."""
    lines = []
    pattern = finding["pattern"]

    lines.append(f"  Session: {session_slug}")
    lines.append(f"  File: ...{session_file[-60:]}")

    if pattern == "consecutive_retry":
        lines.insert(0, f"[RETRY LOOP] {finding['tool']} called {finding['count']}x consecutively")
        lines.append(f"  Signature: {finding['signature']}")
        lines.append(f"  Errors in run: {finding['had_errors']}/{finding['count']}")
        # Show first and last call details
        first = finding["calls"][0]
        last = finding["calls"][-1]
        if first["name"] == "Bash":
            lines.append(f"  First cmd: {first['input'].get('command', '')[:100]}")
            lines.append(f"  Last cmd:  {last['input'].get('command', '')[:100]}")
        elif first["name"] == "Edit":
            lines.append(f"  File: {first['input'].get('file_path', '')}")

    elif pattern == "edit_fail_loop":
        lines.insert(0, f"[EDIT-FAIL LOOP] {finding['total_attempts']} edit attempts, {finding['errors']} errors")
        lines.append(f"  Target file: {finding['file']}")
        lines.append(f"  Resolved: {'Yes' if finding['resolved'] else 'NO - still failing'}")
        # Show error messages
        for c in finding["calls"]:
            if c["name"] == "Edit" and c["result_error"]:
                err_text = c["result_text"][:150].replace("\n", " ")
                lines.append(f"  Error: {err_text}")
                break

    elif pattern == "bash_retry":
        lines.insert(0, f"[BASH RETRY] Command retried {finding['count']}x ({finding['errors']} errors)")
        lines.append(f"  Base command: {finding['command_base']}")
        lines.append(f"  Resolved: {'Yes' if finding['resolved'] else 'NO'}")

    elif pattern == "search_retry":
        lines.insert(0, f"[SEARCH RETRY] {finding['tool']} called {finding['count']}x for similar query")
        lines.append(f"  Query: {finding['query']}")

    elif pattern == "user_rejection":
        lines.insert(0, f"[USER REJECTION] {finding['count']} consecutive rejections")
        lines.append(f"  Tools rejected: {', '.join(finding['tools'])}")

    lines.append(f"  Estimated wasted tokens: ~{finding['estimated_tokens']:,}")
    return "\n".join(lines)


def resolution_summary(finding: dict) -> str:
    """Describe how a loop was broken."""
    pattern = finding["pattern"]
    calls = finding.get("calls", [])
    if not calls:
        return "Unknown"

    last = calls[-1]
    if pattern == "edit_fail_loop":
        if finding.get("resolved"):
            return "Successful edit after multiple attempts"
        return "Loop NOT resolved (still failing at end of sequence)"

    if pattern == "bash_retry":
        if finding.get("resolved"):
            return f"Command succeeded on attempt #{finding['count']}"
        return "Command never succeeded in this sequence"

    if pattern == "consecutive_retry":
        if not last["result_error"]:
            return "Last call succeeded"
        return "Loop ended (possibly gave up or switched approach)"

    if pattern == "user_rejection":
        return "User rejected agent's output multiple times"

    if pattern == "search_retry":
        return "Multiple similar searches executed"

    return "Unknown"


def main():
    output_lines = []
    output_lines.append("=" * 80)
    output_lines.append("CLAUDE CODE SESSION ANALYSIS: Retry Loops & Wasted Effort")
    output_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    output_lines.append("=" * 80)

    sessions = find_sessions(PROJECTS_DIR, MIN_SIZE, MAX_SESSIONS)
    output_lines.append(f"\nScanned {len(sessions)} sessions (most recent, >100KB, excluding subagents)")
    output_lines.append("")

    all_findings = []
    session_stats = defaultdict(lambda: {"findings": 0, "tokens_wasted": 0})

    for session_path in sessions:
        slug = "unknown"
        session_id = session_path.stem

        messages = parse_session(session_path)
        if not messages:
            continue

        # Extract slug from first message
        for msg in messages[:5]:
            s = msg.get("slug")
            if s:
                slug = s
                break

        tool_calls = extract_tool_calls(messages)
        if len(tool_calls) < 3:
            continue

        findings = []
        findings.extend(detect_consecutive_retries(tool_calls))
        findings.extend(detect_edit_fail_loops(tool_calls))
        findings.extend(detect_bash_retries(tool_calls))
        findings.extend(detect_search_retries(tool_calls))
        findings.extend(detect_user_rejections(tool_calls))

        for f in findings:
            f["session_slug"] = slug
            f["session_file"] = str(session_path)
            f["session_id"] = session_id
            all_findings.append(f)
            session_stats[slug]["findings"] += 1
            session_stats[slug]["tokens_wasted"] += f["estimated_tokens"]

    # Sort findings by estimated tokens wasted (descending)
    all_findings.sort(key=lambda f: f["estimated_tokens"], reverse=True)

    # Summary
    output_lines.append("-" * 80)
    output_lines.append("SUMMARY")
    output_lines.append("-" * 80)

    pattern_counts = defaultdict(int)
    pattern_tokens = defaultdict(int)
    for f in all_findings:
        pattern_counts[f["pattern"]] += 1
        pattern_tokens[f["pattern"]] += f["estimated_tokens"]

    total_tokens = sum(f["estimated_tokens"] for f in all_findings)
    output_lines.append(f"Total findings: {len(all_findings)}")
    output_lines.append(f"Total estimated wasted tokens: ~{total_tokens:,}")
    output_lines.append("")

    pattern_labels = {
        "consecutive_retry": "Consecutive retry loops (same tool 3+ times)",
        "edit_fail_loop": "Edit-fail loops (Read/Edit/Error cycles)",
        "bash_retry": "Bash command retries (fail + retry variants)",
        "search_retry": "Search retries (similar WebSearch/WebFetch)",
        "user_rejection": "User rejections (code immediately rejected)",
    }
    for pat, label in pattern_labels.items():
        count = pattern_counts.get(pat, 0)
        tokens = pattern_tokens.get(pat, 0)
        if count > 0:
            output_lines.append(f"  {label}: {count} instances (~{tokens:,} tokens)")

    # Worst sessions
    output_lines.append("")
    output_lines.append("-" * 80)
    output_lines.append("WORST SESSIONS (by wasted tokens)")
    output_lines.append("-" * 80)

    worst = sorted(session_stats.items(), key=lambda x: x[1]["tokens_wasted"], reverse=True)[:15]
    for slug, stats in worst:
        output_lines.append(f"  {slug}: {stats['findings']} findings, ~{stats['tokens_wasted']:,} tokens wasted")

    # Detailed findings
    output_lines.append("")
    output_lines.append("-" * 80)
    output_lines.append("DETAILED FINDINGS (sorted by wasted tokens, top 100)")
    output_lines.append("-" * 80)

    for i, finding in enumerate(all_findings[:100], 1):
        output_lines.append("")
        output_lines.append(f"--- Finding #{i} ---")
        output_lines.append(format_finding(finding, finding["session_slug"], finding["session_file"]))
        output_lines.append(f"  Resolution: {resolution_summary(finding)}")

    output_lines.append("")
    output_lines.append("=" * 80)
    output_lines.append("END OF REPORT")
    output_lines.append("=" * 80)

    report = "\n".join(output_lines)

    # Write to file
    output_path = _output_path("retry_loops")
    with open(output_path, "w") as f:
        f.write(report)

    # Also print to stdout
    print(report)
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
