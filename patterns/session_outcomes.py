#!/usr/bin/env python3
"""
Analyze Claude Code conversation JSONL files to classify session outcomes and productivity.

Processes all sessions in ~/.claude/projects/, extracting:
- Success/failure signals
- Productivity metrics (files edited, read, commands, LOC, commits, deployments)
- Session categorization (BUILD, FIX, EXPLORE, DEPLOY, DESIGN, PLAN, MIXED)
- Top 10 most/least productive sessions
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path, resolve_project_name
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path, resolve_project_name
PROJECTS_DIR = CLAUDE_PROJECTS_DIR
OUTPUT_PATH = _output_path("session_outcomes")

POSITIVE_WORDS = {"thanks", "thank", "perfect", "great", "done", "awesome", "nice", "good", "works", "excellent", "beautiful", "love"}
DEPLOY_KEYWORDS = {"vercel", "deploy", "push", "railway", "render", "netlify", "heroku"}


def parse_jsonl(filepath):
    """Parse a JSONL file, skipping empty and malformed lines."""
    messages = []
    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                messages.append(obj)
            except (json.JSONDecodeError, ValueError):
                continue
    return messages


def extract_content_blocks(msg):
    """Extract content blocks from a message object."""
    message = msg.get("message", {})
    content = message.get("content", [])
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def analyze_session(filepath):
    """Analyze a single session JSONL file and return metrics."""
    messages = parse_jsonl(filepath)
    if not messages:
        return None

    session_id = filepath.stem
    # Handle sessions inside subdirectories (e.g., uuid/subagents/)
    project_dir = filepath.parent
    while project_dir != PROJECTS_DIR and project_dir.parent != PROJECTS_DIR:
        project_dir = project_dir.parent
    raw_name = project_dir.name if project_dir.parent == PROJECTS_DIR else filepath.parent.name
    project_name = resolve_project_name(raw_name)

    # Metrics
    files_edited = set()
    files_read = set()
    bash_commands = 0
    loc_added = 0
    loc_removed = 0
    commits = 0
    deployments = 0
    errors = 0
    total_tool_uses = 0
    tool_sequence = []
    user_texts = []
    timestamps = []
    last_user_ts = None
    last_any_ts = None
    session_ended_with_error = False
    cwd = None

    # Category signals
    build_signals = 0
    fix_signals = 0
    explore_signals = 0
    deploy_signals = 0
    design_signals = 0
    plan_signals = 0

    for msg in messages:
        msg_type = msg.get("type")
        ts_str = msg.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                timestamps.append(ts)
            except (ValueError, TypeError):
                pass

        if not cwd and msg.get("cwd"):
            cwd = msg.get("cwd")

        blocks = extract_content_blocks(msg)

        for block in blocks:
            btype = block.get("type", "")

            if btype == "tool_use":
                total_tool_uses += 1
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                tool_sequence.append(tool_name)

                if tool_name == "Bash":
                    bash_commands += 1
                    cmd = tool_input.get("command", "")

                    # Detect git commits
                    if re.search(r"git\s+commit", cmd):
                        commits += 1
                        build_signals += 2

                    # Detect deployments
                    cmd_lower = cmd.lower()
                    for kw in DEPLOY_KEYWORDS:
                        if kw in cmd_lower:
                            if kw == "push" and "git push" in cmd_lower:
                                deployments += 1
                                deploy_signals += 2
                            elif kw != "push":
                                deployments += 1
                                deploy_signals += 2
                            break

                    # Fix signals
                    if any(w in cmd_lower for w in ["test", "lint", "typecheck", "tsc"]):
                        fix_signals += 1

                elif tool_name in ("Edit", "Write"):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        files_edited.add(fp)
                        build_signals += 1

                    if tool_name == "Edit":
                        old_s = tool_input.get("old_string", "")
                        new_s = tool_input.get("new_string", "")
                        old_lines = old_s.count("\n") + (1 if old_s else 0)
                        new_lines = new_s.count("\n") + (1 if new_s else 0)
                        loc_removed += old_lines
                        loc_added += new_lines

                        # Check for CSS/styling edits
                        if any(ext in fp for ext in [".css", ".scss", ".tailwind"]) or \
                           any(kw in new_s.lower() for kw in ["className", "style", "color", "font", "padding", "margin", "border"]):
                            design_signals += 1

                elif tool_name == "Read":
                    fp = tool_input.get("file_path", "")
                    if fp:
                        files_read.add(fp)
                    explore_signals += 1

                elif tool_name in ("Grep", "Glob"):
                    explore_signals += 1

                elif tool_name == "Skill":
                    skill = tool_input.get("skill", "")
                    if "workplan" in skill.lower():
                        plan_signals += 3

            elif btype == "tool_result":
                is_error = block.get("is_error", False)
                if is_error:
                    errors += 1

            elif btype == "text" and msg_type == "user":
                text = block.get("text", "").lower().strip()
                user_texts.append(text)
                if msg_type == "user":
                    if ts_str:
                        try:
                            last_user_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            pass

                # Category signals from user text
                for w in ["fix", "bug", "error", "broken", "wrong", "issue", "crash", "fail"]:
                    if w in text:
                        fix_signals += 2
                        break
                for w in ["deploy", "push", "production", "release", "ship"]:
                    if w in text:
                        deploy_signals += 2
                        break
                for w in ["design", "ui", "ux", "style", "css", "layout", "screenshot", "look"]:
                    if w in text:
                        design_signals += 1
                        break
                for w in ["plan", "workplan", "architecture", "think", "approach", "strategy", "discuss"]:
                    if w in text:
                        plan_signals += 2
                        break
                for w in ["build", "create", "add", "implement", "feature", "new"]:
                    if w in text:
                        build_signals += 1
                        break

    if not timestamps:
        return None

    # Duration
    start_ts = min(timestamps)
    end_ts = max(timestamps)
    duration_minutes = (end_ts - start_ts).total_seconds() / 60

    # Error rate
    error_rate = (errors / total_tool_uses * 100) if total_tool_uses > 0 else 0

    # Detect loops (same tool 5+ times consecutively)
    has_loop = False
    if len(tool_sequence) >= 5:
        for i in range(len(tool_sequence) - 4):
            if len(set(tool_sequence[i:i+5])) == 1:
                has_loop = True
                break

    # Detect abandonment: gap > 10 min between last user message and session end, with no further user activity
    abandoned = False
    if last_user_ts and end_ts:
        gap = (end_ts - last_user_ts).total_seconds() / 60
        if gap > 10 and len(user_texts) > 0:
            # Check if the very last message was from user (not abandoned) or assistant (possibly abandoned)
            last_msg_type = None
            for m in reversed(messages):
                if m.get("type") in ("user", "assistant"):
                    last_msg_type = m.get("type")
                    break
            if last_msg_type == "assistant":
                abandoned = True

    # Check if session ended with error
    for m in reversed(messages):
        blocks = extract_content_blocks(m)
        for b in blocks:
            if b.get("type") == "tool_result" and b.get("is_error"):
                session_ended_with_error = True
                break
            if b.get("type") in ("text", "tool_use"):
                break
        if session_ended_with_error or blocks:
            break

    # Success signals
    positive_ending = False
    for text in reversed(user_texts[-3:]):
        for w in POSITIVE_WORDS:
            if w in text:
                positive_ending = True
                break
        if positive_ending:
            break

    has_commits = commits > 0
    has_deploys = deployments > 0
    low_error_rate = error_rate < 2

    success_score = 0
    if has_commits:
        success_score += 3
    if has_deploys:
        success_score += 3
    if positive_ending:
        success_score += 2
    if low_error_rate:
        success_score += 1

    # Failure signals
    failure_score = 0
    if error_rate > 10:
        failure_score += 3
    if abandoned:
        failure_score += 2
    if has_loop:
        failure_score += 2
    if session_ended_with_error:
        failure_score += 1

    # Determine outcome
    if success_score >= 4:
        outcome = "SUCCESS"
    elif failure_score >= 4:
        outcome = "FAILURE"
    elif success_score > failure_score:
        outcome = "PARTIAL_SUCCESS"
    elif failure_score > success_score:
        outcome = "PARTIAL_FAILURE"
    else:
        outcome = "NEUTRAL"

    # Session category
    category_scores = {
        "BUILD": build_signals,
        "FIX": fix_signals,
        "EXPLORE": explore_signals,
        "DEPLOY": deploy_signals,
        "DESIGN": design_signals,
        "PLAN": plan_signals,
    }
    sorted_cats = sorted(category_scores.items(), key=lambda x: x[1], reverse=True)

    if sorted_cats[0][1] == 0:
        category = "EXPLORE"
    elif sorted_cats[0][1] > 0 and sorted_cats[1][1] > 0:
        ratio = sorted_cats[1][1] / sorted_cats[0][1] if sorted_cats[0][1] > 0 else 0
        if ratio > 0.7:
            category = "MIXED"
        else:
            category = sorted_cats[0][0]
    else:
        category = sorted_cats[0][0]

    # Productivity score: files edited + commits*5 + deployments*3, penalized by error rate
    productivity = (len(files_edited) + commits * 5 + deployments * 3) * max(0, 1 - error_rate / 100)

    # Inefficiency score: high when lots of tool uses but low output
    output_count = len(files_edited) + commits + deployments
    inefficiency = total_tool_uses / max(output_count, 1) * (1 + error_rate / 10)

    return {
        "session_id": session_id,
        "project": project_name,
        "cwd": cwd or "",
        "start": start_ts,
        "end": end_ts,
        "duration_min": round(duration_minutes, 1),
        "files_edited": len(files_edited),
        "files_read": len(files_read),
        "bash_commands": bash_commands,
        "loc_added": loc_added,
        "loc_removed": loc_removed,
        "loc_changed": loc_added + loc_removed,
        "commits": commits,
        "deployments": deployments,
        "errors": errors,
        "total_tool_uses": total_tool_uses,
        "error_rate": round(error_rate, 1),
        "has_loop": has_loop,
        "abandoned": abandoned,
        "positive_ending": positive_ending,
        "session_ended_with_error": session_ended_with_error,
        "outcome": outcome,
        "category": category,
        "productivity": round(productivity, 1),
        "inefficiency": round(inefficiency, 1),
        "success_score": success_score,
        "failure_score": failure_score,
    }


def find_all_sessions():
    """Find all JSONL session files, excluding subagents."""
    sessions = []
    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        if "subagents" in jsonl.parts:
            continue
        sessions.append(jsonl)
    return sessions


def generate_report(results):
    """Generate markdown report from analysis results."""
    lines = []
    lines.append("# Session Outcomes Analysis")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Total sessions analyzed:** {len(results)}")
    lines.append("")

    # Overall stats
    outcomes = Counter(r["outcome"] for r in results)
    categories = Counter(r["category"] for r in results)
    total_commits = sum(r["commits"] for r in results)
    total_deploys = sum(r["deployments"] for r in results)
    total_files_edited = sum(r["files_edited"] for r in results)
    total_files_read = sum(r["files_read"] for r in results)
    total_bash = sum(r["bash_commands"] for r in results)
    total_loc = sum(r["loc_changed"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    total_tool_uses = sum(r["total_tool_uses"] for r in results)
    sessions_with_loops = sum(1 for r in results if r["has_loop"])
    sessions_abandoned = sum(1 for r in results if r["abandoned"])

    lines.append("## Summary")
    lines.append("")
    lines.append("### Outcome Distribution")
    lines.append("")
    lines.append("| Outcome | Count | % |")
    lines.append("|---------|-------|---|")
    for outcome in ["SUCCESS", "PARTIAL_SUCCESS", "NEUTRAL", "PARTIAL_FAILURE", "FAILURE"]:
        count = outcomes.get(outcome, 0)
        pct = round(count / len(results) * 100, 1) if results else 0
        lines.append(f"| {outcome} | {count} | {pct}% |")
    lines.append("")

    lines.append("### Category Distribution")
    lines.append("")
    lines.append("| Category | Count | % |")
    lines.append("|----------|-------|---|")
    for cat in ["BUILD", "FIX", "EXPLORE", "DEPLOY", "DESIGN", "PLAN", "MIXED"]:
        count = categories.get(cat, 0)
        pct = round(count / len(results) * 100, 1) if results else 0
        lines.append(f"| {cat} | {count} | {pct}% |")
    lines.append("")

    lines.append("### Aggregate Metrics")
    lines.append("")
    lines.append(f"- **Total commits:** {total_commits}")
    lines.append(f"- **Total deployments:** {total_deploys}")
    lines.append(f"- **Total files edited:** {total_files_edited}")
    lines.append(f"- **Total files read:** {total_files_read}")
    lines.append(f"- **Total bash commands:** {total_bash}")
    lines.append(f"- **Total LOC changed (estimate):** {total_loc}")
    lines.append(f"- **Total tool uses:** {total_tool_uses}")
    lines.append(f"- **Total errors:** {total_errors}")
    overall_err = round(total_errors / total_tool_uses * 100, 1) if total_tool_uses else 0
    lines.append(f"- **Overall error rate:** {overall_err}%")
    lines.append(f"- **Sessions with loops:** {sessions_with_loops}")
    lines.append(f"- **Sessions with abandonment:** {sessions_abandoned}")
    lines.append("")

    # Failure signals breakdown
    lines.append("### Failure Signal Breakdown")
    lines.append("")
    high_err = sum(1 for r in results if r["error_rate"] > 10)
    ended_err = sum(1 for r in results if r["session_ended_with_error"])
    lines.append(f"- **High error rate (>10%):** {high_err} sessions")
    lines.append(f"- **User abandonment:** {sessions_abandoned} sessions")
    lines.append(f"- **Agent stuck in loops:** {sessions_with_loops} sessions")
    lines.append(f"- **Ended with error:** {ended_err} sessions")
    lines.append("")

    # Success signals breakdown
    lines.append("### Success Signal Breakdown")
    lines.append("")
    with_commits = sum(1 for r in results if r["commits"] > 0)
    with_deploys = sum(1 for r in results if r["deployments"] > 0)
    with_positive = sum(1 for r in results if r["positive_ending"])
    with_low_err = sum(1 for r in results if r["error_rate"] < 2)
    lines.append(f"- **Sessions with commits:** {with_commits}")
    lines.append(f"- **Sessions with deployments:** {with_deploys}")
    lines.append(f"- **Positive user ending:** {with_positive}")
    lines.append(f"- **Low error rate (<2%):** {with_low_err}")
    lines.append("")

    # Top 10 most productive
    lines.append("## Top 10 Most Productive Sessions")
    lines.append("")
    lines.append("Ranked by productivity score (files edited + commits*5 + deployments*3, penalized by error rate).")
    lines.append("")
    lines.append("| # | Session ID | Project | Category | Files Edited | Commits | Deploys | LOC Changed | Error Rate | Productivity |")
    lines.append("|---|-----------|---------|----------|-------------|---------|---------|-------------|------------|-------------|")
    top_productive = sorted(results, key=lambda x: x["productivity"], reverse=True)[:10]
    for i, r in enumerate(top_productive, 1):
        sid = r["session_id"][:12]
        lines.append(
            f"| {i} | `{sid}` | {r['project'][:30]} | {r['category']} | "
            f"{r['files_edited']} | {r['commits']} | {r['deployments']} | "
            f"{r['loc_changed']} | {r['error_rate']}% | {r['productivity']} |"
        )
    lines.append("")

    # Top 10 least productive
    lines.append("## Top 10 Least Productive Sessions")
    lines.append("")
    lines.append("Ranked by inefficiency score (tool uses / output, amplified by error rate). Only sessions with 10+ tool uses.")
    lines.append("")
    lines.append("| # | Session ID | Project | Category | Tool Uses | Files Edited | Commits | Errors | Error Rate | Inefficiency |")
    lines.append("|---|-----------|---------|----------|-----------|-------------|---------|--------|------------|-------------|")
    # Filter to sessions with meaningful activity
    active_sessions = [r for r in results if r["total_tool_uses"] >= 10]
    top_inefficient = sorted(active_sessions, key=lambda x: x["inefficiency"], reverse=True)[:10]
    for i, r in enumerate(top_inefficient, 1):
        sid = r["session_id"][:12]
        lines.append(
            f"| {i} | `{sid}` | {r['project'][:30]} | {r['category']} | "
            f"{r['total_tool_uses']} | {r['files_edited']} | {r['commits']} | "
            f"{r['errors']} | {r['error_rate']}% | {r['inefficiency']} |"
        )
    lines.append("")

    # Per-project breakdown
    lines.append("## Per-Project Summary")
    lines.append("")
    project_stats = defaultdict(lambda: {
        "sessions": 0, "commits": 0, "deploys": 0, "files_edited": 0,
        "errors": 0, "tool_uses": 0, "outcomes": Counter(), "categories": Counter()
    })
    for r in results:
        p = r["project"]
        ps = project_stats[p]
        ps["sessions"] += 1
        ps["commits"] += r["commits"]
        ps["deploys"] += r["deployments"]
        ps["files_edited"] += r["files_edited"]
        ps["errors"] += r["errors"]
        ps["tool_uses"] += r["total_tool_uses"]
        ps["outcomes"][r["outcome"]] += 1
        ps["categories"][r["category"]] += 1

    lines.append("| Project | Sessions | Commits | Deploys | Files Edited | Error Rate | Top Category | Top Outcome |")
    lines.append("|---------|----------|---------|---------|-------------|------------|-------------|------------|")
    for proj in sorted(project_stats.keys()):
        ps = project_stats[proj]
        err_rate = round(ps["errors"] / ps["tool_uses"] * 100, 1) if ps["tool_uses"] else 0
        top_cat = ps["categories"].most_common(1)[0][0] if ps["categories"] else "N/A"
        top_out = ps["outcomes"].most_common(1)[0][0] if ps["outcomes"] else "N/A"
        lines.append(
            f"| {proj[:40]} | {ps['sessions']} | {ps['commits']} | {ps['deploys']} | "
            f"{ps['files_edited']} | {err_rate}% | {top_cat} | {top_out} |"
        )
    lines.append("")

    # Session details (all sessions)
    lines.append("## All Sessions")
    lines.append("")
    lines.append("| Session ID | Project | Date | Duration | Category | Outcome | Files Ed. | Commits | Err Rate |")
    lines.append("|-----------|---------|------|----------|----------|---------|-----------|---------|----------|")
    sorted_results = sorted(results, key=lambda x: x["start"], reverse=True)
    for r in sorted_results:
        sid = r["session_id"][:12]
        date = r["start"].strftime("%Y-%m-%d")
        dur = f"{r['duration_min']}m"
        lines.append(
            f"| `{sid}` | {r['project'][:25]} | {date} | {dur} | "
            f"{r['category']} | {r['outcome']} | {r['files_edited']} | "
            f"{r['commits']} | {r['error_rate']}% |"
        )
    lines.append("")

    return "\n".join(lines)


def main():
    print("Finding session files...")
    session_files = find_all_sessions()
    print(f"Found {len(session_files)} session files")

    results = []
    skipped = 0
    for i, sf in enumerate(session_files):
        if (i + 1) % 50 == 0:
            print(f"  Processing {i+1}/{len(session_files)}...")
        try:
            result = analyze_session(sf)
            if result:
                results.append(result)
            else:
                skipped += 1
        except Exception as e:
            print(f"  Error processing {sf.name}: {e}")
            skipped += 1

    print(f"Analyzed {len(results)} sessions, skipped {skipped}")

    report = generate_report(results)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report)
    print(f"Report saved to {OUTPUT_PATH}")

    # Print summary to stdout
    outcomes = Counter(r["outcome"] for r in results)
    categories = Counter(r["category"] for r in results)
    print(f"\nOutcome distribution: {dict(outcomes)}")
    print(f"Category distribution: {dict(categories)}")
    print(f"Total commits: {sum(r['commits'] for r in results)}")
    print(f"Total files edited: {sum(r['files_edited'] for r in results)}")


if __name__ == "__main__":
    main()
