#!/usr/bin/env python3
"""
Claude Recap - Operational stats dashboard for Claude Code.

Reads JSONL session transcripts, computes aggregate metrics, and renders
a terminal dashboard or exports to HTML/JSON.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patterns.config import (
    CLAUDE_PROJECTS_DIRS,
    CLAUDE_PROJECTS_DIR,
    resolve_project_name,
    REJECTION_PATTERNS,
)
from patterns.project_stats import (
    estimate_costs,
    classify_model,
    PRICING,
)
from patterns.error_taxonomy import classify_error

VERSION = "2.0.8"

# ---------------------------------------------------------------------------
# 1. Formatting utilities
# ---------------------------------------------------------------------------

# ANSI color scheme - readable on both light and dark terminals
# Respects NO_COLOR (https://no-color.org/) and pipe/redirect detection
def _use_color():
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not sys.stdout.isatty():
        return False
    return True

_COLOR = _use_color()

# Color codes - #da7756 orange is Claude Code's accent color
B    = "\033[1m"              if _COLOR else ""   # bold
R    = "\033[0m"              if _COLOR else ""   # reset
DIM  = "\033[2m"              if _COLOR else ""   # dim/faint
HEAD = "\033[1;38;5;173m"     if _COLOR else ""   # bold orange (section headers, #da7756)
VAL  = "\033[1m"              if _COLOR else ""   # bold (key values)
NUM  = "\033[1;38;5;173m"     if _COLOR else ""   # bold orange (hero numbers)
POS  = "\033[32m"             if _COLOR else ""   # green (positive delta)
NEG  = "\033[31m"             if _COLOR else ""   # red (negative delta)
ERR  = "\033[31m"             if _COLOR else ""   # red (error counts)
BAR_C = "\033[38;5;173m"      if _COLOR else ""   # orange (progress bars)
SPARK_C = "\033[38;5;173m"    if _COLOR else ""   # orange (sparklines)


def bar(value, maximum, width=20):
    """Render a progress bar: ████████░░░░░░░░░░░░"""
    if maximum <= 0:
        return f"{DIM}{'░' * width}{R}"
    filled = int(value / maximum * width)
    filled = min(filled, width)
    return f"{BAR_C}{'█' * filled}{DIM}{'░' * (width - filled)}{R}"


def sparkline(values):
    """Render a sparkline from a list of numbers: ▁▂▃▅▇█▅▃"""
    if not values:
        return ""
    chars = "▁▁▂▃▄▅▆▇█"
    mn = min(values)
    mx = max(values)
    rng = mx - mn if mx != mn else 1
    raw = "".join(chars[min(int((v - mn) / rng * 8), 8)] for v in values)
    return f"{SPARK_C}{raw}{R}"


def delta_str(cur, prev):
    """Format a delta: +12.3% ▲ or -5.1% ▼ with color"""
    if prev == 0:
        return ""
    pct = (cur - prev) / prev * 100
    sign = "+" if pct > 0 else ""
    arrow = "▲" if pct > 0 else "▼"
    color = POS if pct > 0 else NEG
    return f"{color}{sign}{pct:.1f}% {arrow}{R}"


def fmt_tokens(n):
    """Format token count with B/M/K suffix."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(n):
    """Format dollar amount."""
    return f"${n:,.2f}"


def fmt_hours(minutes):
    """Format minutes as hours string."""
    return f"{minutes / 60:.1f}h"


# ---------------------------------------------------------------------------
# 2. Data collection
# ---------------------------------------------------------------------------

def find_all_sessions(days_filter=None, project_filter=None, min_size=500):
    """Find all JSONL sessions, optionally filtered by days and project."""
    cutoff_dt = None
    if days_filter:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_filter)

    seen = set()
    sessions = []

    for projects_dir in CLAUDE_PROJECTS_DIRS:
        if not projects_dir.exists():
            continue
        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            proj_dir_name = proj_dir.name
            for fn in os.listdir(proj_dir):
                if not fn.endswith(".jsonl"):
                    continue
                fp = proj_dir / fn
                if "subagents" in str(fp):
                    continue
                try:
                    resolved = fp.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    stat = fp.stat()
                    if stat.st_size < min_size:
                        continue
                    # Pre-filter by mtime if days_filter is set
                    if cutoff_dt:
                        file_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        if file_dt < cutoff_dt:
                            continue
                    sessions.append((stat.st_mtime, stat.st_size, fp, proj_dir_name))
                except OSError:
                    continue

    sessions.sort(reverse=True)
    return sessions


