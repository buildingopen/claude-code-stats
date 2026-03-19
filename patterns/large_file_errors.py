#!/usr/bin/env python3
"""
Analyze Claude Code JSONL transcripts for "file content exceeds maximum" errors.

Searches through ~/.claude/projects/ JSONL files to find sessions where the agent
tried to Read files that were too large (HEIC, JPG, large code files, etc.).

For each session: extracts slug/name, error count, files attempted, and recovery behavior.
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path
PROJECTS_DIR = CLAUDE_PROJECTS_DIR

# Patterns that indicate the "exceeds maximum" error
ERROR_PATTERNS = [
    re.compile(r"File content \([^)]+\) exceeds maximum allowed (?:tokens|size)"),
]


def extract_error_info(content_str):
    """Extract size/token info from the error message."""
    m = re.search(r"File content \(([^)]+)\) exceeds maximum allowed (?:tokens|size) \(([^)]+)\)", content_str)
    if m:
        return {"file_size": m.group(1), "max_allowed": m.group(2)}
    return {}


def find_tool_use_file_path(lines_data, tool_use_id):
    """Given a tool_use_id, search through parsed lines to find the corresponding Read call and its file_path."""
    if not tool_use_id:
        return None, None

    for obj in lines_data:
        # Direct assistant message
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id") == tool_use_id:
                    inp = c.get("input", {})
                    return inp.get("file_path"), c.get("name")

        # Progress/subagent message
        data = obj.get("data", {})
        if data:
            inner_msg = data.get("message", {})
            inner_content = inner_msg.get("message", {}).get("content", []) if isinstance(inner_msg.get("message"), dict) else []
            if isinstance(inner_content, list):
                for c in inner_content:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id") == tool_use_id:
                        inp = c.get("input", {})
                        return inp.get("file_path"), c.get("name")

    return None, None


def classify_recovery(lines_data, error_index, tool_use_id):
    """Look at what the agent did after the error. Returns a recovery description."""
    # Look at the next few messages after the error
    actions = []
    for j in range(error_index + 1, min(error_index + 6, len(lines_data))):
        obj = lines_data[j]

        # Extract tool calls or text from assistant messages
        msg = obj.get("message", {})
        content = msg.get("content", [])
        obj_type = obj.get("type", "")

        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "tool_use":
                        name = c.get("name", "?")
                        inp = c.get("input", {})
                        if name == "Read":
                            fp = inp.get("file_path", "")
                            if "offset" in inp or "limit" in inp:
                                actions.append(f"retried Read with offset/limit on {os.path.basename(fp)}")
                            else:
                                actions.append(f"tried Read on different file: {os.path.basename(fp)}")
                        elif name in ("Grep", "GrepTool"):
                            actions.append(f"switched to Grep/search")
                        elif name == "Bash":
                            cmd = inp.get("command", "")[:80]
                            actions.append(f"used Bash: {cmd}")
                        elif name in ("Glob", "GlobTool"):
                            actions.append(f"used Glob to find files")
                        else:
                            actions.append(f"called {name}")
                    elif c.get("type") == "text":
                        text = c.get("text", "")[:200]
                        if text.strip():
                            actions.append(f"said: {text[:100]}")
                    elif c.get("type") == "thinking":
                        thinking = c.get("thinking", "")[:200]
                        # Check for recovery-related thinking
                        if any(kw in thinking.lower() for kw in ["retry", "offset", "limit", "too large", "cannot read", "different approach", "binary", "image"]):
                            actions.append(f"thought about: {thinking[:100]}")

        # Also check progress messages (subagent)
        data = obj.get("data", {})
        if data:
            inner_msg = data.get("message", {})
            inner_content = inner_msg.get("message", {}).get("content", []) if isinstance(inner_msg.get("message"), dict) else []
            if isinstance(inner_content, list):
                for c in inner_content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        name = c.get("name", "?")
                        inp = c.get("input", {})
                        if name == "Read":
                            fp = inp.get("file_path", "")
                            if "offset" in inp or "limit" in inp:
                                actions.append(f"retried Read with offset/limit on {os.path.basename(fp)}")
                            else:
                                actions.append(f"tried Read on: {os.path.basename(fp)}")
                        elif name in ("Grep", "GrepTool"):
                            actions.append(f"switched to Grep/search")
                        elif name == "Bash":
                            cmd = inp.get("command", "")[:80]
                            actions.append(f"used Bash: {cmd}")
                        else:
                            actions.append(f"called {name}")

        if len(actions) >= 3:
            break

    if not actions:
        return "no visible recovery (end of session or context)"
    return "; ".join(actions[:3])


def analyze_file(filepath):
    """Analyze a single JSONL file for exceeds-maximum errors."""
    lines_data = []
    try:
        with open(filepath, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    lines_data.append(obj)
                except (json.JSONDecodeError, ValueError):
                    continue
    except (OSError, IOError):
        return None

    # Find the session slug/name
    session_slug = None
    session_id = None
    for obj in lines_data:
        if obj.get("slug"):
            session_slug = obj["slug"]
        if obj.get("sessionId"):
            session_id = obj["sessionId"]
        if session_slug:
            break

    if not session_slug:
        # Use session_id or filename
        session_slug = session_id or Path(filepath).stem

    # Find all error occurrences
    errors = []
    for i, obj in enumerate(lines_data):
        s = json.dumps(obj)
        if "exceeds maximum" not in s:
            continue

        # Extract tool_use_id from error
        tool_use_ids = []

        # Direct user message with tool_result
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("is_error") and "exceeds maximum" in str(c.get("content", "")):
                    tool_use_ids.append(c.get("tool_use_id"))
                    error_content = c.get("content", "")

        # Progress/subagent message
        data = obj.get("data", {})
        if data:
            inner_msg = data.get("message", {})
            # toolUseResult field
            tur = inner_msg.get("toolUseResult", "")
            if "exceeds maximum" in str(tur):
                inner_content = inner_msg.get("message", {}).get("content", []) if isinstance(inner_msg.get("message"), dict) else []
                if isinstance(inner_content, list):
                    for c in inner_content:
                        if isinstance(c, dict) and c.get("is_error") and "exceeds maximum" in str(c.get("content", "")):
                            tool_use_ids.append(c.get("tool_use_id"))
                            error_content = c.get("content", "")

        for tuid in tool_use_ids:
            file_path, tool_name = find_tool_use_file_path(lines_data, tuid)
            error_info = extract_error_info(error_content) if 'error_content' in dir() else {}
            recovery = classify_recovery(lines_data, i, tuid)

            errors.append({
                "file_path": file_path or "(unknown)",
                "tool_name": tool_name or "Read",
                "error_info": error_info,
                "recovery": recovery,
            })

    if not errors:
        return None

    return {
        "session_slug": session_slug,
        "jsonl_file": str(filepath),
        "error_count": len(errors),
        "errors": errors,
    }


def get_file_extension(fp):
    """Get file extension, lowercased."""
    if fp and fp != "(unknown)":
        return Path(fp).suffix.lower()
    return "(unknown)"


def main():
    print("Scanning JSONL files in", PROJECTS_DIR)
    print()

    # Find all JSONL files
    jsonl_files = []
    for root, dirs, files in os.walk(PROJECTS_DIR):
        for f in files:
            if f.endswith(".jsonl"):
                jsonl_files.append(os.path.join(root, f))

    print(f"Found {len(jsonl_files)} JSONL files to scan")
    print()

    # Pre-filter: only scan files that contain the error string
    candidate_files = []
    for fp in jsonl_files:
        try:
            with open(fp, "r", errors="replace") as f:
                content = f.read()
                if "exceeds maximum" in content:
                    candidate_files.append(fp)
        except (OSError, IOError):
            continue

    print(f"Found {len(candidate_files)} files containing 'exceeds maximum' errors")
    print()

    # Analyze each candidate
    results = []
    for i, fp in enumerate(candidate_files):
        if (i + 1) % 50 == 0:
            print(f"  Analyzing {i+1}/{len(candidate_files)}...", file=sys.stderr)
        result = analyze_file(fp)
        if result:
            results.append(result)

    # Deduplicate by session (multiple JSONL files can belong to same session)
    session_map = defaultdict(lambda: {"error_count": 0, "errors": [], "jsonl_files": [], "session_slug": ""})
    for r in results:
        key = r["session_slug"]
        session_map[key]["session_slug"] = r["session_slug"]
        session_map[key]["error_count"] += r["error_count"]
        session_map[key]["errors"].extend(r["errors"])
        session_map[key]["jsonl_files"].append(r["jsonl_file"])

    # Sort by error count descending
    sorted_sessions = sorted(session_map.values(), key=lambda x: x["error_count"], reverse=True)

    # Build report
    lines = []
    lines.append("=" * 80)
    lines.append("LARGE FILE ERROR ANALYSIS")
    total_errors = sum(s["error_count"] for s in sorted_sessions)
    lines.append(f"Sessions with 'file content exceeds maximum' errors: {len(sorted_sessions)}")
    lines.append(f"Total error occurrences: {total_errors}")
    lines.append("=" * 80)
    lines.append("")

    # Summary statistics
    all_files = []
    all_extensions = defaultdict(int)
    recovery_types = defaultdict(int)

    for sess in sorted_sessions:
        for err in sess["errors"]:
            fp = err["file_path"]
            all_files.append(fp)
            ext = get_file_extension(fp)
            all_extensions[ext] += 1

            rec = err["recovery"]
            if "retried Read with offset/limit" in rec:
                recovery_types["retried with offset/limit"] += 1
            elif "switched to Grep" in rec:
                recovery_types["switched to Grep/search"] += 1
            elif "used Bash" in rec:
                recovery_types["used Bash command"] += 1
            elif "tried Read on" in rec:
                recovery_types["tried reading another file"] += 1
            elif "no visible recovery" in rec:
                recovery_types["no visible recovery"] += 1
            elif "said:" in rec:
                recovery_types["responded with text"] += 1
            else:
                recovery_types["other"] += 1

    lines.append("-" * 80)
    lines.append("FILE EXTENSIONS BREAKDOWN")
    lines.append("-" * 80)
    for ext, count in sorted(all_extensions.items(), key=lambda x: -x[1]):
        lines.append(f"  {ext or '(no ext)':<15} {count:>4} occurrences")
    lines.append("")

    lines.append("-" * 80)
    lines.append("RECOVERY BEHAVIOR SUMMARY")
    lines.append("-" * 80)
    for rec_type, count in sorted(recovery_types.items(), key=lambda x: -x[1]):
        lines.append(f"  {rec_type:<35} {count:>4} times")
    lines.append("")

    lines.append("=" * 80)
    lines.append("PER-SESSION DETAILS")
    lines.append("=" * 80)
    lines.append("")

    for sess in sorted_sessions:
        lines.append(f"Session: {sess['session_slug']}")
        lines.append(f"  Error count: {sess['error_count']}")
        lines.append(f"  JSONL files: {len(sess['jsonl_files'])}")

        file_counts = defaultdict(int)
        for err in sess["errors"]:
            file_counts[err["file_path"]] += 1

        lines.append(f"  Files attempted:")
        for fp, cnt in sorted(file_counts.items(), key=lambda x: -x[1]):
            basename = os.path.basename(fp) if fp != "(unknown)" else "(unknown)"
            ext = get_file_extension(fp)
            lines.append(f"    - {basename} ({ext}) x{cnt}")
            if fp != "(unknown)":
                lines.append(f"      Full path: {fp}")

        lines.append(f"  Recovery actions:")
        seen_recoveries = set()
        for err in sess["errors"]:
            rec = err["recovery"]
            if rec not in seen_recoveries:
                seen_recoveries.add(rec)
                lines.append(f"    - {rec}")

        for err in sess["errors"]:
            if err.get("error_info"):
                info = err["error_info"]
                lines.append(f"  Size info: file was {info.get('file_size', '?')}, max allowed {info.get('max_allowed', '?')}")
                break

        lines.append("")

    lines.append("=" * 80)
    lines.append("NOTABLE PATTERNS")
    lines.append("=" * 80)
    lines.append("")

    if sorted_sessions:
        top = sorted_sessions[0]
        lines.append(f"- Most errors in single session: {top['session_slug']} ({top['error_count']} errors)")

    binary_exts = {".heic", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp",
                   ".mp4", ".mov", ".avi", ".pdf", ".zip", ".tar", ".gz", ".dmg",
                   ".exe", ".bin", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".otf",
                   ".ico", ".svg"}
    binary_count = sum(1 for fp in all_files if get_file_extension(fp) in binary_exts)
    code_count = len(all_files) - binary_count - sum(1 for fp in all_files if get_file_extension(fp) == "(unknown)")
    lines.append(f"- Binary/media files attempted: {binary_count} ({binary_count*100//max(len(all_files),1)}%)")
    lines.append(f"- Code/text files too large: {code_count}")
    lines.append(f"- Unknown file paths: {sum(1 for fp in all_files if get_file_extension(fp) == '(unknown)')}")

    file_freq = defaultdict(int)
    for fp in all_files:
        if fp != "(unknown)":
            file_freq[os.path.basename(fp)] += 1
    if file_freq:
        lines.append("")
        lines.append("Most frequently problematic files (by basename):")
        for fname, cnt in sorted(file_freq.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"  {fname}: {cnt} times")

    report = "\n".join(lines)
    print(report[:3000])
    if len(report) > 3000:
        print(f"\n... [truncated in console]")

    output_path = _output_path("large_file_errors")
    with open(output_path, "w") as f:
        f.write(report)
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
