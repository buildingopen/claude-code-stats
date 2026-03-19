#!/usr/bin/env python3
"""
Error Taxonomy Analyzer for Claude Code conversation JSONL files.

Processes all sessions in ~/.claude/projects/ to build a complete taxonomy of errors,
classify them, analyze sequences, and identify preventable patterns.
"""

import json
import os
import re
import glob
from collections import defaultdict, Counter
from pathlib import Path
from datetime import datetime

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path


# ---------------------------------------------------------------------------
# Error classification rules
# ---------------------------------------------------------------------------

ERROR_CATEGORIES = {
    "FILE_TOO_LARGE": [
        r"exceeds maximum",
        r"file content exceeds",
        r"exceeds maximum allowed size",
        r"exceeds maximum allowed tokens",
    ],
    "FILE_NOT_READ": [
        r"file has not been read",
        r"Read it first before writing",
        r"File has been modified since read",
    ],
    "FILE_NOT_FOUND": [
        r"File does not exist",
        r"Path does not exist",
        r"No such file or directory",
        r"EISDIR",
    ],
    "HOOK_BLOCKED": [
        r"hook error",
        r"PreToolUse",
        r"PostToolUse.*hook",
    ],
    "USER_REJECTED": [
        r"user doesn't want to proceed",
        r"rejected",
        r"Request interrupted by user",
        r"tool use was rejected",
    ],
    "COMMAND_FAILED": [
        r"^Exit code [0-9]+",
    ],
    "EDIT_FAILED": [
        r"old_string.*not found",
        r"String to replace not found",
        r"not unique",
        r"Found \d+ matches.*replace_all is false",
        r"No changes to make.*same",
        r"validation failed after edit",
    ],
    "PERMISSION_DENIED": [
        r"permission denied",
        r"EACCES",
    ],
    "NETWORK_ERROR": [
        r"(?<!\-)timeout(?![\s=]\d)",  # "timeout" but not "--timeout=60"
        r"ECONNREFUSED",
        r"ECONNRESET",
        r"fetch failed",
        r"unable to fetch",
        r"net::ERR_",
        r"Repository not found",
        r"timed out after",
        r"connect ETIMEDOUT",
    ],
    "TOOL_NOT_FOUND": [
        r"No such tool available",
        r"Unknown skill",
        r"No task found with ID",
    ],
    "SIBLING_ERROR": [
        r"Sibling tool call errored",
        r"parallel tool call.*errored",
    ],
    "MCP_ERROR": [
        r"Ref e\d+ not found",
        r"browserType\.connect",
        r"page\._wrapApiCall",
        r"locator\.",
        r"page\.goto.*net::ERR",
        r"Execution context was destroyed",
        r"Target page.*closed",
    ],
    "TOOL_ERROR": [
        r"<tool_use_error>",
        r"Bad substitution",
        r"Failed to parse command",
        r"Bash command failed for pattern",
    ],
}

# Order matters: more specific categories first
CATEGORY_ORDER = [
    "FILE_TOO_LARGE",
    "FILE_NOT_READ",
    "FILE_NOT_FOUND",
    "HOOK_BLOCKED",
    "USER_REJECTED",
    "EDIT_FAILED",
    "PERMISSION_DENIED",
    "NETWORK_ERROR",
    "TOOL_NOT_FOUND",
    "SIBLING_ERROR",
    "MCP_ERROR",
    "COMMAND_FAILED",
    "TOOL_ERROR",
    "UNKNOWN",
]


def classify_error(text: str) -> str:
    """Classify an error message into a category."""
    text_lower = text.lower()
    for category in CATEGORY_ORDER:
        if category == "UNKNOWN":
            continue
        patterns = ERROR_CATEGORIES.get(category, [])
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return category
    return "UNKNOWN"


