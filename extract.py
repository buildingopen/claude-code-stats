#!/usr/bin/env python3
"""
Extract and summarize Claude Code conversation transcripts for analysis.
Produces a compact representation that preserves the conversation flow
while stripping noise (progress updates, file snapshots, binary content).
"""

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path


CLAUDE_PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))

REJECTION_PATTERNS = [
    "the user rejected",
    "user doesn't want to proceed",
    "tool use was rejected",
    "request interrupted by user",
]


def _is_rejection(text):
    """Check if error text indicates a user rejection (not just any 'rejected' string)."""
    t = text.lower()
    return any(pat in t for pat in REJECTION_PATTERNS)


def extract_text_content(content_blocks):
    """Extract text from content blocks."""
    if isinstance(content_blocks, str):
        return content_blocks
    texts = []
    for block in content_blocks:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {})
                # Compact representation of tool calls
                if name in ("Read", "Glob", "Grep"):
                    texts.append(f"[TOOL: {name}({json.dumps(inp, separators=(',', ':'))[:200]})]")
                elif name == "Bash":
                    cmd = inp.get("command", "")[:300]
                    texts.append(f"[TOOL: Bash({cmd})]")
                elif name in ("Edit", "Write"):
                    fp = inp.get("file_path", "")
                    old = (inp.get("old_string", "") or "")[:100]
                    new = (inp.get("new_string", "") or "")[:100]
                    if name == "Edit":
                        texts.append(f"[TOOL: Edit({fp}, '{old}' -> '{new}')]")
                    else:
                        texts.append(f"[TOOL: Write({fp}, {len(inp.get('content', ''))} chars)]")
                elif name == "Agent":
                    prompt = inp.get("prompt", "")[:200]
                    texts.append(f"[TOOL: Agent({prompt})]")
                else:
                    texts.append(f"[TOOL: {name}({json.dumps(inp, separators=(',', ':'))[:150]})]")
            elif block.get("type") == "tool_result":
                content = block.get("content", "")
                is_error = block.get("is_error", False)
                if is_error:
                    if isinstance(content, str):
                        texts.append(f"[ERROR: {content[:300]}]")
                    elif isinstance(content, list):
                        for sub in content:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                texts.append(f"[ERROR: {sub.get('text', '')[:300]}]")
                else:
                    if isinstance(content, str) and content.strip():
                        # Truncate large tool results
                        texts.append(f"[RESULT: {content[:500]}]")
                    elif isinstance(content, list):
                        for sub in content:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                texts.append(f"[RESULT: {sub.get('text', '')[:500]}]")
    return "\n".join(texts)


