#!/usr/bin/env python3
"""
Claude Recap: Operational stats dashboard for Claude Code.
Terminal output with ANSI colors. No HTML, no browser.

Reads ~/.claude/projects/ JSONL session transcripts, extracts token usage,
timestamps, error patterns, and model info. All processing is local.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure patterns/ is importable
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from patterns.config import CLAUDE_PROJECTS_DIRS, resolve_project_name
from patterns.project_stats import (
    parse_session_fast,
    estimate_costs,
    classify_model,
    fmt_tokens,
    PRICING,
)
from patterns.error_taxonomy import process_session as process_errors_session

# ── Plan pricing ──
PLANS = {
    "pro":  {"cost": 20,  "label": "Pro"},
    "max5": {"cost": 100, "label": "Max 5x"},
    "max":  {"cost": 200, "label": "Max 20x"},
}

# ── ANSI colors ──
_IS_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
_NO_COLOR = os.environ.get("NO_COLOR") or not _IS_TTY

def _c(code, text):
    if _NO_COLOR: return str(text)
    return f"\033[{code}m{text}\033[0m"

def _bold(t):   return _c("1", t)
def _dim(t):    return _c("2", t)
def _green(t):  return _c("32", t)
def _red(t):    return _c("31", t)
def _cyan(t):   return _c("36", t)
def _yellow(t): return _c("33", t)
def _white(t):  return _c("1;37", t)


# ── Formatting utilities ──

def _fmt_compact(n):
    """Format large numbers: 159K, 1.2M, 9.7B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))

def _fmt_cost(n):
    """Format dollar amount."""
    if n >= 1000:
        return f"${n:,.2f}"
    return f"${n:.2f}"

def _fmt_pct(n):
    """Format percentage."""
    return f"{n:.1f}%"

def _bar(value, maximum, width=30):
    """Horizontal bar chart."""
    if maximum <= 0:
        return "\u2591" * width
    filled = int(value / maximum * width)
    return "\u2588" * filled + "\u2591" * (width - filled)

def _sparkline(values):
    """Create a sparkline from a list of numbers."""
    if not values:
        return ""
    chars = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    mn, mx = min(values), max(values)
    rng = mx - mn if mx > mn else 1
    return "".join(chars[min(8, int((v - mn) / rng * 8))] for v in values)

def _delta(cur, prev):
    """Format change as +12.3% or -5.1% with arrow."""
    if prev == 0:
        return _dim("--")
    pct = (cur - prev) / prev * 100
    if pct > 0:
        return _green(f"+{pct:.1f}% \u25b2")
    elif pct < 0:
        return _red(f"{pct:.1f}% \u25bc")
    return _dim("0.0%")

def _kv(label, value, width=20):
    """Format a key-value pair."""
    return f"{label:<{width}}{value}"

def _header(title, right_text=""):
    """Print a section header."""
    print()
    if right_text:
        pad = 66 - len(title) - len(right_text)
        print(f"  {_bold(_white(title))}{' ' * max(1, pad)}{_dim(right_text)}")
    else:
        print(f"  {_bold(_white(title))}")
    print()

def _sep():
    """Print a separator line."""
    print(f"  {_dim('\u2500' * 66)}")


# ── Session discovery ──

def find_all_sessions(days_filter=None):
    """Find all JSONL session files, optionally filtered by recency."""
    seen = set()
    sessions = []
    cutoff = None
    if days_filter:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_filter)

    for projects_dir in CLAUDE_PROJECTS_DIRS:
        if not projects_dir.exists():
            continue
        for jsonl in projects_dir.rglob("*.jsonl"):
            if "subagent" in str(jsonl):
                continue
            try:
                resolved = jsonl.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                stat = jsonl.stat()
                if stat.st_size < 10 * 1024:
                    continue
                if cutoff:
                    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    if mtime < cutoff:
                        continue
                sessions.append(jsonl)
            except OSError:
                continue
    return sorted(sessions)


def get_proj_dir_name(filepath):
    """Extract project directory name from a session filepath."""
    parts = filepath.parts
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


# ── Data collection ──