def extract_tool_name_from_assistant(uuid_to_msg: dict, source_uuid: str, tool_use_id: str) -> str | None:
    """Given an assistant message UUID and tool_use_id, find the tool name."""
    assistant_msg = uuid_to_msg.get(source_uuid)
    if not assistant_msg:
        return None
    content = assistant_msg.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if block.get("id") == tool_use_id:
                return block.get("name")
    return None


def get_next_assistant_tools(uuid_to_msg: dict, parent_chain: dict, error_uuid: str) -> list[str]:
    """Find what tools the assistant used in its next response after an error."""
    # Find messages whose parent is the error message
    children = parent_chain.get(error_uuid, [])
    tools = []
    for child_uuid in children:
        child_msg = uuid_to_msg.get(child_uuid)
        if not child_msg:
            continue
        if child_msg.get("type") == "assistant":
            content = child_msg.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools.append(block.get("name", "unknown"))
    return tools


def analyze_post_error_action(
    error_tool_name: str | None,
    next_tools: list[str],
    next_has_text: bool,
) -> str:
    """Classify what happened after an error."""
    if not next_tools and not next_has_text:
        return "gave_up"
    if not next_tools and next_has_text:
        return "asked_user"

    if error_tool_name and error_tool_name in next_tools:
        return "retried_same_tool"
    if next_tools:
        return "switched_approach"
    return "unknown"


def assess_preventability(category: str, error_text: str, tool_name: str | None) -> list[str]:
    """Determine how an error could have been prevented."""
    preventions = []

    if category == "FILE_NOT_READ":
        preventions.append("Reading the file first")

    if category == "FILE_NOT_FOUND":
        preventions.append("Checking before acting")
        preventions.append("Better tool choice")

    if category == "FILE_TOO_LARGE":
        preventions.append("Better tool choice")

    if category == "EDIT_FAILED":
        preventions.append("Reading the file first")
        preventions.append("Checking before acting")

    if category == "HOOK_BLOCKED":
        preventions.append("Better CLAUDE.md instructions")

    if category == "COMMAND_FAILED":
        if "not found" in error_text.lower() or "unrecognized arguments" in error_text.lower():
            preventions.append("Checking before acting")
        if "test" in error_text.lower():
            preventions.append("Better tool choice")

    if category == "PERMISSION_DENIED":
        preventions.append("Better CLAUDE.md instructions")
        preventions.append("Checking before acting")

    if category == "NETWORK_ERROR":
        if "Repository not found" in error_text:
            preventions.append("Checking before acting")

    if category == "TOOL_NOT_FOUND":
        preventions.append("Better CLAUDE.md instructions")

    if category == "SIBLING_ERROR":
        preventions.append("Better tool choice")

    if category == "USER_REJECTED":
        preventions.append("Better CLAUDE.md instructions")

    return preventions if preventions else ["Not easily preventable"]


def process_session(filepath: str) -> dict:
    """Process a single JSONL session file and return structured data."""
    messages = []
    errors = []
    uuid_to_msg = {}
    parent_chain = defaultdict(list)  # parent_uuid -> [child_uuids]

    try:
        with open(filepath, "r", errors="replace") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                uuid = obj.get("uuid")
                if uuid:
                    uuid_to_msg[uuid] = obj
                    messages.append(obj)

                parent = obj.get("parentUuid")
                if parent and uuid:
                    parent_chain[parent].append(uuid)

                # Extract errors from tool_result blocks
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("is_error"):
                            error_text = block.get("content", "")
                            if isinstance(error_text, list):
                                error_text = " ".join(str(t) for t in error_text)
                            if not error_text:
                                continue

                            tool_use_id = block.get("tool_use_id", "")
                            source_uuid = obj.get("sourceToolAssistantUUID", "")
                            tool_name = extract_tool_name_from_assistant(
                                uuid_to_msg, source_uuid, tool_use_id
                            )

                            errors.append({
                                "text": error_text,
                                "category": classify_error(error_text),
                                "tool_name": tool_name,
                                "tool_use_id": tool_use_id,
                                "uuid": uuid,
                                "source_uuid": source_uuid,
                                "timestamp": obj.get("timestamp", ""),
                                "session_id": obj.get("sessionId", ""),
                            })
    except Exception as e:
        pass

    return {
        "filepath": filepath,
        "messages": messages,
        "errors": errors,
        "uuid_to_msg": uuid_to_msg,
        "parent_chain": parent_chain,
    }