def extract_conversation(filepath):
    """Extract a conversation from a JSONL file into a structured summary."""
    messages = []
    metadata = {
        "file": str(filepath),
        "session_id": None,
        "version": None,
        "slug": None,
        "model": None,
        "start_time": None,
        "end_time": None,
        "cwd": None,
        "git_branch": None,
    }
    tool_usage = Counter()
    rejections = 0
    errors = 0
    total_input_tokens = 0
    total_output_tokens = 0

    with open(filepath) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")
            timestamp = obj.get("timestamp")

            # Skip noise
            if msg_type in ("progress", "file-history-snapshot", "queue-operation"):
                continue

            # Extract metadata from first assistant message
            if msg_type == "assistant" and not metadata["session_id"]:
                metadata["session_id"] = obj.get("sessionId")
                metadata["version"] = obj.get("version")
                metadata["slug"] = obj.get("slug")
                metadata["cwd"] = obj.get("cwd")
                metadata["git_branch"] = obj.get("gitBranch")

            # Track timestamps
            if timestamp:
                if not metadata["start_time"] or timestamp < metadata["start_time"]:
                    metadata["start_time"] = timestamp
                if not metadata["end_time"] or timestamp > metadata["end_time"]:
                    metadata["end_time"] = timestamp

            if msg_type == "assistant":
                msg = obj.get("message", {})
                model = msg.get("model")
                if model:
                    metadata["model"] = model

                # Token usage
                usage = msg.get("usage", {})
                total_input_tokens += usage.get("input_tokens", 0)
                total_input_tokens += usage.get("cache_read_input_tokens", 0)
                total_input_tokens += usage.get("cache_creation_input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)

                content = msg.get("content", [])
                # Count tool uses
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_usage[block.get("name", "unknown")] += 1

                text = extract_text_content(content)
                if text.strip():
                    messages.append({
                        "role": "assistant",
                        "text": text,
                        "timestamp": timestamp,
                    })

            elif msg_type == "user":
                msg = obj.get("message", {})
                content = msg.get("content", [])

                # Count errors and rejections
                for block in content:
                    if isinstance(block, dict):
                        if block.get("is_error"):
                            errors += 1
                            c = block.get("content", "")
                            if isinstance(c, str) and _is_rejection(c):
                                rejections += 1

                text = extract_text_content(content)
                if text.strip():
                    messages.append({
                        "role": "user",
                        "text": text,
                        "timestamp": timestamp,
                    })

            elif msg_type == "system":
                msg = obj.get("message", {})
                content = msg.get("content", [])
                text = extract_text_content(content)
                if text.strip():
                    messages.append({
                        "role": "system",
                        "text": text[:500],
                        "timestamp": timestamp,
                    })

    # Calculate duration
    duration_min = None
    if metadata["start_time"] and metadata["end_time"]:
        try:
            start = datetime.fromisoformat(metadata["start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(metadata["end_time"].replace("Z", "+00:00"))
            duration_min = round((end - start).total_seconds() / 60, 1)
        except Exception:
            pass

    return {
        "metadata": metadata,
        "stats": {
            "message_count": len(messages),
            "tool_usage": dict(tool_usage.most_common()),
            "rejections": rejections,
            "errors": errors,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "duration_minutes": duration_min,
        },
        "messages": messages,
    }


def list_conversations(projects_dir=None, min_size_kb=10, max_size_kb=None,
                       limit=None, include_subagents=False):
    """List all conversation files, sorted by recency."""
    base = Path(projects_dir) if projects_dir else CLAUDE_PROJECTS_DIR
    convos = []
    for root, dirs, files in os.walk(base):
        if not include_subagents and "subagents" in root:
            continue
        for fn in files:
            if fn.endswith(".jsonl"):
                fp = Path(root) / fn
                size = fp.stat().st_size
                if size >= min_size_kb * 1024:
                    if max_size_kb and size > max_size_kb * 1024:
                        continue
                    mtime = fp.stat().st_mtime
                    convos.append((mtime, size, fp))
    convos.sort(reverse=True)
    if limit:
        convos = convos[:limit]
    return convos


def format_conversation_for_analysis(convo, max_messages=200):
    """Format extracted conversation data into a text block for LLM analysis."""
    meta = convo["metadata"]
    stats = convo["stats"]

    lines = []
    lines.append(f"=== SESSION: {meta.get('slug', 'unknown')} ===")
    lines.append(f"Model: {meta.get('model', 'unknown')}")
    lines.append(f"Time: {meta.get('start_time', '?')} to {meta.get('end_time', '?')}")
    lines.append(f"Duration: {stats.get('duration_minutes', '?')} min")
    lines.append(f"CWD: {meta.get('cwd', '?')}")
    lines.append(f"Branch: {meta.get('git_branch', '?')}")
    lines.append(f"Messages: {stats['message_count']}")
    lines.append(f"Tokens: {stats['total_input_tokens']:,} in / {stats['total_output_tokens']:,} out")
    lines.append(f"Tools: {json.dumps(stats['tool_usage'])}")
    lines.append(f"Errors: {stats['errors']}, Rejections: {stats['rejections']}")
    lines.append("")

    for msg in convo["messages"][:max_messages]:
        role = msg["role"].upper()
        text = msg["text"]
        # Truncate very long messages
        if len(text) > 2000:
            text = text[:2000] + f"\n... [truncated, {len(text)} chars total]"
        lines.append(f"[{role}]: {text}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract Claude Code transcripts")
    parser.add_argument("--limit", type=int, default=5, help="Number of recent conversations")
    parser.add_argument("--min-size", type=int, default=100, help="Minimum file size in KB")
    parser.add_argument("--list", action="store_true", help="Just list conversations")
    parser.add_argument("--extract", type=str, help="Extract specific session file")
    parser.add_argument("--stats-only", action="store_true", help="Only show stats, no messages")
    args = parser.parse_args()

    if args.extract:
        convo = extract_conversation(args.extract)
        if args.stats_only:
            print(json.dumps({"metadata": convo["metadata"], "stats": convo["stats"]}, indent=2))
        else:
            print(format_conversation_for_analysis(convo))
    elif args.list:
        convos = list_conversations(min_size_kb=args.min_size, limit=args.limit)
        for mtime, size, fp in convos:
            dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{dt}  {size/1024/1024:>6.1f} MB  {fp.name}")
    else:
        convos = list_conversations(min_size_kb=args.min_size, limit=args.limit)
        for mtime, size, fp in convos:
            convo = extract_conversation(fp)
            if args.stats_only:
                meta = convo["metadata"]
                stats = convo["stats"]
                print(f"{meta.get('slug', fp.name):40s}  {stats['duration_minutes'] or '?':>6} min  "
                      f"msgs={stats['message_count']:>4}  errs={stats['errors']:>3}  "
                      f"rej={stats['rejections']:>2}  tools={sum(stats['tool_usage'].values()):>4}")
            else:
                print(format_conversation_for_analysis(convo))
                print("\n" + "=" * 80 + "\n")
