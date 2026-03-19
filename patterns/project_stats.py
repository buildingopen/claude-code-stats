#!/usr/bin/env python3
"""
Extract detailed project/repo usage statistics from Claude Code JSONL transcripts.

Processes ALL sessions across all project directories in ~/.claude/projects/,
extracts per-project stats, git branch analysis, time-of-day patterns,
session size distributions, model usage over time, and token cost estimates.

Output: patterns/project_stats.md
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extract import extract_conversation

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path, resolve_project_name, REJECTION_PATTERNS
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path, resolve_project_name, REJECTION_PATTERNS
OUTPUT_FILE = _output_path("project_stats")

# Pricing per million tokens
PRICING = {
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
}


def classify_model(model_name):
    """Classify a model string into a pricing tier."""
    if not model_name:
        return "sonnet"
    m = model_name.lower()
    if "opus" in m:
        return "opus"
    return "sonnet"


def derive_project_name(project_dir_name, cwd):
    """Derive a human-readable project name from the project directory and cwd.

    Uses the centralized resolve_project_name() for consistent naming.
    Tries cwd first (real path), falls back to encoded project_dir_name.
    """
    if cwd:
        result = resolve_project_name(cwd)
        if result != "Unknown":
            return result
    return resolve_project_name(project_dir_name)


def collect_all_sessions():
    """Walk all project directories and extract stats from every JSONL session."""
    sessions = []

    for proj_dir in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        proj_dir_name = proj_dir.name

        for fn in os.listdir(proj_dir):
            if not fn.endswith(".jsonl"):
                continue
            fp = proj_dir / fn

            # Skip subagent files (they live in subdirs, but check anyway)
            if "subagents" in str(fp):
                continue

            try:
                session_data = parse_session_fast(fp, proj_dir_name)
                if session_data:
                    sessions.append(session_data)
            except Exception as e:
                # Silently skip malformed files
                continue

    return sessions


def parse_session_fast(filepath, proj_dir_name):
    """Parse a single JSONL session file and extract stats.

    This is a fast extraction that reads the file once and collects
    everything we need without using extract_conversation (which is
    slower and doesn't capture all fields we want).
    """
    session_id = None
    slug = None
    model = None
    models_used = set()
    cwd = None
    cwds_seen = set()
    git_branches = set()
    version = None
    start_time = None
    end_time = None

    message_count = 0
    user_message_count = 0
    assistant_message_count = 0
    total_input_tokens = 0
    total_cache_read_tokens = 0
    total_cache_creation_tokens = 0
    total_output_tokens = 0
    errors = 0
    rejections = 0
    tool_usage = Counter()

    timestamps = []

    with open(filepath) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            msg_type = obj.get("type")
            timestamp = obj.get("timestamp")

            if msg_type in ("progress", "file-history-snapshot", "queue-operation"):
                continue

            # Capture metadata
            if not session_id and obj.get("sessionId"):
                session_id = obj["sessionId"]
            if not slug and obj.get("slug"):
                slug = obj["slug"]
            if not version and obj.get("version"):
                version = obj["version"]
            if obj.get("cwd"):
                if not cwd:
                    cwd = obj["cwd"]
                cwds_seen.add(obj["cwd"])
            if obj.get("gitBranch"):
                git_branches.add(obj["gitBranch"])

            # Track timestamps
            if timestamp:
                timestamps.append(timestamp)
                if not start_time or timestamp < start_time:
                    start_time = timestamp
                if not end_time or timestamp > end_time:
                    end_time = timestamp

            if msg_type == "assistant":
                assistant_message_count += 1
                message_count += 1
                msg = obj.get("message", {})

                m = msg.get("model")
                if m and m != "<synthetic>":
                    model = m
                    models_used.add(m)

                usage = msg.get("usage", {})
                total_input_tokens += usage.get("input_tokens", 0)
                total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)

                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_usage[block.get("name", "unknown")] += 1

            elif msg_type == "user":
                user_message_count += 1
                message_count += 1
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("is_error"):
                                errors += 1
                                c = block.get("content", "")
                                if isinstance(c, str) and any(
                                    pat in c.lower() for pat in REJECTION_PATTERNS
                                ):
                                    rejections += 1

            elif msg_type == "system":
                message_count += 1

    if message_count == 0:
        return None

    # Calculate duration
    duration_min = None
    if start_time and end_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_min = round((end_dt - start_dt).total_seconds() / 60, 1)
        except Exception:
            pass

    # Parse start timestamp for time-of-day analysis
    start_dt = None
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except Exception:
            pass

    project_name = derive_project_name(proj_dir_name, cwd)

    return {
        "file": str(filepath),
        "project_dir": proj_dir_name,
        "project_name": project_name,
        "session_id": session_id,
        "slug": slug,
        "model": model,
        "models_used": models_used,
        "cwd": cwd,
        "cwds_seen": cwds_seen,
        "git_branches": git_branches,
        "version": version,
        "start_time": start_time,
        "end_time": end_time,
        "start_dt": start_dt,
        "duration_min": duration_min,
        "message_count": message_count,
        "user_messages": user_message_count,
        "assistant_messages": assistant_message_count,
        "input_tokens": total_input_tokens,
        "cache_read_tokens": total_cache_read_tokens,
        "cache_creation_tokens": total_cache_creation_tokens,
        "output_tokens": total_output_tokens,
        "errors": errors,
        "rejections": rejections,
        "tool_usage": dict(tool_usage),
    }


def aggregate_per_project(sessions):
    """Aggregate session stats by project name."""
    projects = defaultdict(lambda: {
        "sessions": 0,
        "messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "output_tokens": 0,
        "duration_min": 0,
        "errors": 0,
        "rejections": 0,
        "tool_usage": Counter(),
        "models": Counter(),
        "git_branches": Counter(),
        "first_date": None,
        "last_date": None,
        "cwds": set(),
    })

    for s in sessions:
        name = s["project_name"]
        p = projects[name]
        p["sessions"] += 1
        p["messages"] += s["message_count"]
        p["user_messages"] += s["user_messages"]
        p["assistant_messages"] += s["assistant_messages"]
        p["input_tokens"] += s["input_tokens"]
        p["cache_read_tokens"] += s["cache_read_tokens"]
        p["cache_creation_tokens"] += s["cache_creation_tokens"]
        p["output_tokens"] += s["output_tokens"]
        if s["duration_min"]:
            p["duration_min"] += s["duration_min"]
        p["errors"] += s["errors"]
        p["rejections"] += s["rejections"]
        for tool, count in s["tool_usage"].items():
            p["tool_usage"][tool] += count
        if s["model"]:
            p["models"][s["model"]] += 1
        for b in s["git_branches"]:
            p["git_branches"][b] += 1
        for cwd in s["cwds_seen"]:
            p["cwds"].add(cwd)

        date_str = (s["start_time"] or "")[:10]
        if date_str:
            if not p["first_date"] or date_str < p["first_date"]:
                p["first_date"] = date_str
            if not p["last_date"] or date_str > p["last_date"]:
                p["last_date"] = date_str

    return dict(projects)


def git_branch_analysis(sessions):
    """Analyze git branch usage across all sessions."""
    branch_sessions = Counter()
    branch_projects = defaultdict(set)

    for s in sessions:
        for b in s["git_branches"]:
            if b and b != "HEAD":
                branch_sessions[b] += 1
                branch_projects[b].add(s["project_name"])

    return branch_sessions, branch_projects


def time_of_day_analysis(sessions):
    """Analyze session start times by hour and day of week."""
    hours = Counter()
    days = Counter()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for s in sessions:
        dt = s.get("start_dt")
        if dt:
            hours[dt.hour] += 1
            days[dt.weekday()] += 1

    return hours, days, day_names


def session_size_distribution(sessions):
    """Create histograms of session durations and message counts."""
    duration_buckets = {
        "<10min": 0,
        "10-30min": 0,
        "30-60min": 0,
        "1-2hr": 0,
        "2-4hr": 0,
        "4-8hr": 0,
        "8hr+": 0,
    }
    duration_bucket_order = list(duration_buckets.keys())

    message_buckets = {
        "1-10": 0,
        "11-50": 0,
        "51-100": 0,
        "101-200": 0,
        "201-500": 0,
        "501-1000": 0,
        "1000+": 0,
    }
    message_bucket_order = list(message_buckets.keys())

    no_duration = 0

    for s in sessions:
        dur = s["duration_min"]
        if dur is not None and dur > 0:
            if dur < 10:
                duration_buckets["<10min"] += 1
            elif dur < 30:
                duration_buckets["10-30min"] += 1
            elif dur < 60:
                duration_buckets["30-60min"] += 1
            elif dur < 120:
                duration_buckets["1-2hr"] += 1
            elif dur < 240:
                duration_buckets["2-4hr"] += 1
            elif dur < 480:
                duration_buckets["4-8hr"] += 1
            else:
                duration_buckets["8hr+"] += 1
        else:
            no_duration += 1

        mc = s["message_count"]
        if mc <= 10:
            message_buckets["1-10"] += 1
        elif mc <= 50:
            message_buckets["11-50"] += 1
        elif mc <= 100:
            message_buckets["51-100"] += 1
        elif mc <= 200:
            message_buckets["101-200"] += 1
        elif mc <= 500:
            message_buckets["201-500"] += 1
        elif mc <= 1000:
            message_buckets["501-1000"] += 1
        else:
            message_buckets["1000+"] += 1

    return duration_buckets, duration_bucket_order, message_buckets, message_bucket_order, no_duration


def model_usage_over_time(sessions):
    """Track which models were used on which dates."""
    date_models = defaultdict(Counter)
    for s in sessions:
        date = (s["start_time"] or "")[:10]
        if date and s["model"]:
            date_models[date][s["model"]] += 1
    return dict(date_models)


def estimate_costs(sessions):
    """Estimate total costs based on token usage and model pricing.

    Cache read tokens are priced at 10% of base input price.
    Cache creation tokens are priced at 125% of base input price.
    Base input tokens are at full input price.
    """
    costs = defaultdict(lambda: {
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "output_tokens": 0,
        "input_cost": 0.0,
        "output_cost": 0.0,
    })

    for s in sessions:
        tier = classify_model(s["model"])
        costs[tier]["input_tokens"] += s["input_tokens"]
        costs[tier]["cache_read_tokens"] += s["cache_read_tokens"]
        costs[tier]["cache_creation_tokens"] += s["cache_creation_tokens"]
        costs[tier]["output_tokens"] += s["output_tokens"]

    total_cost = 0.0
    for tier, data in costs.items():
        pricing = PRICING.get(tier, PRICING["sonnet"])
        base_input_cost = data["input_tokens"] / 1_000_000 * pricing["input"]
        cache_read_cost = data["cache_read_tokens"] / 1_000_000 * pricing["input"] * 0.1
        cache_creation_cost = data["cache_creation_tokens"] / 1_000_000 * pricing["input"] * 1.25
        data["input_cost"] = base_input_cost + cache_read_cost + cache_creation_cost
        data["output_cost"] = data["output_tokens"] / 1_000_000 * pricing["output"]
        data["total_cost"] = data["input_cost"] + data["output_cost"]
        data["all_input_tokens"] = data["input_tokens"] + data["cache_read_tokens"] + data["cache_creation_tokens"]
        total_cost += data["total_cost"]

    return dict(costs), total_cost


def fmt_tokens(n):
    """Format token count with K/M suffix."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_bar(count, max_count, width=30):
    """Create a simple text bar for histograms."""
    if max_count == 0:
        return ""
    bar_len = int(count / max_count * width)
    return "#" * bar_len


def generate_report(sessions):
    """Generate the full markdown report."""
    lines = []
    lines.append("# Claude Code Project Usage Statistics")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Total sessions analyzed: {len(sessions)}")
    lines.append("")

    # ── Overall Summary ──
    total_msgs = sum(s["message_count"] for s in sessions)
    total_base_in = sum(s["input_tokens"] for s in sessions)
    total_cache_read = sum(s["cache_read_tokens"] for s in sessions)
    total_cache_create = sum(s["cache_creation_tokens"] for s in sessions)
    total_in = total_base_in + total_cache_read + total_cache_create
    total_out = sum(s["output_tokens"] for s in sessions)
    total_dur = sum(s["duration_min"] or 0 for s in sessions)
    total_errors = sum(s["errors"] for s in sessions)
    total_rejections = sum(s["rejections"] for s in sessions)

    lines.append("## Overall Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total sessions | {len(sessions)} |")
    lines.append(f"| Total messages | {total_msgs:,} |")
    lines.append(f"| Total input tokens (all) | {fmt_tokens(total_in)} ({total_in:,}) |")
    lines.append(f"| -- base input | {fmt_tokens(total_base_in)} ({total_base_in:,}) |")
    lines.append(f"| -- cache read | {fmt_tokens(total_cache_read)} ({total_cache_read:,}) |")
    lines.append(f"| -- cache creation | {fmt_tokens(total_cache_create)} ({total_cache_create:,}) |")
    lines.append(f"| Total output tokens | {fmt_tokens(total_out)} ({total_out:,}) |")
    lines.append(f"| Total duration | {total_dur:.0f} min ({total_dur/60:.1f} hrs) |")
    lines.append(f"| Total errors | {total_errors:,} |")
    lines.append(f"| Total rejections | {total_rejections:,} |")
    lines.append(f"| Avg session duration | {total_dur/len(sessions):.1f} min |")
    lines.append(f"| Avg messages/session | {total_msgs/len(sessions):.1f} |")
    lines.append("")

    # ── 1. Per-Project Stats ──
    lines.append("## 1. Per-Project Stats")
    lines.append("")
    projects = aggregate_per_project(sessions)

    # Sort by total sessions descending
    sorted_projects = sorted(projects.items(), key=lambda x: x[1]["sessions"], reverse=True)

    lines.append("| Project | Sessions | Messages | Input Tokens | Output Tokens | Duration (min) | Errors | Rejections | Date Range |")
    lines.append("|---------|----------|----------|-------------|--------------|----------------|--------|------------|------------|")
    for name, p in sorted_projects:
        date_range = ""
        if p["first_date"] and p["last_date"]:
            if p["first_date"] == p["last_date"]:
                date_range = p["first_date"]
            else:
                date_range = f"{p['first_date']} to {p['last_date']}"
        all_input = p["input_tokens"] + p["cache_read_tokens"] + p["cache_creation_tokens"]
        lines.append(
            f"| {name} | {p['sessions']} | {p['messages']:,} | {fmt_tokens(all_input)} | "
            f"{fmt_tokens(p['output_tokens'])} | {p['duration_min']:.0f} | "
            f"{p['errors']} | {p['rejections']} | {date_range} |"
        )
    lines.append("")

    # Top tools per project (for top 10 projects by sessions)
    lines.append("### Most Used Tools (Top Projects)")
    lines.append("")
    for name, p in sorted_projects[:10]:
        if not p["tool_usage"]:
            continue
        top_tools = p["tool_usage"].most_common(5)
        tools_str = ", ".join(f"{t}({c})" for t, c in top_tools)
        lines.append(f"- **{name}**: {tools_str}")
    lines.append("")

    # Git branches per project
    lines.append("### Git Branches per Project")
    lines.append("")
    for name, p in sorted_projects[:15]:
        branches = {b for b in p["git_branches"] if b != "HEAD"}
        if branches:
            lines.append(f"- **{name}**: {', '.join(sorted(branches))}")
    lines.append("")

    # ── 2. Git Branch Analysis ──
    lines.append("## 2. Git Branch Analysis")
    lines.append("")
    branch_sessions, branch_projects = git_branch_analysis(sessions)

    if branch_sessions:
        lines.append("| Branch | Sessions | Projects |")
        lines.append("|--------|----------|----------|")
        for branch, count in branch_sessions.most_common(30):
            proj_list = ", ".join(sorted(branch_projects[branch]))
            lines.append(f"| `{branch}` | {count} | {proj_list} |")
    else:
        lines.append("No non-HEAD git branches found across sessions.")
    lines.append("")

    # ── 3. Time-of-Day Analysis ──
    lines.append("## 3. Time-of-Day Analysis")
    lines.append("")

    hours, days, day_names = time_of_day_analysis(sessions)

    lines.append("### Sessions by Hour of Day (UTC)")
    lines.append("")
    lines.append("| Hour | Sessions | |")
    lines.append("|------|----------|-|")
    max_hour = max(hours.values()) if hours else 1
    for h in range(24):
        count = hours.get(h, 0)
        bar = fmt_bar(count, max_hour, 25)
        lines.append(f"| {h:02d}:00 | {count:>4} | `{bar}` |")
    lines.append("")

    lines.append("### Sessions by Day of Week")
    lines.append("")
    lines.append("| Day | Sessions | |")
    lines.append("|-----|----------|-|")
    max_day = max(days.values()) if days else 1
    for d in range(7):
        count = days.get(d, 0)
        bar = fmt_bar(count, max_day, 25)
        lines.append(f"| {day_names[d]} | {count:>4} | `{bar}` |")
    lines.append("")

    # ── 4. Session Size Distribution ──
    lines.append("## 4. Session Size Distribution")
    lines.append("")

    dur_buckets, dur_order, msg_buckets, msg_order, no_dur = session_size_distribution(sessions)

    lines.append("### Duration Distribution")
    lines.append("")
    lines.append("| Duration | Sessions | |")
    lines.append("|----------|----------|-|")
    max_dur_count = max(dur_buckets.values()) if dur_buckets else 1
    for bucket in dur_order:
        count = dur_buckets[bucket]
        bar = fmt_bar(count, max_dur_count, 30)
        lines.append(f"| {bucket} | {count:>4} | `{bar}` |")
    if no_dur:
        lines.append(f"| (no duration data) | {no_dur:>4} | |")
    lines.append("")

    lines.append("### Message Count Distribution")
    lines.append("")
    lines.append("| Messages | Sessions | |")
    lines.append("|----------|----------|-|")
    max_msg_count = max(msg_buckets.values()) if msg_buckets else 1
    for bucket in msg_order:
        count = msg_buckets[bucket]
        bar = fmt_bar(count, max_msg_count, 30)
        lines.append(f"| {bucket} | {count:>4} | `{bar}` |")
    lines.append("")

    # ── 5. Model Usage Over Time ──
    lines.append("## 5. Model Usage Over Time")
    lines.append("")

    date_models = model_usage_over_time(sessions)

    # Collect all unique models
    all_models = set()
    for dm in date_models.values():
        all_models.update(dm.keys())
    all_models = sorted(all_models)

    if date_models:
        # Show by week to keep the table manageable
        week_models = defaultdict(Counter)
        for date, models in date_models.items():
            # ISO week
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                week = dt.strftime("%Y-W%V")
                for m, c in models.items():
                    week_models[week][m] += c
            except Exception:
                pass

        lines.append("| Week | " + " | ".join(all_models) + " | Total |")
        lines.append("|------" + "|---" * len(all_models) + "|-------|")
        for week in sorted(week_models.keys()):
            counts = week_models[week]
            total = sum(counts.values())
            row = f"| {week} "
            for m in all_models:
                row += f"| {counts.get(m, 0)} "
            row += f"| {total} |"
            lines.append(row)
    else:
        lines.append("No model data available.")
    lines.append("")

    # Also show overall model counts
    model_totals = Counter()
    for s in sessions:
        if s["model"]:
            model_totals[s["model"]] += 1

    lines.append("### Overall Model Usage")
    lines.append("")
    lines.append("| Model | Sessions | % |")
    lines.append("|-------|----------|---|")
    for m, c in model_totals.most_common():
        pct = c / len(sessions) * 100
        lines.append(f"| {m} | {c} | {pct:.1f}% |")
    lines.append("")

    # ── 6. Token Cost Estimation ──
    lines.append("## 6. Token Cost Estimation")
    lines.append("")

    costs, total_cost = estimate_costs(sessions)

    lines.append("| Tier | Base Input | Cache Read | Cache Create | Output | Input Cost | Output Cost | Total Cost |")
    lines.append("|------|-----------|------------|-------------|--------|-----------|------------|-----------|")
    for tier in sorted(costs.keys()):
        data = costs[tier]
        lines.append(
            f"| {tier.title()} | {fmt_tokens(data['input_tokens'])} | "
            f"{fmt_tokens(data['cache_read_tokens'])} | "
            f"{fmt_tokens(data['cache_creation_tokens'])} | "
            f"{fmt_tokens(data['output_tokens'])} | "
            f"${data['input_cost']:.2f} | ${data['output_cost']:.2f} | "
            f"${data['total_cost']:.2f} |"
        )
    lines.append(f"| **Total** | | | | | | | **${total_cost:.2f}** |")
    lines.append("")

    lines.append("*Note: Cost estimates are rough. Actual billing depends on caching, "
                 "batch vs. interactive pricing, and subscription plans (e.g. Claude Max). "
                 "Cache read/creation tokens are counted as input tokens here.*")
    lines.append("")

    # ── 7. Sessions by Date ──
    lines.append("## 7. Sessions by Date (Last 30 Days)")
    lines.append("")
    sessions_by_date = Counter()
    for s in sessions:
        date = (s["start_time"] or "")[:10]
        if date:
            sessions_by_date[date] += 1

    recent_dates = sorted(sessions_by_date.keys(), reverse=True)[:30]
    if recent_dates:
        max_date_count = max(sessions_by_date[d] for d in recent_dates)
        lines.append("| Date | Sessions | |")
        lines.append("|------|----------|-|")
        for date in recent_dates:
            count = sessions_by_date[date]
            bar = fmt_bar(count, max_date_count, 30)
            lines.append(f"| {date} | {count:>4} | `{bar}` |")
    lines.append("")

    # ── 8. Top Error Sessions ──
    lines.append("## 8. Top Error Sessions")
    lines.append("")
    error_sessions = sorted(sessions, key=lambda s: s["errors"], reverse=True)[:15]
    lines.append("| Slug | Project | Errors | Rejections | Messages | Duration |")
    lines.append("|------|---------|--------|------------|----------|----------|")
    for s in error_sessions:
        if s["errors"] == 0:
            break
        dur_str = f"{s['duration_min']:.0f} min" if s["duration_min"] else "?"
        slug = s["slug"] or s["session_id"] or "unknown"
        lines.append(
            f"| {slug} | {s['project_name']} | {s['errors']} | "
            f"{s['rejections']} | {s['message_count']} | {dur_str} |"
        )
    lines.append("")

    return "\n".join(lines)


def main():
    print("Collecting sessions from all project directories...")
    sessions = collect_all_sessions()
    print(f"Found {len(sessions)} sessions across {len(set(s['project_dir'] for s in sessions))} project directories.")

    if not sessions:
        print("No sessions found.")
        return

    print("Generating report...")
    report = generate_report(sessions)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(report)

    print(f"Report saved to: {OUTPUT_FILE}")

    # Print a brief summary to stdout
    total_msgs = sum(s["message_count"] for s in sessions)
    total_in = sum(s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"] for s in sessions)
    total_out = sum(s["output_tokens"] for s in sessions)
    _, total_cost = estimate_costs(sessions)
    print(f"\nSummary: {len(sessions)} sessions, {total_msgs:,} messages, "
          f"{fmt_tokens(total_in)} input / {fmt_tokens(total_out)} output tokens, "
          f"est. cost ${total_cost:.2f}")


if __name__ == "__main__":
    main()
