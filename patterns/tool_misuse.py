#!/usr/bin/env python3
"""
Detect tool misuse patterns in Claude Code conversation transcripts.

Scans JSONL session files for cases where suboptimal tools were chosen:
1. Bash used when Read tool would have been better (cat, head, tail)
2. Bash used when Write tool would have been better (echo > file, cat << EOF > file)
3. Read tool on very large files without offset/limit
4. Multiple small Reads of the same file instead of one full read
5. Grep/find via Bash instead of Grep/Glob tools
6. Agent spawned for simple tasks that Grep/Glob could handle
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path

# Regex patterns for detecting Bash misuse
BASH_AS_READ_PATTERNS = [
    (r"^cat\s+", "cat file"),
    (r"^head\s+(?!-c)", "head file"),
    (r"^tail\s+", "tail file"),
    (r"\|\s*cat\s*$", "pipe to cat"),
    (r"^less\s+", "less file"),
    (r"^more\s+", "more file"),
]

BASH_AS_WRITE_PATTERNS = [
    (r'echo\s+["\'].*["\']\s*>\s*\S+', "echo > file"),
    (r'echo\s+["\'].*["\']\s*>>\s*\S+', "echo >> file"),
    (r"cat\s*<<\s*['\"]?EOF", "cat << EOF > file"),
    (r"printf\s+.*>\s*\S+", "printf > file"),
    (r"tee\s+\S+\s*<<", "tee << heredoc"),
]

BASH_AS_GREP_PATTERNS = [
    (r"^grep\s+", "grep via Bash"),
    (r"^rg\s+", "rg via Bash"),
    (r"\|\s*grep\s+", "pipe to grep"),
    (r"^ag\s+", "ag via Bash"),
    (r"^ack\s+", "ack via Bash"),
]

BASH_AS_GLOB_PATTERNS = [
    (r"^find\s+\S+\s+-name\s+", "find -name via Bash"),
    (r"^find\s+\S+\s+-type\s+f", "find -type f via Bash"),
    (r"^ls\s+.*\*", "ls with glob via Bash"),
]

# Exceptions: legitimate Bash uses that look like misuse but aren't
BASH_LEGITIMATE_EXCEPTIONS = [
    r"pnpm\s+build.*\|\s*(head|tail)",  # build output piped
    r"npm\s+.*\|\s*(head|tail)",
    r"pytest.*\|\s*(head|tail)",
    r"git\s+log.*\|\s*head",
    r"git\s+diff.*\|\s*head",
    r"2>&1\s*\|\s*(head|tail)",  # stderr redirect piped
    r"curl\s+.*\|\s*(head|grep)",  # curl piped
    r"ls\s+-la\b",  # ls -la is directory listing, not file reading
    r"^ls\s+\S+/?$",  # simple ls of a directory
    r"^ls\s+-[a-zA-Z]*\s+\S+/?$",  # ls with flags
    r"^git\s+commit\s+-m\s+\"\$\(cat\s+<<",  # git commit with heredoc
    r"^ssh\s+",  # remote commands via SSH can't use local tools
    r"docker\s+(exec|run)",  # docker commands can't use local tools
]

# Commands where piped grep is legitimate (filtering process output, not searching files)
PROCESS_GREP_LEGITIMATE = [
    r"(npm|pnpm|yarn)\s+",  # package manager output
    r"(pnpm|npm)\s+(run|build|test|dev)",
    r"vercel\s+",  # deploy output
    r"git\s+(log|diff|status|branch|remote|show|stash)",
    r"docker\s+",
    r"kubectl\s+",
    r"2>&1\s*\|\s*grep",  # stderr+stdout piped to grep
    r"ps\s+",  # process listing
    r"lsof\s+",  # file/port listing
    r"brew\s+",
    r"pip\s+",
    r"cargo\s+",
    r"go\s+(build|test|run)",
    r"make\s+",
    r"(pytest|jest|vitest)",  # test output
    r"fc-list",  # font listing
    r"wc\s+",
    r"sort\s+",
    r"env\s+",
    r"printenv",
    r"xcodebuild",
    r"swift\s+",
    r"python3?\s+",  # running python scripts
    r"node\s+",
    r"deno\s+",
    r"bun\s+",
    r"pkill\s+",
    r"pgrep\s+",
]

# Agent overkill patterns: simple tasks that Agent was spawned for
AGENT_SIMPLE_PATTERNS = [
    r"find.*file",
    r"search.*for.*\bstring\b",
    r"grep.*pattern",
    r"list.*files",
    r"check.*if.*exists",
    r"read.*file",
    r"what.*is.*in",
    r"show.*contents",
    r"look.*for",
]


def is_legitimate_exception(cmd):
    """Check if a Bash command matches a legitimate use pattern."""
    for pat in BASH_LEGITIMATE_EXCEPTIONS:
        if re.search(pat, cmd, re.IGNORECASE):
            return True
    return False


def is_process_grep(cmd):
    """Check if grep is being used to filter process output (legitimate), not search files."""
    for pat in PROCESS_GREP_LEGITIMATE:
        if re.search(pat, cmd, re.IGNORECASE):
            return True
    return False


def detect_bash_misuse(cmd):
    """Detect suboptimal Bash tool use. Returns list of (category, pattern_name) tuples."""
    findings = []
    cmd_stripped = cmd.strip()

    if is_legitimate_exception(cmd_stripped):
        return findings

    # Check Bash-as-Read patterns
    for pat, name in BASH_AS_READ_PATTERNS:
        if re.search(pat, cmd_stripped, re.IGNORECASE):
            # cat > file is a write pattern, not a read pattern
            if name == "cat file" and re.search(r"cat\s*>", cmd_stripped):
                continue
            # cat << heredoc is a write pattern
            if name == "cat file" and re.search(r"cat\s*<<", cmd_stripped):
                continue
            # cat with pipe is grep misuse, not read misuse
            if name == "cat file" and "|" in cmd_stripped:
                continue
            findings.append(("bash_instead_of_read", name))

    # Check Bash-as-Write patterns
    for pat, name in BASH_AS_WRITE_PATTERNS:
        if re.search(pat, cmd_stripped, re.IGNORECASE):
            # git commit heredocs are not file writes
            if re.search(r"git\s+commit", cmd_stripped):
                continue
            # echo inside conditionals/checks (grep -q || echo) is not file writing
            if name in ("echo > file", "echo >> file"):
                # grep -q ... || echo ... >> file is a conditional append (legitimate shell pattern)
                if re.search(r"grep\s+-q.*\|\|.*echo", cmd_stripped):
                    continue
            findings.append(("bash_instead_of_write", name))

    # Check Bash-as-Grep patterns
    for pat, name in BASH_AS_GREP_PATTERNS:
        if re.search(pat, cmd_stripped, re.IGNORECASE):
            if name == "pipe to grep":
                # Only flag if grepping file contents, not process output
                if is_process_grep(cmd_stripped):
                    continue
            # Standalone grep/rg on files IS misuse (should use Grep tool)
            # But grep -q (silent check) in shell conditionals is legitimate
            if name in ("grep via Bash", "rg via Bash"):
                if re.search(r"grep\s+-[a-zA-Z]*q", cmd_stripped):
                    continue
            findings.append(("bash_instead_of_grep", name))

    # Check Bash-as-Glob patterns
    for pat, name in BASH_AS_GLOB_PATTERNS:
        if re.search(pat, cmd_stripped, re.IGNORECASE):
            findings.append(("bash_instead_of_glob", name))

    return findings


def detect_read_misuse(tool_uses_in_session):
    """Detect Read tool misuse patterns across a session.

    Returns list of findings.
    """
    findings = []
    read_calls = [t for t in tool_uses_in_session if t["name"] == "Read"]

    # Track reads per file
    file_reads = defaultdict(list)
    for r in read_calls:
        fp = r.get("input", {}).get("file_path", "")
        if fp:
            file_reads[fp].append(r)

    # Pattern 3: Read on large files without offset/limit
    # We can't know file size from the tool_use, but we can check if offset/limit were used
    # We check tool_result size as a proxy (if available)
    for r in read_calls:
        inp = r.get("input", {})
        fp = inp.get("file_path", "")
        has_offset = "offset" in inp and inp["offset"] is not None
        has_limit = "limit" in inp and inp["limit"] is not None
        result_size = r.get("result_size", 0)

        # If result was very large (>2000 lines ~ reading whole big file), flag it
        if result_size > 80000 and not has_offset and not has_limit:
            findings.append({
                "pattern": "read_large_file_no_limit",
                "file": fp,
                "result_chars": result_size,
                "detail": f"Read {fp} ({result_size} chars result) without offset/limit",
            })

    # Pattern 4: Multiple small reads of same file
    for fp, reads in file_reads.items():
        if len(reads) >= 3:
            # Check if they used offset (intentional chunking) vs. repeated full reads
            offset_reads = sum(1 for r in reads if r.get("input", {}).get("offset") is not None)
            if offset_reads < len(reads) // 2:
                findings.append({
                    "pattern": "repeated_reads_same_file",
                    "file": fp,
                    "count": len(reads),
                    "detail": f"Read {fp} {len(reads)} times ({offset_reads} with offset)",
                })

    return findings


def detect_agent_overkill(tool_uses_in_session):
    """Detect Agent tool spawned for tasks that Grep/Glob could handle."""
    findings = []
    agent_calls = [t for t in tool_uses_in_session if t["name"] == "Agent"]

    for a in agent_calls:
        prompt = a.get("input", {}).get("prompt", "")
        prompt_lower = prompt.lower()

        for pat in AGENT_SIMPLE_PATTERNS:
            if re.search(pat, prompt_lower):
                # Check prompt length as proxy for complexity
                # Very short prompts with simple search intent are overkill
                if len(prompt) < 200:
                    findings.append({
                        "pattern": "agent_overkill",
                        "prompt": prompt[:200],
                        "matched_pattern": pat,
                        "detail": f"Agent spawned for simple task: '{prompt[:100]}...'",
                    })
                    break

    return findings


def analyze_session(filepath):
    """Analyze a single session JSONL file for tool misuse patterns.

    Returns dict with session info and findings.
    """
    session_info = {
        "file": str(filepath),
        "slug": None,
        "session_id": None,
        "model": None,
        "timestamp": None,
    }
    all_tool_uses = []
    findings = []
    tool_results = {}  # tool_use_id -> result text length

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            msg_type = obj.get("type")

            # Extract session metadata
            if msg_type in ("user", "assistant") and not session_info["slug"]:
                session_info["slug"] = obj.get("slug")
                session_info["session_id"] = obj.get("sessionId")
                session_info["timestamp"] = obj.get("timestamp")

            if msg_type == "assistant":
                msg = obj.get("message", {})
                if msg.get("model"):
                    session_info["model"] = msg["model"]

                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue

                    name = block.get("name", "")
                    inp = block.get("input", {})
                    tool_id = block.get("id", "")

                    tool_record = {
                        "name": name,
                        "input": inp,
                        "id": tool_id,
                        "result_size": 0,
                    }

                    # Pattern 1 & 2 & 5 & 6: Bash misuse
                    if name == "Bash":
                        cmd = inp.get("command", "")
                        bash_findings = detect_bash_misuse(cmd)
                        for category, pattern_name in bash_findings:
                            findings.append({
                                "pattern": category,
                                "command": cmd[:300],
                                "matched": pattern_name,
                                "detail": f"Bash({cmd[:150]})",
                            })

                    all_tool_uses.append(tool_record)

            elif msg_type == "user":
                # Look for tool results to measure result sizes
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tid = block.get("tool_use_id", "")
                            result_content = block.get("content", "")
                            if isinstance(result_content, str):
                                size = len(result_content)
                            elif isinstance(result_content, list):
                                size = sum(
                                    len(sub.get("text", ""))
                                    for sub in result_content
                                    if isinstance(sub, dict) and sub.get("type") == "text"
                                )
                            else:
                                size = 0
                            tool_results[tid] = size

    # Attach result sizes to tool records
    for t in all_tool_uses:
        if t["id"] in tool_results:
            t["result_size"] = tool_results[t["id"]]

    # Patterns 3 & 4: Read misuse
    read_findings = detect_read_misuse(all_tool_uses)
    findings.extend(read_findings)

    # Pattern 6: Agent overkill
    agent_findings = detect_agent_overkill(all_tool_uses)
    findings.extend(agent_findings)

    return {
        "session": session_info,
        "findings": findings,
        "tool_count": len(all_tool_uses),
    }


def list_sessions(min_size_kb=100, limit=50):
    """List JSONL session files sorted by recency, skipping subagents."""
    sessions = []
    for root, dirs, files in os.walk(CLAUDE_PROJECTS_DIR):
        if "subagents" in root:
            continue
        for fn in files:
            if fn.endswith(".jsonl"):
                fp = Path(root) / fn
                try:
                    size = fp.stat().st_size
                    if size >= min_size_kb * 1024:
                        mtime = fp.stat().st_mtime
                        sessions.append((mtime, size, fp))
                except OSError:
                    continue
    sessions.sort(reverse=True)
    return sessions[:limit]


def generate_report(results):
    """Generate a human-readable report from analysis results."""
    lines = []
    lines.append("=" * 80)
    lines.append("  TOOL MISUSE ANALYSIS REPORT")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    # Aggregate stats
    total_findings = 0
    pattern_counts = Counter()
    category_examples = defaultdict(list)
    sessions_with_findings = 0

    for r in results:
        if r["findings"]:
            sessions_with_findings += 1
        for f in r["findings"]:
            total_findings += 1
            pat = f.get("pattern", "unknown")
            pattern_counts[pat] += 1
            if len(category_examples[pat]) < 5:
                category_examples[pat].append({
                    "session": r["session"].get("slug") or r["session"].get("session_id") or Path(r["session"]["file"]).stem,
                    "detail": f.get("detail", ""),
                    "command": f.get("command", ""),
                    "file": f.get("file", ""),
                })

    lines.append(f"Sessions analyzed:        {len(results)}")
    lines.append(f"Sessions with misuse:     {sessions_with_findings}")
    lines.append(f"Total findings:           {total_findings}")
    lines.append("")

    # Summary by category
    lines.append("-" * 80)
    lines.append("  SUMMARY BY PATTERN")
    lines.append("-" * 80)
    lines.append("")

    category_labels = {
        "bash_instead_of_read": "1. Bash instead of Read (cat/head/tail file)",
        "bash_instead_of_write": "2. Bash instead of Write (echo/cat > file)",
        "bash_instead_of_grep": "5. Bash instead of Grep (grep/rg via Bash)",
        "bash_instead_of_glob": "5. Bash instead of Glob (find via Bash)",
        "read_large_file_no_limit": "3. Read large file without offset/limit",
        "repeated_reads_same_file": "4. Multiple reads of same file",
        "agent_overkill": "6. Agent for simple Grep/Glob task",
    }

    for pat, count in pattern_counts.most_common():
        label = category_labels.get(pat, pat)
        lines.append(f"  {label}")
        lines.append(f"    Count: {count}")
        lines.append("")

    # Detailed examples per category
    lines.append("")
    lines.append("=" * 80)
    lines.append("  DETAILED FINDINGS BY PATTERN")
    lines.append("=" * 80)

    for pat in [
        "bash_instead_of_read",
        "bash_instead_of_write",
        "read_large_file_no_limit",
        "repeated_reads_same_file",
        "bash_instead_of_grep",
        "bash_instead_of_glob",
        "agent_overkill",
    ]:
        examples = category_examples.get(pat, [])
        if not examples:
            continue

        label = category_labels.get(pat, pat)
        lines.append("")
        lines.append(f"--- {label} ---")
        lines.append(f"    Total occurrences: {pattern_counts[pat]}")
        lines.append("")

        for i, ex in enumerate(examples, 1):
            lines.append(f"  Example {i}: (session: {ex['session']})")
            if ex.get("command"):
                lines.append(f"    Command: {ex['command'][:200]}")
            if ex.get("file"):
                lines.append(f"    File: {ex['file']}")
            if ex.get("detail"):
                lines.append(f"    Detail: {ex['detail']}")
            lines.append("")

    # Per-session breakdown (only sessions with findings, top 20)
    lines.append("")
    lines.append("=" * 80)
    lines.append("  PER-SESSION BREAKDOWN (top 20 by finding count)")
    lines.append("=" * 80)
    lines.append("")

    session_finding_counts = []
    for r in results:
        if r["findings"]:
            slug = r["session"].get("slug") or Path(r["session"]["file"]).stem
            per_pattern = Counter(f["pattern"] for f in r["findings"])
            session_finding_counts.append((len(r["findings"]), slug, per_pattern, r["tool_count"]))

    session_finding_counts.sort(reverse=True)
    for count, slug, per_pattern, tool_count in session_finding_counts[:20]:
        pct = (count / tool_count * 100) if tool_count > 0 else 0
        lines.append(f"  {slug}")
        lines.append(f"    Findings: {count} / {tool_count} tool calls ({pct:.1f}% misuse rate)")
        for pat, c in per_pattern.most_common():
            short = pat.replace("bash_instead_of_", "bash->").replace("_", " ")
            lines.append(f"      {short}: {c}")
        lines.append("")

    # Overall misuse rate
    total_tool_calls = sum(r["tool_count"] for r in results)
    if total_tool_calls > 0:
        lines.append("-" * 80)
        lines.append(f"  Overall: {total_findings} misuse findings across {total_tool_calls} total tool calls")
        lines.append(f"  Misuse rate: {total_findings / total_tool_calls * 100:.2f}%")
        lines.append("-" * 80)

    return "\n".join(lines)


def main():
    print("Scanning for session files (min 100KB, limit 50)...")
    sessions = list_sessions(min_size_kb=100, limit=50)
    print(f"Found {len(sessions)} sessions to analyze.")

    if not sessions:
        print("No sessions found. Check ~/.claude/projects/ path.")
        sys.exit(1)

    results = []
    for i, (mtime, size, fp) in enumerate(sessions):
        dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  [{i+1}/{len(sessions)}] {dt}  {size/1024:.0f}KB  {fp.name[:40]}")
        try:
            result = analyze_session(fp)
            results.append(result)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue

    report = generate_report(results)
    print("\n" + report)

    # Save report
    output_path = _output_path("tool_misuse")
    with open(output_path, "w") as f:
        f.write(report)
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
