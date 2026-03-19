#!/usr/bin/env python3
"""
Analyze Claude Code JSONL transcripts for PreToolUse hook rejections.

Scans all JSONL files under ~/.claude/projects/ and extracts:
  - Session slug/name
  - The exact bash command that was rejected
  - The hook error message
  - What the agent did after the rejection (workaround, gave up, asked user)
"""

import json
import glob
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path
PROJECTS_DIR = str(CLAUDE_PROJECTS_DIR)
OUTPUT_FILE = str(_output_path("hook_rejections"))

# Hook error patterns in toolUseResult or is_error content
HOOK_ERROR_PREFIX = "PreToolUse:"
HOOK_ERROR_MARKER = "hook error"


def classify_followup(lines, error_line_idx, tool_use_id):
    """
    Look at the next assistant message(s) after the rejection to classify behavior.
    Returns a tuple: (classification, detail)
    """
    # Scan forward from the error line for the next assistant message
    for i in range(error_line_idx + 1, min(error_line_idx + 20, len(lines))):
        raw = lines[i]
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        if d.get("type") != "assistant":
            continue

        msg = d.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        texts = []
        tools = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                tools.append((name, inp))

        combined_text = " ".join(texts).lower()

        # Check for asking user
        if any(
            kw in combined_text
            for kw in [
                "would you like",
                "shall i",
                "do you want",
                "let me know",
                "could you",
                "please confirm",
                "i can't",
                "i cannot",
                "not allowed",
                "the hook blocked",
                "was blocked",
                "hook prevented",
                "instead, i'll",
                "instead i'll",
                "alternative approach",
            ]
        ):
            # Determine sub-type
            if tools:
                tool_names = [t[0] for t in tools]
                detail = f"asked user, then tried: {', '.join(tool_names)}"
                return ("workaround_with_explanation", detail)
            return ("asked_user", combined_text[:200] if combined_text else "no text")

        # Check for workaround (tried a different tool)
        if tools:
            tool_summary = []
            for name, inp in tools:
                if name == "Bash":
                    cmd = inp.get("command", "")[:150]
                    tool_summary.append(f"Bash({cmd})")
                elif name == "Write":
                    tool_summary.append(f"Write({inp.get('file_path', '')})")
                elif name == "Edit":
                    tool_summary.append(f"Edit({inp.get('file_path', '')})")
                else:
                    tool_summary.append(name)
            return ("workaround", "; ".join(tool_summary))

        # Just text, no tools -- likely gave up or explained
        if texts:
            return ("gave_up_or_explained", combined_text[:200])

        return ("unknown", "empty assistant message")

    return ("no_followup_found", "")


def extract_command_from_assistant(lines, error_line_idx, tool_use_id):
    """
    Find the assistant message containing the tool_use with the given id.
    Scan backwards from the error line.
    """
    for i in range(error_line_idx - 1, max(error_line_idx - 30, -1), -1):
        raw = lines[i]
        if not raw:
            continue
        if tool_use_id not in raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        if d.get("type") != "assistant":
            # Could be in a progress message wrapping an assistant message
            data = d.get("data", {})
            if isinstance(data, dict):
                inner = data.get("message", {})
                if isinstance(inner, dict):
                    inner_msg = inner.get("message", {})
                    content = inner_msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("id") == tool_use_id:
                                inp = block.get("input", {})
                                return block.get("name", ""), inp.get("command", inp.get("file_path", str(inp)[:300]))
            continue

        msg = d.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if isinstance(block, dict) and block.get("id") == tool_use_id:
                inp = block.get("input", {})
                tool_name = block.get("name", "")
                if tool_name == "Bash":
                    return tool_name, inp.get("command", "")
                elif tool_name in ("Edit", "Write"):
                    return tool_name, inp.get("file_path", "")
                else:
                    return tool_name, str(inp)[:300]

    return ("unknown", "could not find original tool_use")


def scan_file(filepath):
    """Scan a single JSONL file for PreToolUse hook rejections."""
    rejections = []
    lines = []

    try:
        with open(filepath, errors="replace") as f:
            for line in f:
                lines.append(line.strip())
    except Exception:
        return rejections

    session_id = None
    slug = None

    for idx, raw in enumerate(lines):
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        # Track session metadata
        if d.get("sessionId"):
            session_id = d["sessionId"]
        if d.get("slug"):
            slug = d["slug"]

        # Look for user messages with is_error containing hook error
        if d.get("type") != "user":
            continue

        tool_use_result = d.get("toolUseResult", "")
        if isinstance(tool_use_result, str) and HOOK_ERROR_MARKER in tool_use_result.lower():
            pass  # confirmed hook error via toolUseResult
        elif isinstance(tool_use_result, dict):
            continue
        else:
            # Also check content blocks
            msg = d.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            found = False
            for block in content:
                if isinstance(block, dict) and block.get("is_error"):
                    text = str(block.get("content", ""))
                    if HOOK_ERROR_PREFIX.lower() in text.lower() and HOOK_ERROR_MARKER in text.lower():
                        found = True
                        break
            if not found:
                continue

        # Extract hook error details
        msg = d.get("message", {})
        content = msg.get("content", [])
        error_text = ""
        tool_use_id = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("is_error"):
                    error_text = str(block.get("content", ""))
                    tool_use_id = block.get("tool_use_id", "")
                    break

        if not error_text:
            error_text = str(tool_use_result)

        # Extract the rejected command
        tool_name, rejected_command = extract_command_from_assistant(lines, idx, tool_use_id)

        # Classify the agent's follow-up behavior
        followup_type, followup_detail = classify_followup(lines, idx, tool_use_id)

        # Use slug from this message or fallback to file-level
        entry_slug = d.get("slug", slug or "N/A")
        entry_session = d.get("sessionId", session_id or "N/A")
        timestamp = d.get("timestamp", "N/A")

        rejections.append({
            "file": filepath,
            "session_id": entry_session,
            "slug": entry_slug,
            "timestamp": timestamp,
            "tool_name": tool_name,
            "rejected_command": rejected_command,
            "hook_error": error_text,
            "followup_type": followup_type,
            "followup_detail": followup_detail,
        })

    return rejections