def analyze_error_sequences(session_data: dict) -> list[dict]:
    """Analyze what happens after each error in a session."""
    results = []
    uuid_to_msg = session_data["uuid_to_msg"]
    parent_chain = session_data["parent_chain"]

    for error in session_data["errors"]:
        error_uuid = error["uuid"]
        if not error_uuid:
            results.append({**error, "post_action": "unknown"})
            continue

        # Find the next assistant response
        children = parent_chain.get(error_uuid, [])
        next_tools = []
        next_has_text = False

        for child_uuid in children:
            child_msg = uuid_to_msg.get(child_uuid)
            if not child_msg:
                continue
            if child_msg.get("type") == "assistant":
                content = child_msg.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_use":
                                next_tools.append(block.get("name", "unknown"))
                            elif block.get("type") == "text" and block.get("text", "").strip():
                                next_has_text = True

        post_action = analyze_post_error_action(error["tool_name"], next_tools, next_has_text)
        results.append({
            **error,
            "post_action": post_action,
            "next_tools": next_tools,
        })

    return results


def truncate_msg(text: str, max_len: int = 120) -> str:
    """Truncate and clean an error message for display."""
    text = text.replace("\n", " ").replace("\r", "")
    text = re.sub(r"\s+", " ", text).strip()
    # Strip ANSI codes
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    # Strip XML tags
    text = re.sub(r"<[^>]+>", "", text)
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def main():
    projects_dir = str(CLAUDE_PROJECTS_DIR)
    jsonl_files = glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)

    print(f"Found {len(jsonl_files)} JSONL files to process...")

    # Process all sessions
    all_errors = []
    all_sequences = []
    session_error_counts = defaultdict(lambda: defaultdict(int))
    files_processed = 0
    files_with_errors = 0

    for i, filepath in enumerate(jsonl_files):
        if (i + 1) % 500 == 0:
            print(f"  Processing file {i+1}/{len(jsonl_files)}...")

        session_data = process_session(filepath)
        files_processed += 1

        if session_data["errors"]:
            files_with_errors += 1

        for error in session_data["errors"]:
            all_errors.append(error)
            session_id = error.get("session_id", os.path.basename(filepath))
            session_error_counts[error["category"]][session_id] += 1

        sequences = analyze_error_sequences(session_data)
        all_sequences.extend(sequences)

    print(f"Processed {files_processed} files, {files_with_errors} had errors.")
    print(f"Total errors found: {len(all_errors)}")

    # ---------------------------------------------------------------------------
    # Aggregate stats
    # ---------------------------------------------------------------------------
    category_counts = Counter(e["category"] for e in all_errors)
    total_errors = len(all_errors)

    # Unique example messages per category
    category_examples = defaultdict(list)
    category_examples_seen = defaultdict(set)
    for error in all_errors:
        cat = error["category"]
        msg_key = truncate_msg(error["text"], 100)
        if msg_key not in category_examples_seen[cat] and len(category_examples[cat]) < 3:
            category_examples[cat].append(truncate_msg(error["text"], 200))
            category_examples_seen[cat].add(msg_key)

    # Top sessions per category
    top_sessions_per_cat = {}
    for cat, sessions in session_error_counts.items():
        sorted_sessions = sorted(sessions.items(), key=lambda x: -x[1])[:3]
        top_sessions_per_cat[cat] = sorted_sessions

    # Tool involved in errors
    tool_error_counts = Counter()
    for e in all_errors:
        if e.get("tool_name"):
            tool_error_counts[e["tool_name"]] += 1

    # Post-error action stats
    post_action_counts = Counter(s.get("post_action", "unknown") for s in all_sequences)
    post_action_by_category = defaultdict(Counter)
    for s in all_sequences:
        post_action_by_category[s["category"]][s.get("post_action", "unknown")] += 1

    # Preventability analysis
    prevention_counts = Counter()
    prevention_by_category = defaultdict(Counter)
    for e in all_errors:
        preventions = assess_preventability(e["category"], e["text"], e.get("tool_name"))
        for p in preventions:
            prevention_counts[p] += 1
            prevention_by_category[e["category"]][p] += 1

    # ---------------------------------------------------------------------------
    # Generate markdown report
    # ---------------------------------------------------------------------------
    output_path = str(_output_path("error_taxonomy"))
    lines = []

    def w(text=""):
        lines.append(text)

    w("# Error Taxonomy Report")
    w()
    w(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    w(f"**Files processed:** {files_processed}")
    w(f"**Files with errors:** {files_with_errors}")
    w(f"**Total errors:** {total_errors}")
    w()

    # ------------------------------------------------------------------
    # 1. Category Overview
    # ------------------------------------------------------------------
    w("## 1. Error Categories Overview")
    w()
    w("| Category | Count | % of Total | Description |")
    w("|----------|------:|----------:|-------------|")

    descriptions = {
        "COMMAND_FAILED": "Bash commands returning non-zero exit codes",
        "FILE_NOT_FOUND": "Attempting to read/edit files that do not exist",
        "USER_REJECTED": "User declined a proposed tool use",
        "EDIT_FAILED": "Edit operations with wrong old_string or ambiguous matches",
        "FILE_TOO_LARGE": "File exceeds size or token limits for Read tool",
        "FILE_NOT_READ": "Writing to a file without reading it first",
        "HOOK_BLOCKED": "PreToolUse/PostToolUse hooks blocking operations",
        "PERMISSION_DENIED": "OS-level or SSH permission failures",
        "NETWORK_ERROR": "Timeouts, connection refused, DNS failures",
        "TOOL_NOT_FOUND": "Calling tools/skills that do not exist",
        "SIBLING_ERROR": "Parallel tool call cancelled due to sibling failure",
        "MCP_ERROR": "MCP server errors (Playwright refs, connection issues)",
        "TOOL_ERROR": "Generic tool execution errors",
        "UNKNOWN": "Uncategorized errors",
    }

    for cat in CATEGORY_ORDER:
        count = category_counts.get(cat, 0)
        if count == 0:
            continue
        pct = (count / total_errors * 100) if total_errors > 0 else 0
        desc = descriptions.get(cat, "")
        w(f"| `{cat}` | {count} | {pct:.1f}% | {desc} |")

    w()

    # ------------------------------------------------------------------
    # 2. Per-Category Details
    # ------------------------------------------------------------------
    w("## 2. Per-Category Details")
    w()

    for cat in CATEGORY_ORDER:
        count = category_counts.get(cat, 0)
        if count == 0:
            continue
        pct = (count / total_errors * 100) if total_errors > 0 else 0

        w(f"### {cat}")
        w()
        w(f"**Count:** {count} ({pct:.1f}%)")
        w()

        # Examples
        examples = category_examples.get(cat, [])
        if examples:
            w("**Example messages:**")
            w()
            for i, ex in enumerate(examples, 1):
                w(f"{i}. `{ex}`")
            w()

        # Top sessions
        top = top_sessions_per_cat.get(cat, [])
        if top:
            w("**Sessions with most occurrences:**")
            w()
            w("| Session | Count |")
            w("|---------|------:|")
            for session_id, cnt in top:
                short_id = session_id[:24] + "..." if len(session_id) > 24 else session_id
                w(f"| `{short_id}` | {cnt} |")
            w()

        # Post-error actions for this category
        actions = post_action_by_category.get(cat, {})
        if actions:
            w("**Post-error behavior:**")
            w()
            w("| Action | Count | % |")
            w("|--------|------:|--:|")
            cat_total = sum(actions.values())
            for action, cnt in actions.most_common():
                act_pct = (cnt / cat_total * 100) if cat_total > 0 else 0
                w(f"| {action} | {cnt} | {act_pct:.1f}% |")
            w()

    # ------------------------------------------------------------------
    # 3. Error Sequences
    # ------------------------------------------------------------------
    w("## 3. Error Sequences: What Happens After an Error?")
    w()
    w("Analysis of agent behavior immediately following an error.")
    w()
    w("| Action | Count | % of Total | Description |")
    w("|--------|------:|----------:|-------------|")

    action_desc = {
        "retried_same_tool": "Agent retried the same tool (possibly with different parameters)",
        "switched_approach": "Agent used a different tool or strategy",
        "asked_user": "Agent responded with text only (likely asking for guidance)",
        "gave_up": "No further assistant action found in the chain",
        "unknown": "Could not determine the follow-up action",
    }

    total_seq = sum(post_action_counts.values())
    for action, cnt in post_action_counts.most_common():
        pct = (cnt / total_seq * 100) if total_seq > 0 else 0
        desc = action_desc.get(action, "")
        w(f"| `{action}` | {cnt} | {pct:.1f}% | {desc} |")
    w()

    # Retry analysis by tool
    w("### Retry Rates by Error Category")
    w()
    w("Which error types lead to retrying the same tool vs switching approach?")
    w()
    w("| Category | Retry Same | Switch | Ask User | Gave Up |")
    w("|----------|----------:|-------:|---------:|--------:|")
    for cat in CATEGORY_ORDER:
        actions = post_action_by_category.get(cat, {})
        if not actions:
            continue
        retry = actions.get("retried_same_tool", 0)
        switch = actions.get("switched_approach", 0)
        ask = actions.get("asked_user", 0)
        gave_up = actions.get("gave_up", 0)
        w(f"| `{cat}` | {retry} | {switch} | {ask} | {gave_up} |")
    w()

    # ------------------------------------------------------------------
    # 4. Tool Error Distribution
    # ------------------------------------------------------------------
    w("## 4. Errors by Tool")
    w()
    w("Which tools produce the most errors?")
    w()
    w("| Tool | Error Count | % of Identified |")
    w("|------|----------:|-----------:|")
    total_identified = sum(tool_error_counts.values())
    for tool, cnt in tool_error_counts.most_common(15):
        pct = (cnt / total_identified * 100) if total_identified > 0 else 0
        w(f"| `{tool}` | {cnt} | {pct:.1f}% |")
    w()

    # ------------------------------------------------------------------
    # 5. Preventable Errors
    # ------------------------------------------------------------------
    w("## 5. Preventable Errors")
    w()
    w("How many errors could have been avoided with different practices?")
    w()

    w("### Prevention Strategies")
    w()
    w("| Strategy | Errors Prevented | % of Total |")
    w("|----------|----------------:|----------:|")
    for strategy, cnt in prevention_counts.most_common():
        pct = (cnt / total_errors * 100) if total_errors > 0 else 0
        w(f"| {strategy} | {cnt} | {pct:.1f}% |")
    w()

    # Count how many errors are preventable (not "Not easily preventable")
    preventable = sum(1 for e in all_errors if assess_preventability(e["category"], e["text"], e.get("tool_name")) != ["Not easily preventable"])
    not_preventable = total_errors - preventable
    w(f"**Total preventable errors:** {preventable} ({(preventable/total_errors*100) if total_errors else 0:.1f}%)")
    w(f"**Not easily preventable:** {not_preventable} ({(not_preventable/total_errors*100) if total_errors else 0:.1f}%)")
    w()

    w("### Prevention by Category")
    w()
    w("| Category | Errors | Prevention Strategies |")
    w("|----------|------:|----------------------|")
    for cat in CATEGORY_ORDER:
        count = category_counts.get(cat, 0)
        if count == 0:
            continue
        strategies = prevention_by_category.get(cat, {})
        if strategies:
            strat_list = ", ".join(f"{s} ({c})" for s, c in strategies.most_common())
        else:
            strat_list = "Not easily preventable"
        w(f"| `{cat}` | {count} | {strat_list} |")
    w()

    # ------------------------------------------------------------------
    # 6. Recommendations
    # ------------------------------------------------------------------
    w("## 6. Key Findings and Recommendations")
    w()

    # Sort categories by count for recommendations
    sorted_cats = sorted(category_counts.items(), key=lambda x: -x[1])

    w("### Top Error Sources")
    w()
    for i, (cat, cnt) in enumerate(sorted_cats[:5], 1):
        pct = (cnt / total_errors * 100) if total_errors > 0 else 0
        w(f"{i}. **{cat}** -- {cnt} errors ({pct:.1f}%)")
    w()

    w("### Actionable Recommendations")
    w()

    recommendations = []
    if category_counts.get("COMMAND_FAILED", 0) > 0:
        recommendations.append(
            "**Reduce COMMAND_FAILED errors:** Many bash command failures come from test suites "
            "and missing dependencies. Add CLAUDE.md instructions to check command availability "
            "before running, and use `which` or `command -v` to verify tools exist."
        )
    if category_counts.get("FILE_NOT_FOUND", 0) > 0:
        recommendations.append(
            "**Reduce FILE_NOT_FOUND errors:** Agent frequently tries to read files that do not exist. "
            "Add CLAUDE.md instruction: 'Always use Glob or ls to verify file paths before reading.'"
        )
    if category_counts.get("EDIT_FAILED", 0) > 0:
        recommendations.append(
            "**Reduce EDIT_FAILED errors:** String matching failures in Edit tool. "
            "Ensure the agent always reads the latest file content before editing, "
            "and uses more context in old_string for uniqueness."
        )
    if category_counts.get("FILE_TOO_LARGE", 0) > 0:
        recommendations.append(
            "**Reduce FILE_TOO_LARGE errors:** Agent attempts to read very large files. "
            "Add CLAUDE.md instruction: 'For files over 200KB, always use offset/limit or Grep instead of Read.'"
        )
    if category_counts.get("HOOK_BLOCKED", 0) > 0:
        recommendations.append(
            "**Reduce HOOK_BLOCKED errors:** Custom hooks are blocking operations the agent attempts. "
            "Review hook scripts and add clearer CLAUDE.md rules about restricted operations."
        )
    if category_counts.get("USER_REJECTED", 0) > 0:
        recommendations.append(
            "**Reduce USER_REJECTED errors:** The agent proposes actions the user declines. "
            "This may indicate the agent should ask for confirmation earlier in its reasoning, "
            "or CLAUDE.md constraints are not specific enough."
        )
    if category_counts.get("SIBLING_ERROR", 0) > 0:
        recommendations.append(
            "**Reduce SIBLING_ERROR errors:** Parallel tool calls fail as a cascade. "
            "Consider making dependent tool calls sequential, or validate inputs before batching."
        )
    if category_counts.get("FILE_NOT_READ", 0) > 0:
        recommendations.append(
            "**Reduce FILE_NOT_READ errors:** Agent writes without reading first. "
            "This is already enforced by the tool, but the agent still attempts it. "
            "Reinforce in CLAUDE.md: 'ALWAYS read a file before writing or editing it.'"
        )

    for i, rec in enumerate(recommendations, 1):
        w(f"{i}. {rec}")
        w()

    # Write output
    with open(output_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\nReport written to: {output_path}")
    print(f"\nQuick summary:")
    print(f"  Total errors: {total_errors}")
    print(f"  Categories found: {len(category_counts)}")
    print(f"  Top 3 categories:")
    for cat, cnt in sorted_cats[:3]:
        pct = (cnt / total_errors * 100) if total_errors > 0 else 0
        print(f"    {cat}: {cnt} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