def collect_stats(sessions, on_progress=None, project_filter=None):
    """Single pass: parse all sessions with parse_session_fast."""
    results = []
    total = len(sessions)
    for i, filepath in enumerate(sessions):
        if on_progress and i % 5 == 0:
            on_progress(i + 1, total)
        try:
            proj_dir = get_proj_dir_name(filepath)
            ps = parse_session_fast(filepath, proj_dir)
            if ps:
                if project_filter:
                    if ps["project_name"].lower() != project_filter.lower():
                        continue
                ps["_filepath"] = filepath
                results.append(ps)
        except Exception:
            continue
    if on_progress:
        on_progress(total, total)
    return results


def collect_errors(sessions, on_progress=None):
    """Second pass: classify errors by type."""
    error_counts = Counter()
    total = len(sessions)
    for i, filepath in enumerate(sessions):
        if on_progress and i % 10 == 0:
            on_progress(i + 1, total)
        try:
            result = process_errors_session(str(filepath))
            if result and result.get("errors"):
                for err in result["errors"]:
                    cat = err.get("category", "OTHER")
                    error_counts[cat] += 1
        except Exception:
            continue
    return error_counts


# ── Compute sections ──

def compute_all(sessions_data, plan_cost, plan_label):
    """Compute all dashboard metrics from parsed session data."""
    data = {}

    # ── Date range ──
    dates = []
    for s in sessions_data:
        if s.get("start_time"):
            dates.append(s["start_time"][:10])
    if dates:
        data["start_date"] = min(dates)
        data["end_date"] = max(dates)
        d0 = datetime.strptime(data["start_date"], "%Y-%m-%d")
        d1 = datetime.strptime(data["end_date"], "%Y-%m-%d")
        data["total_days"] = max(1, (d1 - d0).days + 1)
    else:
        data["start_date"] = "?"
        data["end_date"] = "?"
        data["total_days"] = 1
    unique_dates = set(dates)
    data["active_days"] = len(unique_dates)

    # ── Overview ──
    data["sessions"] = len(sessions_data)
    # Cap per-session duration at 4 hours to avoid inflated totals
    # from multi-day sessions (start/end spans days of intermittent use)
    MAX_SESSION_MIN = 240
    total_dur = sum(min(s["duration_min"] or 0, MAX_SESSION_MIN) for s in sessions_data)
    data["total_hours"] = round(total_dur / 60, 1)
    data["avg_session_min"] = round(total_dur / max(len(sessions_data), 1))
    data["messages"] = sum(s["message_count"] for s in sessions_data)
    projects = set(s["project_name"] for s in sessions_data)
    data["projects"] = len(projects)

    # ── Tokens ──
    data["input_tokens"] = sum(s["input_tokens"] for s in sessions_data)
    data["cache_read_tokens"] = sum(s["cache_read_tokens"] for s in sessions_data)
    data["cache_creation_tokens"] = sum(s["cache_creation_tokens"] for s in sessions_data)
    data["output_tokens"] = sum(s["output_tokens"] for s in sessions_data)
    data["total_tokens"] = (
        data["input_tokens"] + data["cache_read_tokens"] +
        data["cache_creation_tokens"] + data["output_tokens"]
    )
    all_input = data["input_tokens"] + data["cache_read_tokens"] + data["cache_creation_tokens"]
    data["cache_hit_rate"] = (data["cache_read_tokens"] / max(all_input, 1)) * 100
    data["tokens_per_session"] = data["total_tokens"] / max(data["sessions"], 1)

    # Cache savings: difference between full-price and cached-price for cache_read tokens
    cache_savings = 0
    for s in sessions_data:
        tier = classify_model(s["model"])
        pricing = PRICING.get(tier, PRICING["sonnet"])
        full_cost = s["cache_read_tokens"] / 1_000_000 * pricing["input"]
        cached_cost = s["cache_read_tokens"] / 1_000_000 * pricing["input"] * 0.1
        cache_savings += full_cost - cached_cost
    data["cache_savings"] = round(cache_savings)

    # ── Cost ──
    cost_by_tier, total_cost = estimate_costs(sessions_data)
    data["api_value"] = total_cost
    data["plan_cost"] = plan_cost
    data["plan_label"] = plan_label
    data["roi"] = round(total_cost / max(plan_cost, 1), 1)
    data["cost_per_session"] = total_cost / max(data["sessions"], 1)
    data["cost_per_hour"] = total_cost / max(data["total_hours"], 0.1)

    # Most expensive session
    best_cost = 0
    best_session = None
    for s in sessions_data:
        _, sc = estimate_costs([s])
        if sc > best_cost:
            best_cost = sc
            best_session = s
    data["most_expensive_cost"] = best_cost
    data["most_expensive_date"] = (best_session["start_time"][:10] if best_session and best_session.get("start_time") else "?")
    data["most_expensive_project"] = (best_session["project_name"] if best_session else "?")

    # Daily spend
    daily = defaultdict(float)
    for s in sessions_data:
        if s.get("start_time"):
            day = s["start_time"][:10]
            _, sc = estimate_costs([s])
            daily[day] += sc
    if daily:
        sorted_days = sorted(daily.keys())
        recent_days = sorted_days[-20:]
        data["daily_values"] = [round(daily.get(d, 0), 2) for d in recent_days]
        data["daily_avg"] = sum(daily.values()) / max(len(daily), 1)
    else:
        data["daily_values"] = []
        data["daily_avg"] = 0

    # ── Models ──
    model_sessions = Counter()
    for s in sessions_data:
        m = s["model"] or "unknown"
        model_sessions[m] += 1

    model_costs = {}
    for model_name in model_sessions:
        model_data = [s for s in sessions_data if (s["model"] or "unknown") == model_name]
        _, mc = estimate_costs(model_data)
        model_costs[model_name] = mc

    data["models"] = []
    for m in sorted(model_costs, key=model_costs.get, reverse=True):
        data["models"].append({
            "model": m,
            "sessions": model_sessions[m],
            "cost": model_costs[m],
            "pct": model_costs[m] / max(total_cost, 0.01) * 100,
        })

    # ── Per-project ──
    proj_sessions = defaultdict(list)
    for s in sessions_data:
        proj_sessions[s["project_name"]].append(s)

    data["project_table"] = []
    for name, psessions in proj_sessions.items():
        _, pc = estimate_costs(psessions)
        total_tok = sum(
            s["input_tokens"] + s["cache_read_tokens"] +
            s["cache_creation_tokens"] + s["output_tokens"]
            for s in psessions
        )
        total_hr = sum(min(s["duration_min"] or 0, MAX_SESSION_MIN) for s in psessions) / 60
        data["project_table"].append({
            "name": name,
            "sessions": len(psessions),
            "cost": pc,
            "tokens": total_tok,
            "hours": round(total_hr, 1),
        })
    data["project_table"].sort(key=lambda x: x["cost"], reverse=True)

    # ── Activity ──
    hours_counter = Counter()
    days_counter = Counter()
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for s in sessions_data:
        dt = s.get("start_dt")
        if dt:
            hours_counter[dt.hour] += 1
            days_counter[dt.weekday()] += 1

    if hours_counter:
        peak_hour = hours_counter.most_common(1)[0][0]
        data["peak_hour"] = f"{peak_hour}:00"
    else:
        data["peak_hour"] = "?"

    if days_counter:
        busiest = days_counter.most_common(1)[0][0]
        data["busiest_day"] = day_names[busiest]
    else:
        data["busiest_day"] = "?"

    # Streak
    sorted_dates = sorted(unique_dates)
    max_streak = 0
    current_streak = 1
    for i in range(1, len(sorted_dates)):
        d0 = datetime.strptime(sorted_dates[i-1], "%Y-%m-%d")
        d1 = datetime.strptime(sorted_dates[i], "%Y-%m-%d")
        if (d1 - d0).days == 1:
            current_streak += 1
        else:
            max_streak = max(max_streak, current_streak)
            current_streak = 1
    max_streak = max(max_streak, current_streak)
    data["streak"] = max_streak

    # Duration distribution
    dur_buckets = {"<10m": 0, "10-30m": 0, "30-60m": 0, "1-2h": 0, "2h+": 0}
    for s in sessions_data:
        d = s["duration_min"]
        if d is None or d <= 0:
            continue
        if d < 10: dur_buckets["<10m"] += 1
        elif d < 30: dur_buckets["10-30m"] += 1
        elif d < 60: dur_buckets["30-60m"] += 1
        elif d < 120: dur_buckets["1-2h"] += 1
        else: dur_buckets["2h+"] += 1
    data["duration_buckets"] = dur_buckets

    # ── Errors ──
    data["total_errors"] = sum(s["errors"] for s in sessions_data)
    data["error_rate"] = data["total_errors"] / max(data["messages"], 1) * 100
    data["errors_per_session"] = data["total_errors"] / max(data["sessions"], 1)

    # ── Trend (this week vs last week) ──
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    this_week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    last_week_start = this_week_start - timedelta(days=7)

    def _safe_tz(dt):
        if dt and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    tw = [s for s in sessions_data if s.get("start_dt") and _safe_tz(s["start_dt"]) >= this_week_start]
    lw = [s for s in sessions_data if s.get("start_dt") and
          last_week_start <= _safe_tz(s["start_dt"]) < this_week_start]

    _, tw_cost = estimate_costs(tw) if tw else ({}, 0)
    _, lw_cost = estimate_costs(lw) if lw else ({}, 0)

    tw_tokens = sum(s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"] + s["output_tokens"] for s in tw)
    lw_tokens = sum(s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"] + s["output_tokens"] for s in lw)

    tw_cache_input = sum(s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"] for s in tw)
    lw_cache_input = sum(s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"] for s in lw)
    tw_cache_rate = sum(s["cache_read_tokens"] for s in tw) / max(tw_cache_input, 1) * 100
    lw_cache_rate = sum(s["cache_read_tokens"] for s in lw) / max(lw_cache_input, 1) * 100

    data["trend"] = {
        "this_week": {"sessions": len(tw), "cost": round(tw_cost, 2), "tokens": tw_tokens, "cache_rate": round(tw_cache_rate, 1)},
        "last_week": {"sessions": len(lw), "cost": round(lw_cost, 2), "tokens": lw_tokens, "cache_rate": round(lw_cache_rate, 1)},
    }

    return data


# ── Render: Terminal Dashboard ──

def render_dashboard(data, error_categories=None):
    """Print the dashboard to stdout with ANSI colors."""

    # ── Header ──
    date_range = f"{data['start_date']}  to  {data['end_date']}"
    _sep()
    print(f"  {_bold(_white('CLAUDE RECAP'))}{' ' * max(1, 54 - len(date_range))}{_dim(date_range)}")
    print(f"  {' ' * 54}{_dim(str(data['total_days']) + ' days')}")
    _sep()

    # ── Overview ──
    _header("OVERVIEW")
    col1 = _kv("Sessions", _bold(str(data["sessions"])))
    col2 = _kv("Active Days", f"{data['active_days']}/{data['total_days']}", 14)
    print(f"  {col1}    {col2}")
    col1 = _kv("Total Hours", _bold(f"{data['total_hours']}h"))
    col2 = _kv("Projects", str(data["projects"]), 14)
    print(f"  {col1}    {col2}")
    col1 = _kv("Avg Session", f"{data['avg_session_min']} min")
    col2 = _kv("Messages", f"{data['messages']:,}", 14)
    print(f"  {col1}    {col2}")

    # ── Cost & ROI ──
    _header("COST & ROI")
    print(f"  {'API Value':<20}{_bold(_fmt_cost(data['api_value']))}")
    print(f"  {'Plan Cost':<20}{_fmt_cost(data['plan_cost'])}  {_dim('(' + data['plan_label'] + ')')}")
    roi_color = _green if data["roi"] >= 1 else _red
    roi_val = data["roi"]
    print(f"  {'ROI':<20}{roi_color(_bold(f'{roi_val}x'))}")
    print()
    print(f"  {'Cost / Session':<20}{_fmt_cost(data['cost_per_session'])}")
    print(f"  {'Cost / Hour':<20}{_fmt_cost(data['cost_per_hour'])}{' ' * 4}{_dim('(vs ~$75/hr junior dev)')}")
    print(f"  {'Most Expensive':<20}{_fmt_cost(data['most_expensive_cost'])}{' ' * 4}{_dim(data['most_expensive_date'] + '  ' + data['most_expensive_project'])}")
    if data["daily_values"]:
        spark = _sparkline(data["daily_values"])
        print()
        print(f"  {'Daily Spend':<20}{spark}  {_dim('avg ' + _fmt_cost(data['daily_avg']) + '/day')}")

    # ── Models ──
    if data["models"]:
        _header("MODELS")
        print(f"  {'Model':<30}{'Sessions':>10}{'Cost':>12}{'Share':>10}")
        print(f"  {_dim('\u2500' * 62)}")
        for m in data["models"]:
            name = m["model"]
            if len(name) > 28:
                name = name[:28] + ".."
            print(f"  {name:<30}{m['sessions']:>10}{_fmt_cost(m['cost']):>12}{_fmt_pct(m['pct']):>10}")

    # ── Tokens ──
    _header("TOKENS")
    print(f"  {'Total':<16}{_bold(_fmt_compact(data['total_tokens'])):<12}{'Input':<12}{_fmt_compact(data['input_tokens'])}")
    print(f"  {'Output':<16}{_fmt_compact(data['output_tokens']):<12}{'Cache Read':<12}{_fmt_compact(data['cache_read_tokens'])}")
    print(f"  {'Cache Create':<16}{_fmt_compact(data['cache_creation_tokens']):<12}{'Cache Hit':<12}{_fmt_pct(data['cache_hit_rate'])}")
    print()
    cache_bar = _bar(data["cache_hit_rate"], 100, width=24)
    print(f"  {'Cache Rate':<16}{cache_bar}  {_green(_fmt_pct(data['cache_hit_rate']))}")
    print(f"  {'Cache Savings':<16}{_green('~' + _fmt_cost(data['cache_savings']))}{' ' * 4}{_dim('vs uncached pricing')}")
    print(f"  {'Tokens/Session':<16}{_fmt_compact(data['tokens_per_session'])} avg")

    # ── Projects ──
    _header("PROJECTS")
    print(f"  {'Project':<24}{'Sessions':>10}{'Cost':>12}{'Tokens':>10}{'Hours':>8}")
    print(f"  {_dim('\u2500' * 64)}")
    shown = data["project_table"][:8]
    rest = data["project_table"][8:]
    for p in shown:
        name = p["name"]
        if len(name) > 22:
            name = name[:22] + ".."
        print(f"  {name:<24}{p['sessions']:>10}{_fmt_cost(p['cost']):>12}{_fmt_compact(p['tokens']):>10}{p['hours']:>7.1f}h")
    if rest:
        rest_sessions = sum(p["sessions"] for p in rest)
        rest_cost = sum(p["cost"] for p in rest)
        rest_tokens = sum(p["tokens"] for p in rest)
        rest_hours = sum(p["hours"] for p in rest)
        label = f"({len(rest)} more)"
        print(f"  {_dim(label):<24}{rest_sessions:>10}{_fmt_cost(rest_cost):>12}{_fmt_compact(rest_tokens):>10}{rest_hours:>7.1f}h")
    print(f"  {_dim('\u2500' * 64)}")
    total_sessions = sum(p["sessions"] for p in data["project_table"])
    total_cost = sum(p["cost"] for p in data["project_table"])
    total_tokens = sum(p["tokens"] for p in data["project_table"])
    total_hours = sum(p["hours"] for p in data["project_table"])
    print(f"  {_bold('TOTAL'):<24}{total_sessions:>10}{_bold(_fmt_cost(total_cost)):>12}{_fmt_compact(total_tokens):>10}{total_hours:>7.1f}h")

    # ── Activity ──
    _header("ACTIVITY")
    print(f"  {'Peak Hours':<20}{data['peak_hour']:<20}{'Busiest Day':<16}{data['busiest_day']}")
    print(f"  {'Active Days':<20}{data['active_days']} of {data['total_days']:<14}{'Streak':<16}{data['streak']}d")
    print()
    buckets = data["duration_buckets"]
    parts = [f"{k}: {v}" for k, v in buckets.items() if v > 0]
    print(f"  {_dim('Duration')}  {_dim('  '.join(parts))}")

    # ── Errors ──
    _header("ERRORS")
    rate_color = _green if data["error_rate"] < 5 else (_yellow if data["error_rate"] < 10 else _red)
    print(f"  {'Total':<12}{data['total_errors']:<10}{'Rate':<8}{rate_color(_fmt_pct(data['error_rate'])):<12}{'Per Session':<14}{data['errors_per_session']:.1f} avg")

    if error_categories:
        print()
        top_errors = error_categories.most_common(5)
        max_count = top_errors[0][1] if top_errors else 1
        for cat, count in top_errors:
            pct = count / max(data["total_errors"], 1) * 100
            bar = _bar(count, max_count, width=24)
            print(f"  {cat:<20}{count:>5}  {pct:>4.0f}%  {bar}")

    # ── Trend ──
    tw = data["trend"]["this_week"]
    lw = data["trend"]["last_week"]
    if tw["sessions"] > 0 or lw["sessions"] > 0:
        _header("TREND", "this week vs last")
        s_delta = _delta(tw["sessions"], lw["sessions"])
        c_delta = _delta(tw["cost"], lw["cost"])
        t_delta = _delta(tw["tokens"], lw["tokens"])
        print(f"  {'Sessions':<12}{lw['sessions']:>5} \u2192 {tw['sessions']:<5} {s_delta}")
        print(f"  {'Cost':<12}{_fmt_cost(lw['cost']):>8} \u2192 {_fmt_cost(tw['cost']):<8} {c_delta}")
        print(f"  {'Tokens':<12}{_fmt_compact(lw['tokens']):>5} \u2192 {_fmt_compact(tw['tokens']):<5} {t_delta}")

    # ── Footer ──
    print()
    _sep()
    print(f"  {_dim('claude-recap v2.1.0')}{' ' * 20}{_dim('github.com/buildingopen/claude-code-stats')}")
    print()


# ── Render: JSON ──

def render_json(data, error_categories=None):
    """Print JSON to stdout."""
    out = {
        "version": "2.1.0",
        "generated": datetime.now(timezone.utc).isoformat(),
        "period": {
            "start": data["start_date"],
            "end": data["end_date"],
            "days": data["total_days"],
            "active_days": data["active_days"],
        },
        "overview": {
            "sessions": data["sessions"],
            "hours": data["total_hours"],
            "projects": data["projects"],
            "avg_session_min": data["avg_session_min"],
            "messages": data["messages"],
        },
        "cost": {
            "api_value": round(data["api_value"], 2),
            "plan_cost": data["plan_cost"],
            "plan_label": data["plan_label"],
            "roi": data["roi"],
            "per_session": round(data["cost_per_session"], 2),
            "per_hour": round(data["cost_per_hour"], 2),
            "most_expensive": {
                "cost": round(data["most_expensive_cost"], 2),
                "date": data["most_expensive_date"],
                "project": data["most_expensive_project"],
            },
            "daily_avg": round(data["daily_avg"], 2),
        },
        "models": [{"model": m["model"], "sessions": m["sessions"],
                     "cost": round(m["cost"], 2), "pct": round(m["pct"], 1)}
                    for m in data["models"]],
        "tokens": {
            "total": data["total_tokens"],
            "input": data["input_tokens"],
            "output": data["output_tokens"],
            "cache_read": data["cache_read_tokens"],
            "cache_creation": data["cache_creation_tokens"],
            "cache_hit_rate": round(data["cache_hit_rate"], 1),
            "cache_savings": data["cache_savings"],
            "per_session": round(data["tokens_per_session"]),
        },
        "projects": [{"name": p["name"], "sessions": p["sessions"],
                       "cost": round(p["cost"], 2), "tokens": p["tokens"],
                       "hours": p["hours"]} for p in data["project_table"]],
        "activity": {
            "peak_hour": data["peak_hour"],
            "busiest_day": data["busiest_day"],
            "streak": data["streak"],
            "active_days": data["active_days"],
            "total_days": data["total_days"],
            "duration_buckets": data["duration_buckets"],
        },
        "errors": {
            "total": data["total_errors"],
            "rate": round(data["error_rate"], 1),
            "per_session": round(data["errors_per_session"], 1),
        },
        "trend": data["trend"],
    }
    if error_categories:
        out["errors"]["by_type"] = dict(error_categories.most_common())

    json.dump(out, sys.stdout, indent=2, default=str)
    print()


# ── Progress display ──

def _progress_bar_str(current, total, width=24):
    pct = current / max(total, 1)
    filled = int(width * pct)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f"{bar} {int(pct * 100)}%"

def _show_progress(phase, num, total_phases, current, count):
    if not _IS_TTY:
        return
    bar = _progress_bar_str(current, count)
    sys.stdout.write(f"\r  {_dim(f'[{num}/{total_phases}]')} {phase} {bar} {_dim(f'{current}/{count}')}")
    sys.stdout.flush()

def _show_done(phase, num, total_phases, detail):
    prefix = _dim(f"[{num}/{total_phases}]")
    if _IS_TTY:
        sys.stdout.write(f"\r\033[K")
    sys.stdout.write(f"  {prefix} {_green('\u2713')} {phase} {_cyan(detail)}\n")
    sys.stdout.flush()


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Claude Recap: Operational stats for Claude Code")
    parser.add_argument("--plan", choices=["pro", "max5", "max"], default="max",
                        help="Your Claude plan for ROI calculation (default: max)")
    parser.add_argument("--days", type=int, default=None,
                        help="Only include sessions from the last N days")
    parser.add_argument("--project", type=str, default=None,
                        help="Filter to a specific project name")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output as JSON instead of terminal dashboard")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI colors")
    args = parser.parse_args()

    if args.no_color:
        global _NO_COLOR
        _NO_COLOR = True

    plan = PLANS[args.plan]
    plan_cost = plan["cost"]
    plan_label = plan["label"]

    if not args.json_output:
        print()
        print(f"  {_bold(_white('Claude Recap'))}")
        print(f"  {_dim('by BuildingOpen')}")
        print()
        print(f"  {_dim('\u2714 100% local')}{' ' * 3}{_dim('\u2714 Zero dependencies')}{' ' * 3}{_dim('\u2714 No network calls')}")
        print()

    # Phase 1: Find sessions
    sessions = find_all_sessions(days_filter=args.days)
    if not args.json_output:
        _show_done("Finding sessions", 1, 3, f"{len(sessions)} files")

    if not sessions:
        print()
        print(f"  No sessions found in {CLAUDE_PROJECTS_DIRS[0]}")
        print(f"  Set CLAUDE_PROJECTS_DIR to point to your Claude Code projects directory.")
        print()
        return

    # Phase 2: Parse sessions
    def _on_parse(cur, tot):
        _show_progress("Parsing sessions", 2, 3, cur, tot)

    sessions_data = collect_stats(sessions, on_progress=_on_parse, project_filter=args.project)
    if not args.json_output:
        _show_done("Parsing sessions", 2, 3, f"{len(sessions_data)} sessions analyzed")

    if not sessions_data:
        print()
        if args.project:
            print(f"  No sessions found for project '{args.project}'")
        else:
            print(f"  No valid session data found.")
        print()
        return

    # Phase 3: Classify errors (use only filtered sessions if project filter active)
    def _on_errors(cur, tot):
        _show_progress("Classifying errors", 3, 3, cur, tot)

    error_sessions = sessions
    if args.project:
        error_sessions = [s["_filepath"] for s in sessions_data if "_filepath" in s]
    error_categories = collect_errors(error_sessions, on_progress=_on_errors)
    if not args.json_output:
        _show_done("Classifying errors", 3, 3, f"{sum(error_categories.values())} errors categorized")

    # Compute all metrics
    data = compute_all(sessions_data, plan_cost, plan_label)

    # Render
    if args.json_output:
        render_json(data, error_categories)
    else:
        print()
        render_dashboard(data, error_categories)


if __name__ == "__main__":
    main()