def main():
    jsonl_pattern = os.path.join(PROJECTS_DIR, "**", "*.jsonl")
    files = glob.glob(jsonl_pattern, recursive=True)
    print(f"Scanning {len(files)} JSONL files...")

    all_rejections = []
    files_with_hits = 0

    for i, filepath in enumerate(sorted(files)):
        if (i + 1) % 500 == 0:
            print(f"  ...processed {i + 1}/{len(files)} files, found {len(all_rejections)} rejections so far")
        rejections = scan_file(filepath)
        if rejections:
            files_with_hits += 1
            all_rejections.extend(rejections)

    print(f"\nDone. Found {len(all_rejections)} hook rejections across {files_with_hits} session files.\n")

    # Group by hook script
    by_hook = defaultdict(list)
    for r in all_rejections:
        # Extract hook script name from error text
        err = r["hook_error"]
        hook_name = "unknown"
        if "[" in err and "]" in err:
            start = err.index("[") + 1
            end = err.index("]", start)
            hook_name = err[start:end]
        by_hook[hook_name].append(r)

    # Group by followup type
    by_followup = defaultdict(int)
    for r in all_rejections:
        by_followup[r["followup_type"]] += 1

    # Write output
    lines = []
    lines.append("=" * 80)
    lines.append("CLAUDE CODE PRETOOLUSE HOOK REJECTION ANALYSIS")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"Total rejections found: {len(all_rejections)}")
    lines.append(f"Session files with rejections: {files_with_hits}")
    lines.append(f"Total JSONL files scanned: {len(files)}")
    lines.append("")

    # Summary by hook
    lines.append("-" * 60)
    lines.append("REJECTIONS BY HOOK SCRIPT")
    lines.append("-" * 60)
    for hook, entries in sorted(by_hook.items(), key=lambda x: -len(x[1])):
        hook_short = os.path.basename(hook) if "/" in hook else hook
        lines.append(f"  {hook_short}: {len(entries)}")
    lines.append("")

    # Summary by followup behavior
    lines.append("-" * 60)
    lines.append("AGENT BEHAVIOR AFTER REJECTION")
    lines.append("-" * 60)
    for ftype, count in sorted(by_followup.items(), key=lambda x: -x[1]):
        lines.append(f"  {ftype}: {count}")
    lines.append("")

    # Detailed entries grouped by hook
    for hook, entries in sorted(by_hook.items(), key=lambda x: -len(x[1])):
        hook_short = os.path.basename(hook) if "/" in hook else hook
        lines.append("")
        lines.append("=" * 80)
        lines.append(f"HOOK: {hook_short} ({len(entries)} rejections)")
        lines.append("=" * 80)

        for i, r in enumerate(entries, 1):
            lines.append("")
            lines.append(f"--- Rejection #{i} ---")
            lines.append(f"  Session: {r['slug']} ({r['session_id'][:12]}...)")
            lines.append(f"  Timestamp: {r['timestamp']}")
            lines.append(f"  Tool: {r['tool_name']}")

            # Truncate long commands for readability
            cmd = r["rejected_command"]
            if len(cmd) > 500:
                cmd = cmd[:500] + "... [truncated]"
            lines.append(f"  Rejected command:")
            for cmd_line in cmd.split("\n")[:15]:
                lines.append(f"    {cmd_line}")
            if cmd.count("\n") > 15:
                lines.append(f"    ... [{cmd.count(chr(10)) - 15} more lines]")

            lines.append(f"  Hook error: {r['hook_error'][:200]}")
            lines.append(f"  Agent reaction: {r['followup_type']}")
            if r["followup_detail"]:
                detail = r["followup_detail"]
                if len(detail) > 300:
                    detail = detail[:300] + "..."
                lines.append(f"  Reaction detail: {detail}")

    output = "\n".join(lines)
    print(output[:3000])
    if len(output) > 3000:
        print(f"\n... [output truncated in console, full output in {OUTPUT_FILE}]")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(output)

    print(f"\nFull report saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