def _parse_session_with_errors(filepath, proj_dir_name):
    """Parse a JSONL session in a single fast pass.

    Uses string pre-checks to skip irrelevant lines before JSON parsing.
    Most lines in large sessions are tool results (content blocks) that
    don't contain metadata we need. We only JSON-parse lines containing
    key markers.
    """
    from patterns.project_stats import derive_project_name

    session_id = slug = model = cwd = version = start_time = end_time = None
    cwds_seen = set()
    git_branches = set()
    message_count = user_message_count = assistant_message_count = 0
    total_input_tokens = total_cache_read_tokens = total_cache_creation_tokens = total_output_tokens = 0
    errors = rejections = 0
    tool_usage = Counter()
    error_categories = Counter()
    timestamps = []  # for active duration calculation

    # Pre-compiled rejection patterns for speed
    rej_pats = REJECTION_PATTERNS

    with open(filepath, errors="replace") as f:
        for line in f:
            # Quick string checks to skip lines we don't care about
            # Every JSONL line has "type", so check for the types we need
            if '"progress"' in line or '"file-history-snapshot"' in line or '"queue-operation"' in line:
                continue

            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            msg_type = obj.get("type")
            timestamp = obj.get("timestamp")

            # Capture metadata from first few lines
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

            if timestamp:
                if not start_time or timestamp < start_time:
                    start_time = timestamp
                if not end_time or timestamp > end_time:
                    end_time = timestamp
                timestamps.append(timestamp)

            if msg_type == "assistant":
                assistant_message_count += 1
                message_count += 1
                msg = obj.get("message", {})
                m = msg.get("model")
                if m and m != "<synthetic>":
                    model = m
                usage = msg.get("usage", {})
                total_input_tokens += usage.get("input_tokens", 0)
                total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)
                # Count tool uses (skip iterating content for huge blocks)
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_usage[block.get("name", "unknown")] += 1

            elif msg_type == "user":
                user_message_count += 1
                message_count += 1
                content = obj.get("message", {}).get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("is_error"):
                            errors += 1
                            c = block.get("content", "")
                            if isinstance(c, list):
                                c = " ".join(str(t) for t in c)
                            if isinstance(c, str):
                                if any(pat in c.lower() for pat in rej_pats):
                                    rejections += 1
                                if c:
                                    error_categories[classify_error(c)] += 1

            elif msg_type == "system":
                message_count += 1

    if message_count == 0:
        return None, Counter()

    duration_min = None
    start_dt = None

    # Compute active duration: sum of inter-message gaps under 30 minutes.
    # This excludes idle time when sessions are left open overnight.
    IDLE_THRESHOLD_SEC = 30 * 60  # 30 minutes
    if len(timestamps) >= 2:
        try:
            sorted_ts = sorted(timestamps)
            dts = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in sorted_ts]
            start_dt = dts[0]
            active_sec = 0.0
            for i in range(1, len(dts)):
                gap = (dts[i] - dts[i - 1]).total_seconds()
                if gap < IDLE_THRESHOLD_SEC:
                    active_sec += gap
            duration_min = round(active_sec / 60, 1)
        except Exception:
            pass
    elif start_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except Exception:
            pass

    project_name = derive_project_name(proj_dir_name, cwd)

    data = {
        "file": str(filepath),
        "project_dir": proj_dir_name,
        "project_name": project_name,
        "session_id": session_id,
        "slug": slug,
        "model": model,
        "models_used": set(),
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
    return data, error_categories


def _progress_bar(current, total, width=24):
    """Render a simple progress bar for stderr."""
    if total <= 0:
        return ""
    pct = min(current / total, 1.0)
    filled = int(pct * width)
    bar_str = "█" * filled + "░" * (width - filled)
    return f"{bar_str} {int(pct * 100)}%"


def collect_stats(session_list, days_filter=None, project_filter=None, quiet=False):
    """Parse all sessions and collect stats in a single pass per file."""
    parsed = []
    all_error_categories = Counter()
    cutoff_dt = None
    if days_filter:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_filter)

    total = len(session_list)
    for i, (mtime, size, fp, proj_dir_name) in enumerate(session_list):
        if not quiet and (i + 1) % max(total // 20, 1) == 0:
            sys.stderr.write(f"\r  Processing {_progress_bar(i + 1, total)}  {i+1}/{total} sessions")
            sys.stderr.flush()

        try:
            data, err_cats = _parse_session_with_errors(fp, proj_dir_name)
        except Exception:
            continue
        if not data:
            continue

        # Apply days filter
        if cutoff_dt and data.get("start_dt"):
            if data["start_dt"] < cutoff_dt:
                continue

        # Apply project filter
        if project_filter:
            if project_filter.lower() not in data["project_name"].lower():
                continue

        parsed.append(data)
        all_error_categories += err_cats

    if not quiet:
        sys.stderr.write("\r" + " " * 70 + "\r")
        sys.stderr.flush()

    return {
        "sessions": parsed,
        "error_categories": dict(all_error_categories),
    }


# ---------------------------------------------------------------------------
# 3. Aggregation
# ---------------------------------------------------------------------------

def compute_overview(sessions):
    total_sessions = len(sessions)
    total_messages = sum(s["message_count"] for s in sessions)
    total_duration = sum(s["duration_min"] or 0 for s in sessions)
    total_hours = total_duration / 60

    dates = set()
    for s in sessions:
        d = (s.get("start_time") or "")[:10]
        if d:
            dates.add(d)

    projects = set(s["project_name"] for s in sessions)
    active_days = len(dates)
    all_dates = sorted(dates)
    if all_dates:
        first = datetime.strptime(all_dates[0], "%Y-%m-%d")
        last = datetime.strptime(all_dates[-1], "%Y-%m-%d")
        total_days = (last - first).days + 1
    else:
        total_days = 0

    avg_session_min = total_duration / total_sessions if total_sessions else 0

    return {
        "sessions": total_sessions,
        "messages": total_messages,
        "hours": round(total_hours, 1),
        "projects": len(projects),
        "active_days": active_days,
        "total_days": total_days,
        "avg_session_min": round(avg_session_min),
        "first_date": all_dates[0] if all_dates else None,
        "last_date": all_dates[-1] if all_dates else None,
    }


def compute_cost(sessions, plan_cost=200):
    # Per-session costs
    session_costs = []
    for s in sessions:
        _, cost = estimate_costs([s])
        date = (s.get("start_time") or "")[:10]
        session_costs.append({
            "cost": cost,
            "date": date,
            "project": s["project_name"],
            "file": s.get("file", ""),
        })

    total_cost = sum(sc["cost"] for sc in session_costs)
    total_sessions = len(sessions)
    total_hours = sum(s["duration_min"] or 0 for s in sessions) / 60

    # Most expensive session
    most_expensive = max(session_costs, key=lambda x: x["cost"]) if session_costs else None

    # Daily spend
    daily = defaultdict(float)
    for sc in session_costs:
        if sc["date"]:
            daily[sc["date"]] += sc["cost"]

    daily_sorted = sorted(daily.items())
    daily_values = [v for _, v in daily_sorted]
    daily_avg = sum(daily_values) / len(daily_values) if daily_values else 0

    # ROI
    roi = total_cost / plan_cost if plan_cost > 0 else 0

    return {
        "api_value": round(total_cost, 2),
        "plan_cost": plan_cost,
        "roi": round(roi, 1),
        "per_session": round(total_cost / total_sessions, 2) if total_sessions else 0,
        "per_hour": round(total_cost / total_hours, 2) if total_hours > 0 else 0,
        "most_expensive": most_expensive,
        "daily": daily_sorted,
        "daily_values": daily_values,
        "daily_avg": round(daily_avg, 2),
        "session_costs": session_costs,
    }


def compute_models(sessions):
    model_data = defaultdict(lambda: {"sessions": 0, "cost": 0.0, "tokens": 0})

    for s in sessions:
        model_name = s.get("model") or "unknown"
        # Normalize model name for display
        display_name = model_name
        tier = classify_model(model_name)

        model_data[display_name]["sessions"] += 1
        _, cost = estimate_costs([s])
        model_data[display_name]["cost"] += cost
        total_tokens = (s["input_tokens"] + s["cache_read_tokens"] +
                        s["cache_creation_tokens"] + s["output_tokens"])
        model_data[display_name]["tokens"] += total_tokens

    total_cost = sum(m["cost"] for m in model_data.values())

    result = []
    for name, data in sorted(model_data.items(), key=lambda x: -x[1]["cost"]):
        pct = (data["cost"] / total_cost * 100) if total_cost > 0 else 0
        result.append({
            "model": name,
            "sessions": data["sessions"],
            "cost": round(data["cost"], 2),
            "pct": round(pct, 1),
        })

    return result


def compute_tokens(sessions):
    total_input = sum(s["input_tokens"] for s in sessions)
    total_output = sum(s["output_tokens"] for s in sessions)
    total_cache_read = sum(s["cache_read_tokens"] for s in sessions)
    total_cache_create = sum(s["cache_creation_tokens"] for s in sessions)
    total_all = total_input + total_output + total_cache_read + total_cache_create

    # Cache hit rate: cache_read / (cache_read + cache_creation + input)
    cache_eligible = total_cache_read + total_cache_create + total_input
    cache_hit_rate = (total_cache_read / cache_eligible * 100) if cache_eligible > 0 else 0

    # Cache savings estimate: what it would have cost at full input price
    savings = 0.0
    for s in sessions:
        tier = classify_model(s.get("model"))
        pricing = PRICING.get(tier, PRICING["sonnet"])
        # Cache read saves 90% of input price
        savings += s["cache_read_tokens"] / 1_000_000 * pricing["input"] * 0.9

    per_session = total_all / len(sessions) if sessions else 0

    return {
        "total": total_all,
        "input": total_input,
        "output": total_output,
        "cache_read": total_cache_read,
        "cache_creation": total_cache_create,
        "cache_hit_rate": round(cache_hit_rate, 1),
        "cache_savings": round(savings, 2),
        "per_session": int(per_session),
    }


def compute_projects(sessions):
    projects = defaultdict(lambda: {
        "sessions": 0, "cost": 0.0, "tokens": 0, "hours": 0.0,
    })

    for s in sessions:
        name = s["project_name"]
        projects[name]["sessions"] += 1
        _, cost = estimate_costs([s])
        projects[name]["cost"] += cost
        projects[name]["tokens"] += (s["input_tokens"] + s["cache_read_tokens"] +
                                     s["cache_creation_tokens"] + s["output_tokens"])
        projects[name]["hours"] += (s["duration_min"] or 0) / 60

    result = []
    for name, data in sorted(projects.items(), key=lambda x: -x[1]["cost"]):
        result.append({
            "name": name,
            "sessions": data["sessions"],
            "cost": round(data["cost"], 2),
            "tokens": data["tokens"],
            "hours": round(data["hours"], 1),
        })

    return result


def compute_activity(sessions):
    hours = Counter()
    days = Counter()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dates_active = set()

    for s in sessions:
        dt = s.get("start_dt")
        if dt:
            hours[dt.hour] += 1
            days[dt.weekday()] += 1
            dates_active.add(dt.strftime("%Y-%m-%d"))

    # Peak hour
    peak_hour = hours.most_common(1)[0][0] if hours else 0
    busiest_day_idx = days.most_common(1)[0][0] if days else 0
    busiest_day = day_names[busiest_day_idx]

    # Streak calculation
    sorted_dates = sorted(dates_active)
    streak = 0
    current_streak = 0
    for i, d in enumerate(sorted_dates):
        if i == 0:
            current_streak = 1
        else:
            prev = datetime.strptime(sorted_dates[i - 1], "%Y-%m-%d")
            curr = datetime.strptime(d, "%Y-%m-%d")
            if (curr - prev).days == 1:
                current_streak += 1
            else:
                current_streak = 1
        streak = max(streak, current_streak)

    # Duration distribution
    buckets = {"<10m": 0, "10-30m": 0, "30-60m": 0, "1-2h": 0, "2h+": 0}
    for s in sessions:
        dur = s["duration_min"]
        if dur is None or dur <= 0:
            buckets["<10m"] += 1
            continue
        if dur < 10:
            buckets["<10m"] += 1
        elif dur < 30:
            buckets["10-30m"] += 1
        elif dur < 60:
            buckets["30-60m"] += 1
        elif dur < 120:
            buckets["1-2h"] += 1
        else:
            buckets["2h+"] += 1

    return {
        "peak_hour": peak_hour,
        "busiest_day": busiest_day,
        "streak": streak,
        "active_days": len(dates_active),
        "total_days": 0,  # filled by overview
        "duration_buckets": buckets,
        "hours": dict(hours),
        "days": dict(days),
    }


def compute_errors(sessions, error_categories):
    session_error_total = sum(s["errors"] for s in sessions)
    total_messages = sum(s["message_count"] for s in sessions)
    error_rate = (session_error_total / total_messages * 100) if total_messages > 0 else 0
    per_session = session_error_total / len(sessions) if sessions else 0

    total_errors = sum(error_categories.values()) if error_categories else session_error_total

    # Sort by count descending
    sorted_cats = dict(sorted(error_categories.items(), key=lambda x: -x[1]))

    return {
        "total": total_errors,
        "rate": round(error_rate, 1),
        "per_session": round(per_session, 1),
        "by_type": sorted_cats,
    }


def compute_trend(sessions):
    now = datetime.now(timezone.utc)
    this_week_start = now - timedelta(days=7)
    last_week_start = now - timedelta(days=14)

    this_week = []
    last_week = []

    for s in sessions:
        dt = s.get("start_dt")
        if not dt:
            continue
        if dt >= this_week_start:
            this_week.append(s)
        elif dt >= last_week_start:
            last_week.append(s)

    def week_stats(week_sessions):
        if not week_sessions:
            return {"sessions": 0, "cost": 0, "tokens": 0, "cache_hit_rate": 0}
        _, cost = estimate_costs(week_sessions)
        tokens = sum(s["input_tokens"] + s["cache_read_tokens"] +
                     s["cache_creation_tokens"] + s["output_tokens"]
                     for s in week_sessions)
        cache_read = sum(s["cache_read_tokens"] for s in week_sessions)
        cache_create = sum(s["cache_creation_tokens"] for s in week_sessions)
        cache_eligible = cache_read + cache_create + sum(s["input_tokens"] for s in week_sessions)
        hit_rate = (cache_read / cache_eligible * 100) if cache_eligible > 0 else 0
        return {
            "sessions": len(week_sessions),
            "cost": round(cost, 2),
            "tokens": tokens,
            "cache_hit_rate": round(hit_rate, 1),
        }

    return {
        "this_week": week_stats(this_week),
        "last_week": week_stats(last_week),
    }


# ---------------------------------------------------------------------------
# 4. Terminal rendering
# ---------------------------------------------------------------------------

def _get_width():
    """Detect terminal width, fallback 80, clamp to 60-120."""
    try:
        w = os.get_terminal_size().columns
    except (OSError, ValueError):
        w = 80
    return max(60, min(w, 120))


W = _get_width()


def _fmt_period(overview):
    if overview["first_date"] and overview["last_date"]:
        try:
            fd = datetime.strptime(overview["first_date"], "%Y-%m-%d")
            ld = datetime.strptime(overview["last_date"], "%Y-%m-%d")
            return f"{fd.strftime('%b %-d')} - {ld.strftime('%b %-d')}"
        except Exception:
            return f"{overview['first_date']} - {overview['last_date']}"
    return ""


def _fmt_peak(hour):
    h1 = f"{((hour - 1) % 12) + 1}{'am' if hour < 12 else 'pm'}"
    h2_raw = (hour + 1) % 24
    h2 = f"{((h2_raw - 1) % 12) + 1}{'am' if h2_raw < 12 else 'pm'}"
    return f"{h1} - {h2}"


def _section(title):
    """Section header: blue bold title + dim separator."""
    return f"\n  {HEAD}{title.upper()}{R}\n  {DIM}{'─' * (W - 4)}{R}"


def render_dashboard(overview, cost, models, tokens, projects, activity, errors, trend, plan_cost, project_filter):
    lines = []
    w = lines.append

    period = _fmt_period(overview)
    total_days = overview["total_days"]
    plan_label = "Max" if plan_cost == 200 else "Max 5x" if plan_cost == 100 else "Pro"

    # ── Header ──────────────────────────────────────────────────
    w("")
    title = "Claude Recap"
    header_right = f"{period}  ·  {total_days} days"
    gap = W - 4 - len(title) - len(header_right)
    w(f"  {HEAD}{title}{R}{' ' * max(gap, 2)}{DIM}{header_right}{R}")
    w(f"  {DIM}{'━' * (W - 4)}{R}")

    # ── Overview ────────────────────────────────────────────────
    w(_section("Overview"))
    w("")
    w(f"    {DIM}Sessions{R}       {NUM}{overview['sessions']:>6,}{R}          {DIM}Active Days{R}    {VAL}{overview['active_days']}/{overview['total_days']}{R}")
    w(f"    {DIM}Hours{R}          {VAL}{overview['hours']:>6}{R}h         {DIM}Projects{R}       {VAL}{overview['projects']}{R}")
    w(f"    {DIM}Avg Session{R}    {VAL}{overview['avg_session_min']:>6}{R} min      {DIM}Messages{R}       {VAL}{overview['messages']:,}{R}")

    # ── Cost & ROI ──────────────────────────────────────────────
    w(_section("Cost & ROI"))
    w("")
    w(f"    {DIM}API Value{R}       {NUM}{fmt_cost(cost['api_value']):>12}{R}")
    w(f"    {DIM}Plan Cost{R}       {fmt_cost(plan_cost):>12}     {DIM}{plan_label}{R}")
    w(f"    {DIM}ROI{R}             {NUM}{cost['roi']:>11.1f}x{R}")
    w("")
    w(f"    {DIM}Cost / Session{R}  {fmt_cost(cost['per_session']):>12}")
    w(f"    {DIM}Cost / Hour{R}     {fmt_cost(cost['per_hour']):>12}     {DIM}vs ~$75/hr junior dev{R}")
    if cost["most_expensive"]:
        me = cost["most_expensive"]
        try:
            me_date = datetime.strptime(me['date'], "%Y-%m-%d").strftime("%b %-d")
        except Exception:
            me_date = me['date']
        me_proj = me['project']
        w(f"    {DIM}Most Expensive{R}  {VAL}{fmt_cost(me['cost']):>12}{R}     {DIM}{me_date} ({me_proj}){R}")
    if cost["daily_values"]:
        spark = sparkline(cost["daily_values"][-20:])
        w("")
        w(f"    {DIM}Daily{R}  {spark}  {DIM}avg {fmt_cost(cost['daily_avg'])}/day{R}")

    # ── Models ──────────────────────────────────────────────────
    w(_section("Models"))
    w("")
    col_w = max(len(m["model"]) for m in models) + 2 if models else 20
    col_w = min(col_w, 30)
    w(f"    {DIM}{'Model':<{col_w}} {'Sess':>5}  {'Cost':>10}  {'Share':>6}  {'':10}{R}")
    for m in models:
        name = m["model"]
        if len(name) > col_w - 2:
            name = name[:col_w - 4] + ".."
        pct_bar = bar(m["pct"], 100, 10)
        w(f"    {name:<{col_w}} {VAL}{m['sessions']:>5,}{R}  {fmt_cost(m['cost']):>10}  {m['pct']:>5.1f}%  {pct_bar}")

    # ── Tokens ──────────────────────────────────────────────────
    w(_section("Tokens"))
    w("")
    w(f"    {DIM}Total{R}        {NUM}{fmt_tokens(tokens['total']):>8}{R}          {DIM}Input{R}          {fmt_tokens(tokens['input'])}")
    w(f"    {DIM}Output{R}       {fmt_tokens(tokens['output']):>8}          {DIM}Cache Read{R}     {NUM}{fmt_tokens(tokens['cache_read'])}{R}")
    w(f"    {DIM}Cache Create{R} {fmt_tokens(tokens['cache_creation']):>8}")
    w("")
    cache_pct = tokens["cache_hit_rate"]
    cache_bar_str = bar(cache_pct, 100, 20)
    w(f"    {DIM}Cache Hit{R}    {NUM}{cache_pct:.1f}%{R}  {cache_bar_str}")
    w(f"    {DIM}Savings{R}      {NUM}~{fmt_cost(tokens['cache_savings'])}{R}      {DIM}{fmt_tokens(tokens['per_session'])} avg/session{R}")

    # ── Projects ────────────────────────────────────────────────
    if not project_filter:
        w(_section("Projects"))
        w("")

        shown = projects[:8]
        collapsed = projects[8:]
        name_w = 20

        w(f"    {DIM}{'Project':<{name_w}}  {'Sess':>5}  {'Cost':>10}  {'Tokens':>6}  {'Hours':>6}{R}")
        for p in shown:
            name = p["name"]
            if len(name) > name_w:
                name = name[:name_w - 2] + ".."
            w(f"    {name:<{name_w}}  {VAL}{p['sessions']:>5,}{R}  {fmt_cost(p['cost']):>10}  {fmt_tokens(p['tokens']):>6}  {p['hours']:>6.1f}h")

        if collapsed:
            c_sessions = sum(p["sessions"] for p in collapsed)
            c_cost = sum(p["cost"] for p in collapsed)
            c_tokens = sum(p["tokens"] for p in collapsed)
            c_hours = sum(p["hours"] for p in collapsed)
            label = f"({len(collapsed)} more)"
            w(f"    {DIM}{label:<{name_w}}{R}  {c_sessions:>5,}  {fmt_cost(c_cost):>10}  {fmt_tokens(c_tokens):>6}  {c_hours:>6.1f}h")

        t_sessions = sum(p["sessions"] for p in projects)
        t_cost = sum(p["cost"] for p in projects)
        t_tokens = sum(p["tokens"] for p in projects)
        t_hours = sum(p["hours"] for p in projects)
        w(f"    {DIM}{'─' * (W - 8)}{R}")
        w(f"    {VAL}{'Total':<{name_w}}{R}  {NUM}{t_sessions:>5,}{R}  {NUM}{fmt_cost(t_cost):>10}{R}  {NUM}{fmt_tokens(t_tokens):>6}{R}  {NUM}{t_hours:>6.1f}h{R}")

    # ── Activity ────────────────────────────────────────────────
    w(_section("Activity"))
    w("")
    w(f"    {DIM}Peak Hours{R}    {VAL}{_fmt_peak(activity['peak_hour'])}{R}")
    w(f"    {DIM}Busiest Day{R}   {VAL}{activity['busiest_day']}{R}")
    w(f"    {DIM}Streak{R}        {VAL}{activity['streak']} days{R}          {DIM}Active{R}  {activity['active_days']}/{overview['total_days']}")
    w("")
    buckets = activity["duration_buckets"]
    parts = "  ".join(f"{DIM}{k}{R} {VAL}{v}{R}" for k, v in buckets.items())
    w(f"    {parts}")

    # ── Errors ──────────────────────────────────────────────────
    if errors["total"] > 0:
        w(_section("Errors"))
        w("")
        w(f"    {DIM}Total{R} {ERR}{errors['total']:,}{R}    {DIM}Rate{R} {ERR}{errors['rate']}%{R}    {DIM}Per Session{R} {VAL}{errors['per_session']}{R} avg")
        w("")
        by_type = errors.get("by_type", {})
        if by_type:
            max_count = max(by_type.values()) if by_type else 1
            for cat, count in sorted(by_type.items(), key=lambda x: -x[1])[:5]:
                pct = (count / errors["total"] * 100) if errors["total"] > 0 else 0
                err_bar = bar(count, max_count, 15)
                w(f"    {cat:<18s} {ERR}{count:>5,}{R}  {DIM}{pct:>3.0f}%{R}  {err_bar}")

    # ── Trend ───────────────────────────────────────────────────
    tw = trend["this_week"]
    lw = trend["last_week"]
    if lw["sessions"] > 0:
        w(_section("Trend"))
        w(f"    {'':40s}{DIM}last week  this week{R}")
        w("")
        w(f"    {DIM}Sessions{R}    {lw['sessions']:>8}   {DIM}→{R}  {VAL}{tw['sessions']:>8}{R}    {delta_str(tw['sessions'], lw['sessions'])}")
        w(f"    {DIM}Cost{R}        {fmt_cost(lw['cost']):>8}   {DIM}→{R}  {VAL}{fmt_cost(tw['cost']):>8}{R}    {delta_str(tw['cost'], lw['cost'])}")
        w(f"    {DIM}Tokens{R}      {fmt_tokens(lw['tokens']):>8}   {DIM}→{R}  {VAL}{fmt_tokens(tw['tokens']):>8}{R}    {delta_str(tw['tokens'], lw['tokens'])}")
        w(f"    {DIM}Cache{R}       {lw['cache_hit_rate']:>7.1f}%   {DIM}→{R}  {VAL}{tw['cache_hit_rate']:>7.1f}%{R}    {delta_str(tw['cache_hit_rate'], lw['cache_hit_rate'])}")

    # ── Footer ──────────────────────────────────────────────────
    w("")
    w(f"  {DIM}{'━' * (W - 4)}{R}")
    footer_left = f"claude-recap v{VERSION}"
    footer_right = "github.com/buildingopen/claude-code-stats"
    gap = W - 4 - len(footer_left) - len(footer_right)
    w(f"  {DIM}{footer_left}{' ' * max(gap, 2)}{footer_right}{R}")
    w(f"  {DIM}Export: --html recap.html (Cmd+P for PDF) · --json for machines{R}")
    w("")

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------

def render_html(overview, cost, models, tokens, projects, activity, errors, trend, plan_cost, project_filter):
    """Generate a self-contained HTML dashboard. Returns HTML string."""
    period = _fmt_period(overview)
    total_days = overview["total_days"]
    plan_label = "Max" if plan_cost == 200 else "Max 5x" if plan_cost == 100 else "Pro"

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    sections = []

    # Overview
    sections.append(f"""<section>
<h2>Overview</h2>
<div class="grid">
  <div class="stat"><span class="label">Sessions</span><span class="value">{overview['sessions']:,}</span></div>
  <div class="stat"><span class="label">Active Days</span><span class="value">{overview['active_days']}/{overview['total_days']}</span></div>
  <div class="stat"><span class="label">Total Hours</span><span class="value">{overview['hours']}h</span></div>
  <div class="stat"><span class="label">Projects</span><span class="value">{overview['projects']}</span></div>
  <div class="stat"><span class="label">Avg Session</span><span class="value">{overview['avg_session_min']} min</span></div>
  <div class="stat"><span class="label">Messages</span><span class="value">{overview['messages']:,}</span></div>
</div>
</section>""")

    # Cost & ROI
    me_html = ""
    if cost["most_expensive"]:
        me = cost["most_expensive"]
        try:
            me_date = datetime.strptime(me['date'], "%Y-%m-%d").strftime("%b %d")
        except Exception:
            me_date = me['date']
        me_html = f'<div class="stat"><span class="label">Most Expensive</span><span class="value">{esc(fmt_cost(me["cost"]))}</span><span class="detail">{esc(me_date)} ({esc(me["project"])})</span></div>'

    spark_html = ""
    if cost["daily_values"]:
        spark_html = f'<div class="sparkline-row"><span class="label">Daily Spend</span><span class="spark">{sparkline(cost["daily_values"][-30:])}</span><span class="detail">avg {esc(fmt_cost(cost["daily_avg"]))}/day</span></div>'

    sections.append(f"""<section>
<h2>Cost & ROI</h2>
<div class="grid">
  <div class="stat"><span class="label">API Value</span><span class="value">{esc(fmt_cost(cost['api_value']))}</span></div>
  <div class="stat"><span class="label">Plan Cost</span><span class="value">{esc(fmt_cost(plan_cost))}</span><span class="detail">{plan_label}</span></div>
  <div class="stat"><span class="label">ROI</span><span class="value">{cost['roi']:.1f}x</span></div>
  <div class="stat"><span class="label">Cost / Session</span><span class="value">{esc(fmt_cost(cost['per_session']))}</span></div>
  <div class="stat"><span class="label">Cost / Hour</span><span class="value">{esc(fmt_cost(cost['per_hour']))}</span></div>
  {me_html}
</div>
{spark_html}
</section>""")

    # Models table
    model_rows = ""
    for m in models:
        model_rows += f'<tr><td>{esc(m["model"])}</td><td class="num">{m["sessions"]:,}</td><td class="num">{esc(fmt_cost(m["cost"]))}</td><td class="num">{m["pct"]:.1f}%</td></tr>\n'

    sections.append(f"""<section>
<h2>Models</h2>
<table>
<thead><tr><th>Model</th><th class="num">Sessions</th><th class="num">Cost</th><th class="num">% Spend</th></tr></thead>
<tbody>{model_rows}</tbody>
</table>
</section>""")

    # Tokens
    cache_pct = tokens["cache_hit_rate"]
    sections.append(f"""<section>
<h2>Tokens</h2>
<div class="grid">
  <div class="stat"><span class="label">Total</span><span class="value">{fmt_tokens(tokens['total'])}</span></div>
  <div class="stat"><span class="label">Input</span><span class="value">{fmt_tokens(tokens['input'])}</span></div>
  <div class="stat"><span class="label">Output</span><span class="value">{fmt_tokens(tokens['output'])}</span></div>
  <div class="stat"><span class="label">Cache Read</span><span class="value">{fmt_tokens(tokens['cache_read'])}</span></div>
  <div class="stat"><span class="label">Cache Create</span><span class="value">{fmt_tokens(tokens['cache_creation'])}</span></div>
  <div class="stat"><span class="label">Cache Hit</span><span class="value">{cache_pct:.1f}%</span></div>
</div>
<div class="bar-container"><div class="bar-fill" style="width:{cache_pct}%"></div></div>
<div class="grid" style="margin-top:1em">
  <div class="stat"><span class="label">Cache Savings</span><span class="value">~{esc(fmt_cost(tokens['cache_savings']))}</span></div>
  <div class="stat"><span class="label">Tokens / Session</span><span class="value">{fmt_tokens(tokens['per_session'])} avg</span></div>
</div>
</section>""")

    # Projects
    if not project_filter:
        proj_rows = ""
        for p in projects[:12]:
            proj_rows += f'<tr><td>{esc(p["name"])}</td><td class="num">{p["sessions"]:,}</td><td class="num">{esc(fmt_cost(p["cost"]))}</td><td class="num">{fmt_tokens(p["tokens"])}</td><td class="num">{p["hours"]:.1f}h</td></tr>\n'
        t_s = sum(p["sessions"] for p in projects)
        t_c = sum(p["cost"] for p in projects)
        t_t = sum(p["tokens"] for p in projects)
        t_h = sum(p["hours"] for p in projects)
        proj_rows += f'<tr class="total"><td>TOTAL</td><td class="num">{t_s:,}</td><td class="num">{esc(fmt_cost(t_c))}</td><td class="num">{fmt_tokens(t_t)}</td><td class="num">{t_h:.1f}h</td></tr>'

        sections.append(f"""<section>
<h2>Projects</h2>
<table>
<thead><tr><th>Project</th><th class="num">Sessions</th><th class="num">Cost</th><th class="num">Tokens</th><th class="num">Hours</th></tr></thead>
<tbody>{proj_rows}</tbody>
</table>
</section>""")

    # Activity
    buckets_html = " | ".join(f"{k}: {v}" for k, v in activity["duration_buckets"].items())
    sections.append(f"""<section>
<h2>Activity</h2>
<div class="grid">
  <div class="stat"><span class="label">Peak Hours</span><span class="value">{_fmt_peak(activity['peak_hour'])}</span></div>
  <div class="stat"><span class="label">Busiest Day</span><span class="value">{activity['busiest_day']}</span></div>
  <div class="stat"><span class="label">Active Days</span><span class="value">{activity['active_days']}</span></div>
  <div class="stat"><span class="label">Streak</span><span class="value">{activity['streak']} days</span></div>
</div>
<p class="duration-dist">{buckets_html}</p>
</section>""")

    # Errors
    if errors["total"] > 0:
        err_rows = ""
        by_type = errors.get("by_type", {})
        if by_type:
            max_count = max(by_type.values())
            for cat, count in sorted(by_type.items(), key=lambda x: -x[1])[:7]:
                pct = (count / errors["total"] * 100) if errors["total"] > 0 else 0
                bar_w = int(count / max_count * 100)
                err_rows += f'<tr><td>{esc(cat)}</td><td class="num">{count:,}</td><td class="num">{pct:.0f}%</td><td><div class="bar-container small"><div class="bar-fill" style="width:{bar_w}%"></div></div></td></tr>\n'

        sections.append(f"""<section>
<h2>Errors</h2>
<div class="grid">
  <div class="stat"><span class="label">Total</span><span class="value">{errors['total']:,}</span></div>
  <div class="stat"><span class="label">Rate</span><span class="value">{errors['rate']}%</span></div>
  <div class="stat"><span class="label">Per Session</span><span class="value">{errors['per_session']} avg</span></div>
</div>
<table class="errors">
<tbody>{err_rows}</tbody>
</table>
</section>""")

    # Trend
    tw = trend["this_week"]
    lw = trend["last_week"]
    if lw["sessions"] > 0:
        sections.append(f"""<section>
<h2>Trend <span class="detail">this week vs last</span></h2>
<table class="trend">
<tbody>
<tr><td>Sessions</td><td class="num">{lw['sessions']}</td><td>-></td><td class="num">{tw['sessions']}</td><td class="num">{delta_str(tw['sessions'], lw['sessions'])}</td></tr>
<tr><td>Cost</td><td class="num">{esc(fmt_cost(lw['cost']))}</td><td>-></td><td class="num">{esc(fmt_cost(tw['cost']))}</td><td class="num">{delta_str(tw['cost'], lw['cost'])}</td></tr>
<tr><td>Tokens</td><td class="num">{fmt_tokens(lw['tokens'])}</td><td>-></td><td class="num">{fmt_tokens(tw['tokens'])}</td><td class="num">{delta_str(tw['tokens'], lw['tokens'])}</td></tr>
<tr><td>Cache</td><td class="num">{lw['cache_hit_rate']:.1f}%</td><td>-></td><td class="num">{tw['cache_hit_rate']:.1f}%</td><td class="num">{delta_str(tw['cache_hit_rate'], lw['cache_hit_rate'])}</td></tr>
</tbody>
</table>
</section>""")

    body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Recap - {esc(period)}</title>
<style>
  :root {{ --bg: #0d1117; --surface: #161b22; --border: #30363d; --text: #c9d1d9;
           --text-dim: #8b949e; --text-bright: #f0f6fc; --accent: #da7756; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code',
          'JetBrains Mono', 'Cascadia Code', monospace; font-size: 14px; line-height: 1.6;
          max-width: 720px; margin: 0 auto; padding: 2em; }}
  header {{ border-bottom: 2px solid var(--border); padding-bottom: 1em; margin-bottom: 2em; }}
  header h1 {{ font-size: 1.4em; color: var(--text-bright); font-weight: 600; }}
  header .period {{ color: var(--text-dim); font-size: 0.9em; }}
  section {{ margin-bottom: 2.5em; }}
  h2 {{ font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim);
        border-bottom: 1px solid var(--border); padding-bottom: 0.5em; margin-bottom: 1em; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.8em; }}
  .stat {{ background: var(--surface); padding: 0.8em 1em; border-radius: 6px; border: 1px solid var(--border); }}
  .stat .label {{ display: block; font-size: 0.75em; color: var(--text-dim); text-transform: uppercase;
                  letter-spacing: 0.05em; margin-bottom: 0.2em; }}
  .stat .value {{ display: block; font-size: 1.2em; color: var(--text-bright); font-weight: 600; }}
  .stat .detail, .detail {{ font-size: 0.8em; color: var(--text-dim); font-weight: normal; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  thead th {{ text-align: left; color: var(--text-dim); font-weight: 400; font-size: 0.8em;
              text-transform: uppercase; letter-spacing: 0.05em; padding: 0.5em 0.8em;
              border-bottom: 1px solid var(--border); }}
  th.num, td.num {{ text-align: right; }}
  tbody td {{ padding: 0.5em 0.8em; border-bottom: 1px solid var(--border); }}
  tr.total td {{ font-weight: 600; color: var(--text-bright); border-top: 2px solid var(--border); }}
  .bar-container {{ background: var(--surface); border-radius: 4px; height: 8px; overflow: hidden;
                    border: 1px solid var(--border); }}
  .bar-container.small {{ height: 6px; display: inline-block; width: 100%; }}
  .bar-fill {{ background: var(--accent); height: 100%; border-radius: 3px; transition: width 0.3s; }}
  .sparkline-row {{ margin-top: 1em; display: flex; align-items: center; gap: 1em; }}
  .sparkline-row .label {{ color: var(--text-dim); font-size: 0.8em; text-transform: uppercase; }}
  .sparkline-row .spark {{ font-size: 1.4em; letter-spacing: 1px; }}
  .duration-dist {{ color: var(--text-dim); font-size: 0.85em; margin-top: 0.8em; }}
  footer {{ border-top: 2px solid var(--border); padding-top: 1em; color: var(--text-dim);
            font-size: 0.8em; display: flex; justify-content: space-between; }}
  footer a {{ color: var(--text-dim); text-decoration: none; }}
  footer a:hover {{ color: var(--text-bright); }}
  @media print {{
    body {{ background: #fff; color: #1a1a1a; max-width: none; padding: 1em; }}
    :root {{ --bg: #fff; --surface: #f6f8fa; --border: #d0d7de; --text: #1a1a1a;
             --text-dim: #656d76; --text-bright: #000; --accent: #da7756; }}
    .stat {{ page-break-inside: avoid; }}
  }}
  @media (max-width: 600px) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
</head>
<body>
<header>
  <h1>Claude Recap</h1>
  <div class="period">{esc(period)} &mdash; {total_days} days</div>
</header>
{body}
<footer>
  <span>claude-recap v{VERSION}</span>
  <a href="https://github.com/buildingopen/claude-code-stats">github.com/buildingopen/claude-code-stats</a>
</footer>
</body>
</html>"""


def render_json(overview, cost, models, tokens, projects, activity, errors, trend):
    data = {
        "version": VERSION,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": {
            "start": overview["first_date"],
            "end": overview["last_date"],
            "days": overview["total_days"],
            "active_days": overview["active_days"],
        },
        "overview": {
            "sessions": overview["sessions"],
            "hours": overview["hours"],
            "projects": overview["projects"],
            "avg_session_min": overview["avg_session_min"],
            "messages": overview["messages"],
        },
        "cost": {
            "api_value": cost["api_value"],
            "plan_cost": cost["plan_cost"],
            "roi": cost["roi"],
            "per_session": cost["per_session"],
            "per_hour": cost["per_hour"],
            "most_expensive": {
                "cost": round(cost["most_expensive"]["cost"], 2),
                "date": cost["most_expensive"]["date"],
                "project": cost["most_expensive"]["project"],
            } if cost["most_expensive"] else None,
            "daily": [{"date": d, "cost": round(c, 2)} for d, c in cost["daily"]],
        },
        "models": models,
        "tokens": {
            "total": tokens["total"],
            "input": tokens["input"],
            "output": tokens["output"],
            "cache_read": tokens["cache_read"],
            "cache_creation": tokens["cache_creation"],
            "cache_hit_rate": tokens["cache_hit_rate"] / 100,
            "cache_savings": tokens["cache_savings"],
            "per_session": tokens["per_session"],
        },
        "projects": projects,
        "activity": {
            "peak_hour": activity["peak_hour"],
            "busiest_day": activity["busiest_day"],
            "streak": activity["streak"],
            "active_days": activity["active_days"],
            "total_days": activity.get("total_days", 0),
            "duration_buckets": activity["duration_buckets"],
        },
        "errors": {
            "total": errors["total"],
            "rate": errors["rate"],
            "per_session": errors["per_session"],
            "by_type": errors["by_type"],
        },
        "trend": trend,
    }
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="claude-recap",
        description="Claude Code operational stats dashboard",
    )
    parser.add_argument("--days", type=int, help="Only include last N days")
    parser.add_argument("--project", type=str, help="Filter to a single project")
    parser.add_argument("--plan", type=str, default="max",
                        choices=["pro", "max5", "max", "max20"],
                        help="Subscription plan for ROI calculation")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON instead of dashboard")
    parser.add_argument("--html", type=str, metavar="FILE",
                        help="Export HTML dashboard to FILE")
    parser.add_argument("--version", action="store_true",
                        help="Show version")

    args = parser.parse_args()

    if args.version:
        print(f"claude-recap {VERSION}")
        return

    plan_costs = {"pro": 20, "max5": 100, "max": 200, "max20": 200}
    plan_cost = plan_costs.get(args.plan, 200)

    # Also respect env var from JS wrapper
    env_money = os.environ.get("RECAP_PLAN_COST")
    if env_money:
        try:
            plan_cost = int(env_money)
        except ValueError:
            pass

    quiet = args.json_output or bool(args.html)

    # Color codes for stderr (re-check since stderr might differ)
    _se = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
    _sb = "\033[1m" if _se else ""
    _sh = "\033[1;38;5;173m" if _se else ""
    _sd = "\033[2m" if _se else ""
    _sr = "\033[0m" if _se else ""

    if not quiet:
        sys.stderr.write(f"\n  {_sh}Claude Recap{_sr}  {_sd}github.com/buildingopen/claude-code-stats{_sr}\n")
        sys.stderr.write(f"  {_sd}100% local analysis · no data leaves your machine{_sr}\n\n")
        sys.stderr.write(f"  Discovering sessions...\n")
        sys.stderr.flush()

    # Find and parse sessions
    session_list = find_all_sessions(
        days_filter=args.days,
        project_filter=args.project,
    )

    if not session_list:
        print("No sessions found. Make sure CLAUDE_PROJECTS_DIR is set correctly.")
        print(f"Searched in: {', '.join(str(d) for d in CLAUDE_PROJECTS_DIRS)}")
        sys.exit(1)

    if not quiet:
        total_mb = sum(s for _, s, _, _ in session_list) / (1024 * 1024)
        sys.stderr.write(f"\r  Found {_sb}{len(session_list)}{_sr} sessions ({total_mb:.0f} MB)\n")
        sys.stderr.flush()

    stats = collect_stats(
        session_list,
        days_filter=args.days,
        project_filter=args.project,
        quiet=quiet,
    )

    sessions = stats["sessions"]
    if not sessions:
        print("No sessions matched the filters.")
        sys.exit(1)

    if not quiet:
        sys.stderr.write(f"  Crunching numbers...\n")
        sys.stderr.flush()

    # Compute all sections
    overview = compute_overview(sessions)
    cost = compute_cost(sessions, plan_cost)
    models = compute_models(sessions)
    tokens = compute_tokens(sessions)
    projects = compute_projects(sessions)
    activity = compute_activity(sessions)
    activity["total_days"] = overview["total_days"]
    errors = compute_errors(sessions, stats["error_categories"])
    trend = compute_trend(sessions)

    if not quiet:
        sys.stderr.write(f"\r  {_sd}Done.{_sr}\n\n")
        sys.stderr.flush()

    if args.json_output:
        render_json(overview, cost, models, tokens, projects, activity, errors, trend)
    elif args.html:
        html = render_html(overview, cost, models, tokens, projects, activity, errors, trend, plan_cost, args.project)
        out_path = args.html
        with open(out_path, "w") as f:
            f.write(html)
        sys.stderr.write(f"  {_sh}Saved to {out_path}{_sr}\n")
        sys.stderr.write(f"  {_sd}Tip: Open in browser, Cmd+P to save as PDF{_sr}\n\n")
    else:
        render_dashboard(overview, cost, models, tokens, projects, activity, errors, trend, plan_cost, args.project)


if __name__ == "__main__":
    main()
