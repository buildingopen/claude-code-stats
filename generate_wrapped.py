#!/usr/bin/env python3
"""
Generate a self-contained wrapped.html from Claude Code session data.

Imports analyzers from pattern scripts, iterates sessions once,
computes all metrics, and string-substitutes into wrapped.html template.
Outputs dist/wrapped.html.
"""

import argparse
import hashlib
import json
import logging
import os
import html as html_mod
import re
import sys
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Ensure patterns/ is importable
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from patterns.config import CLAUDE_PROJECTS_DIR, CLAUDE_PROJECTS_DIRS, resolve_project_name
from patterns.session_outcomes import analyze_session as analyze_outcome
from patterns.communication_tone import analyze_session as analyze_tone
from patterns.tool_misuse import analyze_session as analyze_misuse
from patterns.self_scoring import process_session as process_scoring
from patterns.error_taxonomy import process_session as process_errors, analyze_error_sequences
from patterns.retry_loops import (
    parse_session as parse_retry_session,
    extract_tool_calls,
    detect_consecutive_retries,
    detect_edit_fail_loops,
    detect_bash_retries,
    detect_search_retries,
    detect_user_rejections,
)
from patterns.project_stats import parse_session_fast, estimate_costs
from patterns.prompting_style import extract_user_text

# Note: hook_rejections and large_file_errors are excluded from wrapped report
# because they lack per-session analyze_session() functions. Their insights
# (hook blocks, file-too-large errors) are already captured indirectly via
# error_taxonomy (HOOK_BLOCKED, FILE_TOO_LARGE categories).


# ---------------------------------------------------------------------------
# Percentile benchmarks (hardcoded distributions)
# Based on Cursor Wrapped data, Claude Code pricing/limits, GitHub Unwrapped,
# and power-law usage distributions.
# Each entry: (percentile, value) - interpolated for the user's actual value.
# ---------------------------------------------------------------------------
BENCHMARKS = {
    "sessions_monthly": [(25, 8), (50, 20), (75, 50), (90, 100), (95, 200), (99, 400)],
    "hours_monthly": [(25, 20), (50, 80), (75, 250), (90, 600), (95, 1500), (99, 5000)],
    "loc_monthly": [(25, 150), (50, 1000), (75, 5000), (90, 15000), (95, 35000), (99, 100000)],
    "tokens_monthly": [(25, 2e6), (50, 15e6), (75, 150e6), (90, 600e6), (95, 1.5e9), (99, 5e9)],
    "cost_monthly": [(25, 5), (50, 30), (75, 150), (90, 600), (95, 2500), (99, 10000)],
    "success_pct": [(25, 30), (50, 50), (75, 65), (90, 75), (95, 85), (99, 95)],
    "deployments_monthly": [(25, 2), (50, 10), (75, 30), (90, 150), (95, 600), (99, 1500)],
}


def compute_percentile(value, benchmark_key):
    """Return percentile (0-100) by interpolating in benchmark table."""
    table = BENCHMARKS[benchmark_key]
    if value <= 0:
        return 0
    if value <= table[0][1]:
        return max(1, int(value / table[0][1] * table[0][0]))
    for i in range(len(table) - 1):
        p1, v1 = table[i]
        p2, v2 = table[i + 1]
        if value <= v2:
            ratio = (value - v1) / (v2 - v1)
            return int(p1 + ratio * (p2 - p1))
    return 99


def compute_percentiles(d):
    """Compute percentiles for all benchmarked metrics. Returns dict."""
    days = max(1, d.get("days", 1))
    months = days / 30.0

    # Normalize all time-dependent metrics to per-month rates
    sessions_monthly = d.get("sessions", 0) / months
    hours_monthly = d.get("hours", 0) / months
    loc_monthly = d.get("loc", 0) / months
    tokens_monthly = d.get("total_tokens", 0) / months
    cost_monthly = d.get("total_cost", 0) / months
    deployments_monthly = d.get("deployments", 0) / months

    pcts = {
        "sessions": compute_percentile(sessions_monthly, "sessions_monthly"),
        "hours": compute_percentile(hours_monthly, "hours_monthly"),
        "loc": compute_percentile(loc_monthly, "loc_monthly"),
        "tokens": compute_percentile(tokens_monthly, "tokens_monthly"),
        "cost": compute_percentile(cost_monthly, "cost_monthly"),
        "success": compute_percentile(d.get("success_pct", 0), "success_pct"),
        "deployments": compute_percentile(deployments_monthly, "deployments_monthly"),
    }

    # Overall = weighted average
    weights = {
        "sessions": 0.20, "tokens": 0.20, "loc": 0.20,
        "success": 0.15, "deployments": 0.15, "cost": 0.10,
    }
    pcts["overall"] = int(sum(pcts[k] * w for k, w in weights.items()))

    return pcts


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TEMPLATE_PATH = SCRIPT_DIR / "wrapped.html"
OUTPUT_DIR = SCRIPT_DIR / "dist"
OUTPUT_PATH = OUTPUT_DIR / "wrapped.html"

# Author config (overridable via env vars)
AUTHOR_NAME = os.environ.get("WRAPPED_AUTHOR", "")
TIMEZONE_OFFSET = int(os.environ.get("WRAPPED_TZ_OFFSET", "0"))  # hours from UTC
MONEY_PAID = int(os.environ.get("WRAPPED_MONEY_PAID", "0"))  # total subscription cost
MONEY_DETAIL = os.environ.get("WRAPPED_MONEY_DETAIL", "")  # e.g. "3 Claude Max accounts"
SHARE_URL = os.environ.get("WRAPPED_SHARE_URL", "")  # public URL for share buttons


def find_all_sessions():
    """Find all JSONL session files across all configured directories, no cap."""
    seen = set()
    sessions = []
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
                if stat.st_size >= 10 * 1024:  # 10KB min
                    sessions.append(jsonl)
            except OSError:
                continue
    return sorted(sessions)


def get_proj_dir_name(filepath):
    """Extract project directory name from a session filepath."""
    # ~/.claude/projects/<encoded-dir-name>/<session>.jsonl
    parts = filepath.parts
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


def fmt_number(n):
    """Format number with commas."""
    return f"{n:,}"


def fmt_compact(n):
    """Format large numbers compactly: 159K, 1.2M, 9.7B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def hour_label(h):
    """Convert 24h int to display label like '7pm'."""
    if h == 0:
        return "12am"
    if h == 12:
        return "12pm"
    if h < 12:
        return f"{h}am"
    return f"{h - 12}pm"


def censor_word(w):
    """Light censoring for display."""
    w_lower = w.lower()
    MAP = {"fuck": "f**k", "shit": "sh*t", "damn": "d*mn", "ass": "a**", "bitch": "b*tch"}
    return MAP.get(w_lower, w)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def collect_data(max_sessions=None):
    """Single pass over all sessions, calling all analyzers."""
    sessions = find_all_sessions()
    if max_sessions:
        sessions = sessions[:max_sessions]

    # Accumulators
    outcomes = []
    tones = []
    misuses = []
    scoring_instances = []
    error_sessions = []
    loop_findings = []
    project_sessions = []
    all_user_texts = []  # for prompting style
    session_word_counts = {}  # filepath -> list of word counts per user msg

    for i, filepath in enumerate(sessions):
        # 1. Session outcomes
        try:
            o = analyze_outcome(filepath)
            if o:
                outcomes.append(o)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 2. Communication tone
        try:
            t = analyze_tone(filepath)
            if t:
                tones.append(t)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 3. Tool misuse
        try:
            m = analyze_misuse(filepath)
            if m and m.get("findings"):
                misuses.append(m)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 4. Self-scoring
        try:
            instances = process_scoring(filepath)
            scoring_instances.extend(instances)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 5. Error taxonomy
        try:
            edata = process_errors(str(filepath))
            if edata and edata.get("errors"):
                error_sessions.append(edata)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 6. Retry loops
        try:
            msgs = parse_retry_session(filepath)
            tool_calls = extract_tool_calls(msgs)
            if tool_calls:
                findings = []
                findings.extend(detect_consecutive_retries(tool_calls))
                findings.extend(detect_edit_fail_loops(tool_calls))
                findings.extend(detect_bash_retries(tool_calls))
                findings.extend(detect_search_retries(tool_calls))
                findings.extend(detect_user_rejections(tool_calls))
                for f in findings:
                    f["session_file"] = str(filepath)
                if findings:
                    loop_findings.extend(findings)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 7. Project stats
        try:
            proj_dir = get_proj_dir_name(filepath)
            ps = parse_session_fast(filepath, proj_dir)
            if ps:
                project_sessions.append(ps)
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

        # 8. Prompting style (extract user texts from raw JSONL)
        try:
            session_texts = []
            with open(filepath) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if obj.get("type") != "user":
                        continue
                    cleaned = extract_user_text(obj)
                    if cleaned and cleaned not in (
                        "[continuation from previous session]",
                        "Implement the following plan",
                    ):
                        session_texts.append(cleaned)
            all_user_texts.extend(session_texts)
            # Track per-session word counts for error-rate-by-prompt-length
            wc_list = [len(t.split()) for t in session_texts if t.split()]
            if wc_list:
                session_word_counts[str(filepath)] = wc_list
        except Exception:
            log.debug("Analyzer failed on %s", filepath, exc_info=True)

    return {
        "outcomes": outcomes,
        "tones": tones,
        "misuses": misuses,
        "scoring_instances": scoring_instances,
        "error_sessions": error_sessions,
        "loop_findings": loop_findings,
        "project_sessions": project_sessions,
        "all_user_texts": all_user_texts,
        "session_word_counts": session_word_counts,
        "session_count": len(sessions),
    }


def _compute_scale(d, outcomes, session_count):
    """Act 1: Sessions, hours, LOC, ships."""
    d["sessions"] = session_count

    all_dates = []
    for o in outcomes:
        if o.get("start"):
            all_dates.append(o["start"])
        if o.get("end"):
            all_dates.append(o["end"])
    if all_dates:
        d["start_date"] = min(all_dates)
        d["end_date"] = max(all_dates)
        d["days"] = max(1, (d["end_date"] - d["start_date"]).days)
    else:
        d["start_date"] = datetime.now()
        d["end_date"] = datetime.now()
        d["days"] = 1

    total_minutes = sum(o.get("duration_min", 0) for o in outcomes)
    d["hours"] = round(total_minutes / 60)
    d["hours_days"] = round(d["hours"] / 24)

    max_duration = max((o.get("duration_min", 0) for o in outcomes), default=0)
    d["longest_session_hours"] = round(max_duration / 60, 1)
    d["longest_session_days"] = round(max_duration / 60 / 24, 1)

    d["loc"] = sum(o.get("loc_changed", 0) for o in outcomes)
    d["loc_added"] = sum(o.get("loc_added", 0) for o in outcomes)
    d["loc_removed"] = sum(o.get("loc_removed", 0) for o in outcomes)
    d["files_edited_count"] = sum(o.get("files_edited", 0) for o in outcomes)
    d["commits"] = sum(o.get("commits", 0) for o in outcomes)
    d["deployments"] = sum(o.get("deployments", 0) for o in outcomes)
    d["bash_commands"] = sum(o.get("bash_commands", 0) for o in outcomes)
    d["commits_per_deploy"] = round(d["commits"] / d["deployments"]) if d["deployments"] > 0 else 0
    d["abandoned_count"] = sum(1 for o in outcomes if o.get("abandoned"))

    # Session categories (BUILD, FIX, EXPLORE, etc.)
    d["session_categories"] = Counter(o.get("category", "MIXED") for o in outcomes)


def _compute_method(d, outcomes, project_sessions, all_user_texts, session_word_counts):
    """Act 2: Projects, prompting style, tokens, cost."""
    # Projects
    project_counts = Counter()
    for ps in project_sessions:
        project_counts[ps.get("project_name", "Unknown")] += 1
    d["top_projects"] = project_counts.most_common(5)

    # Prompting style
    word_counts = [len(txt.split()) for txt in all_user_texts if txt.split()]
    if word_counts:
        d["median_words"] = round(statistics.median(word_counts))
        short = sum(1 for w in word_counts if w < 10)
        d["pct_short"] = round(short / len(word_counts) * 100, 1)
    else:
        d["median_words"] = 0
        d["pct_short"] = 0

    # Error rates by prompt length
    short_errs, long_errs = [], []
    threshold = d.get("median_words", 10)
    for o in outcomes:
        sid = o.get("session_id", "")
        er = o.get("error_rate", 0)
        if o.get("total_tool_uses", 0) == 0 or er == 0:
            continue
        matched_wc = None
        for path_str, wc_list in session_word_counts.items():
            if sid and sid in path_str:
                matched_wc = wc_list
                break
        if matched_wc:
            avg = statistics.mean(matched_wc)
            (short_errs if avg < threshold else long_errs).append(er)

    d["short_prompt_errors"] = round(statistics.mean(short_errs), 1) if short_errs else 0
    d["long_prompt_errors"] = round(statistics.mean(long_errs), 1) if long_errs else 0

    # Success rate by prompt length
    short_successes, long_successes = [], []
    for o in outcomes:
        sid = o.get("session_id", "")
        matched_wc = None
        for path_str, wc_list in session_word_counts.items():
            if sid and sid in path_str:
                matched_wc = wc_list
                break
        if matched_wc:
            avg = statistics.mean(matched_wc)
            bucket = short_successes if avg < threshold else long_successes
            bucket.append(1 if o["outcome"] == "SUCCESS" else 0)
    d["short_prompt_success_pct"] = round(statistics.mean(short_successes) * 100) if short_successes else 0
    d["long_prompt_success_pct"] = round(statistics.mean(long_successes) * 100) if long_successes else 0
    if d["long_prompt_errors"] > 0 and d["short_prompt_errors"] > 0:
        ratio = d["short_prompt_errors"] / d["long_prompt_errors"]
        if ratio >= 1:
            d["error_ratio"] = f"{ratio:.1f}x"
            d["error_ratio_text"] = f'{d["error_ratio"]} more errors when you\'re terse'
        else:
            d["error_ratio"] = f"{1/ratio:.1f}x"
            d["error_ratio_text"] = f'{d["error_ratio"]} more errors with longer prompts'
    else:
        d["error_ratio"] = "N/A"
        d["error_ratio_text"] = ""

    # Example short prompts
    short_examples = Counter()
    for txt in all_user_texts:
        if len(txt.split()) <= 3 and len(txt) < 30:
            short_examples[txt.lower().strip()] += 1
    d["prompt_examples"] = [w for w, _ in short_examples.most_common(5)]

    # Tokens
    total_input = sum(ps.get("input_tokens", 0) for ps in project_sessions)
    total_output = sum(ps.get("output_tokens", 0) for ps in project_sessions)
    total_cache_read = sum(ps.get("cache_read_tokens", 0) for ps in project_sessions)
    total_cache_create = sum(ps.get("cache_creation_tokens", 0) for ps in project_sessions)
    total_tokens = total_input + total_output + total_cache_read + total_cache_create
    d["total_tokens"] = total_tokens

    if total_tokens >= 1_000_000_000:
        d["tokens_display"] = round(total_tokens / 1_000_000_000, 1)
        d["tokens_suffix"] = "B"
    elif total_tokens >= 1_000_000:
        d["tokens_display"] = round(total_tokens / 1_000_000, 1)
        d["tokens_suffix"] = "M"
    else:
        d["tokens_display"] = round(total_tokens / 1_000, 1)
        d["tokens_suffix"] = "K"

    words_equivalent = total_tokens / 1.3
    reading_years = words_equivalent / (250 * 60 * 24 * 365)
    if reading_years >= 1:
        d["tokens_reading_comparison"] = f"{reading_years:.0f} years of non-stop reading"
    else:
        d["tokens_reading_comparison"] = f"{reading_years * 365:.0f} days of non-stop reading"

    d["total_messages"] = sum(ps.get("message_count", 0) for ps in project_sessions)

    if project_sessions:
        _, total_cost = estimate_costs(project_sessions)
        d["total_cost"] = total_cost
    else:
        d["total_cost"] = 0


def _compute_reality(d, outcomes, error_sessions, loop_findings, scoring_instances, misuses):
    """Act 3: Errors, loops, scoring, success rate, tool misuse."""
    # Errors
    all_errors = []
    for es in error_sessions:
        all_errors.extend(analyze_error_sequences(es))

    error_categories = Counter()
    post_actions = Counter()
    for e in all_errors:
        error_categories[e.get("category", "UNKNOWN")] += 1
        post_actions[e.get("post_action", "unknown")] += 1

    d["total_errors"] = sum(error_categories.values())
    d["error_categories"] = error_categories
    d["error_files_count"] = len(error_sessions)
    total_actions = sum(post_actions.values())
    d["switch_rate"] = round(post_actions.get("switched_approach", 0) / max(total_actions, 1) * 100, 1)
    d["switched_pct"] = round(post_actions.get("switched_approach", 0) / max(total_actions, 1) * 100)
    d["retried_pct"] = round(post_actions.get("retried_same_tool", 0) / max(total_actions, 1) * 100)
    d["gave_up_pct"] = round(post_actions.get("gave_up", 0) / max(total_actions, 1) * 100)

    # Loops
    total_wasted = sum(f.get("estimated_tokens", 0) for f in loop_findings)
    d["wasted_tokens"] = total_wasted
    d["wasted_tokens_m"] = round(total_wasted / 1_000_000, 1)
    d["loop_count"] = len(loop_findings)
    d["sessions_with_loops"] = len(set(
        f.get("session_slug", f.get("session_file", "")) for f in loop_findings
    ))

    if loop_findings:
        worst = max(loop_findings, key=lambda f: f.get("estimated_tokens", 0))
        tool = worst.get("tool", worst.get("command_base", "unknown"))
        count = worst.get("count", worst.get("total_attempts", 0))
        d["worst_loop"] = f'"{tool}" {count} times in a row'
    else:
        d["worst_loop"] = ""

    # Wasted cost estimate (set after tokens are computed in _compute_method)
    # Will be finalized in compute_aggregates after both methods run

    # Self-scoring
    if scoring_instances:
        scores = [inst.score_value for inst in scoring_instances]
        d["avg_score"] = round(statistics.mean(scores), 1)
        d["median_score"] = round(statistics.median(scores))
        perfect = sum(1 for s in scores if s == 10)
        d["pct_perfect"] = round(perfect / len(scores) * 100)
        d["total_scores"] = len(scores)
        d["bugs_after_high"] = sum(
            1 for inst in scoring_instances if inst.score_value >= 9 and inst.outcome_gap
        )
    else:
        d["avg_score"] = 0
        d["median_score"] = 0
        d["pct_perfect"] = 0
        d["total_scores"] = 0
        d["bugs_after_high"] = 0

    # Success rate
    if outcomes:
        success = sum(1 for o in outcomes if o["outcome"] == "SUCCESS")
        partial = sum(1 for o in outcomes if o["outcome"] == "PARTIAL_SUCCESS")
        d["success_pct"] = round((success + partial) / len(outcomes) * 100)
        d["full_success_pct"] = round(success / len(outcomes) * 100, 1)
        d["partial_success_pct"] = round(partial / len(outcomes) * 100, 1)
    else:
        d["success_pct"] = 0
        d["full_success_pct"] = 0
        d["partial_success_pct"] = 0

    # Tool misuse
    misuse_counts = Counter()
    PATTERN_MAP = {
        "grep": "grep via Bash", "glob": "find via Bash", "find": "find via Bash",
        "read": "cat/head/tail via Bash", "write": "echo/cat write via Bash",
        "agent_overkill": "Agent overkill", "repeated_reads": "Repeated file reads",
    }
    for m in misuses:
        for f in m.get("findings", []):
            pattern = f.get("pattern", "unknown")
            matched = next((v for k, v in PATTERN_MAP.items() if k in pattern), pattern)
            misuse_counts[matched] += 1
    d["misuse_total"] = sum(misuse_counts.values())
    d["misuse_top"] = misuse_counts.most_common(3)


def _compute_relationship(d, tones, project_sessions):
    """Act 4: Niceness, tone clock, swears, machine split."""
    if tones:
        d["niceness_score"] = round(statistics.mean(t["niceness_score"] for t in tones), 1)
        d["user_nice_total"] = sum(t["user_nice"] for t in tones)
        d["user_harsh_total"] = sum(t["user_harsh"] for t in tones)
        d["user_swears_total"] = sum(t["user_swears"] for t in tones)
        d["assistant_nice_total"] = sum(t["assistant_nice"] for t in tones)
        d["assistant_swears_total"] = sum(t["assistant_swears"] for t in tones)

        all_nice_words = Counter()
        for t in tones:
            for word, count in t.get("user_nice_words", {}).items():
                all_nice_words[word] += count
        d["please_count"] = all_nice_words.get("please", 0)

        d["nice_to_harsh"] = (
            f'{d["user_nice_total"] / d["user_harsh_total"]:.1f}x'
            if d["user_harsh_total"] > 0 else "inf"
        )
        d["claude_nice_ratio"] = (
            round(d["assistant_nice_total"] / d["user_nice_total"], 1)
            if d["user_nice_total"] > 0 else 0
        )

        all_swear_words = Counter()
        for t in tones:
            for word, count in t.get("user_swear_words", {}).items():
                all_swear_words[word] += count
        d["top_swears"] = all_swear_words.most_common(5)

        # Hourly distributions
        swears_by_hour = [0] * 24
        nice_by_hour = [0] * 24
        for t in tones:
            for h_str, count in t.get("swears_by_hour", {}).items():
                swears_by_hour[int(h_str) % 24] += count
            for h_str, count in t.get("nice_by_hour", {}).items():
                nice_by_hour[int(h_str) % 24] += count
        d["swears_by_hour"] = swears_by_hour
        d["nice_by_hour"] = nice_by_hour

        def shifted_peak(arr, offset):
            shifted = [0] * 24
            for h in range(24):
                shifted[(h + offset) % 24] += arr[h]
            peak_h = max(range(24), key=lambda h: shifted[h])
            return peak_h, shifted[peak_h]

        sh, sv = shifted_peak(swears_by_hour, TIMEZONE_OFFSET)
        nh, nv = shifted_peak(nice_by_hour, TIMEZONE_OFFSET)
        d["swear_peak_hour"], d["swear_peak_count"] = sh, sv
        d["nice_peak_hour"], d["nice_peak_count"] = nh, nv

        best_example = ""
        for t in tones:
            for excerpt, words, *rest in t.get("swear_examples_with_hour", []):
                if len(excerpt) > len(best_example):
                    best_example = excerpt
        # Sanitize PII from quote (phone numbers, emails, IPs)
        if best_example:
            best_example = re.sub(r'\+\d[\d\s\-()]{7,}\d', '[redacted]', best_example)
            best_example = re.sub(r'\b\d{3}[\s\-]\d{3}[\s\-]\d{4}\b', '[redacted]', best_example)
            best_example = re.sub(r'[\w.+-]+@[\w.-]+\.\w+', '[redacted]', best_example)
            best_example = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[redacted]', best_example)
        d["swear_example_quote"] = best_example[:80] if best_example else ""

        total_user_msgs = sum(t["user_msg_count"] for t in tones)
        d["total_user_msgs"] = total_user_msgs
        d["swear_pct"] = round(d["user_swears_total"] / total_user_msgs * 100, 1) if total_user_msgs > 0 else 0
    else:
        d.update({
            "niceness_score": 5.0, "user_nice_total": 0, "user_harsh_total": 0,
            "user_swears_total": 0, "assistant_nice_total": 0, "assistant_swears_total": 0,
            "please_count": 0, "nice_to_harsh": "N/A", "claude_nice_ratio": 0,
            "top_swears": [], "swears_by_hour": [0] * 24, "nice_by_hour": [0] * 24,
            "swear_peak_hour": 0, "swear_peak_count": 0, "nice_peak_hour": 12,
            "nice_peak_count": 0, "swear_example_quote": "", "total_user_msgs": 0,
            "swear_pct": 0,
        })

    # Machine split
    machine_counts = Counter()
    for ps in project_sessions:
        cwd = ps.get("cwd", "") or ""
        if cwd.startswith("/root"):
            machine_counts["AX41"] += 1
        elif cwd.startswith("/Users"):
            machine_counts["Mac"] += 1
        elif cwd.startswith("/home"):
            machine_counts["Linux"] += 1
        else:
            machine_counts["Other"] += 1
    d["machine_counts"] = machine_counts


def compute_aggregates(data):
    """Compute all metrics needed for the template. Delegates to focused helpers."""
    d = {}
    outcomes = data["outcomes"]

    _compute_scale(d, outcomes, data["session_count"])
    _compute_method(d, outcomes, data["project_sessions"],
                    data["all_user_texts"], data["session_word_counts"])
    _compute_reality(d, outcomes, data["error_sessions"],
                     data["loop_findings"], data["scoring_instances"], data["misuses"])
    _compute_relationship(d, data["tones"], data["project_sessions"])

    # Wasted cost (needs both tokens and cost computed)
    if d.get("total_tokens", 0) > 0 and d.get("total_cost", 0) > 0:
        cost_per_token = d["total_cost"] / d["total_tokens"]
        d["wasted_cost"] = round(d.get("wasted_tokens", 0) * cost_per_token)
    else:
        d["wasted_cost"] = 0

    return d


def compute_rules(d):
    """Compute top 3 rules from data, sorted by severity."""
    candidates = []

    median = d.get("median_words", 20)
    if median < 15:
        short_err = d.get("short_prompt_errors", 0)
        long_err = d.get("long_prompt_errors", 0)
        if short_err > long_err and long_err > 0:
            severity = (15 - median) / 15
            ratio = f"{short_err / long_err:.1f}x"
            desc = f"Terse prompts cause {ratio} more errors"
            candidates.append((severity, "Write longer prompts", desc))

    pct_perfect = d.get("pct_perfect", 0)
    if pct_perfect > 25:
        severity = pct_perfect / 100
        candidates.append((severity, "Demand evidence before 10/10",
                           f"{pct_perfect}% of perfect scores had bugs after"))

    switch_rate = d.get("switch_rate", 50)
    if switch_rate < 10:
        severity = (100 - switch_rate) / 100
        candidates.append((severity, "Force strategy switches",
                           f"Agent retried same approach {100 - switch_rate:.0f}% of the time"))

    loop_pct = 0
    if d.get("sessions", 0) > 0:
        loop_pct = d.get("sessions_with_loops", 0) / d["sessions"] * 100
    if loop_pct > 20:
        severity = loop_pct / 100
        candidates.append((severity, "Intervene at retry 3",
                           f"{loop_pct:.0f}% of sessions got stuck in loops"))

    misuse_total = d.get("misuse_total", 0)
    if misuse_total > 10:
        severity = min(misuse_total / 500, 1.0)
        candidates.append((severity, "Use the right tool",
                           f"{misuse_total} wrong tool usages detected"))

    # Sort by severity descending, take top 3
    candidates.sort(key=lambda x: x[0], reverse=True)
    rules = [(title, desc) for _, title, desc in candidates[:3]]

    # Fill to 3 if needed
    fallbacks = [
        ("Run tests before declaring done", "Verify your changes work before moving on"),
        ("Read the error message", "The fix is usually in the error text"),
        ("Check the file exists first", "Use Glob to verify paths before reading"),
    ]
    while len(rules) < 3:
        rules.append(fallbacks[len(rules)])

    return rules


ARCHETYPE_META = {
    "firefighter": {
        "name": "THE FIREFIGHTER",
        "line": "You spend more time fixing than building.",
        "share": "I'm a Firefighter coder",
    },
    "architect": {
        "name": "THE ARCHITECT",
        "line": "You think before you type.",
        "share": "I'm an Architect coder",
    },
    "speedrunner": {
        "name": "THE SPEEDRUNNER",
        "line": "Ship fast, fix later.",
        "share": "I'm a Speedrunner coder",
    },
    "perfectionist": {
        "name": "THE PERFECTIONIST",
        "line": "You want it right. Your AI wants it done.",
        "share": "I'm a Perfectionist coder",
    },
    "whisperer": {
        "name": "THE WHISPERER",
        "line": "You get results with kindness.",
        "share": "I'm a Whisperer coder",
    },
    "commander": {
        "name": "THE COMMANDER",
        "line": "Direct. Efficient. No pleasantries.",
        "share": "I'm a Commander coder",
    },
}


def compute_archetype(d, percentiles=None):
    """Compute a personality archetype from aggregated metrics.

    Returns (key, name, one_liner, share_text, stats_html).
    """
    scores = {}
    s = d.get("sessions", 1) or 1
    cats = d.get("session_categories", {})

    # Firefighter: mostly fixing
    fix_pct = cats.get("FIX", 0) / s * 100
    scores["firefighter"] = fix_pct * 0.6 + (100 - d.get("success_pct", 50)) * 0.4

    # Architect: deliberate builder
    build_pct = cats.get("BUILD", 0) / s * 100
    word_score = min(d.get("median_words", 0) / 30, 1) * 40
    scores["architect"] = build_pct * 0.4 + word_score + d.get("success_pct", 0) * 0.2

    # Speedrunner: fast and terse
    session_score = min(s / 300, 1) * 30
    terse_score = max(0, 15 - d.get("median_words", 15)) / 15 * 40
    scores["speedrunner"] = session_score + terse_score + d.get("deployments", 0) / max(s, 1) * 30

    # Perfectionist: high standards, gets stuck
    perfect_score = d.get("pct_perfect", 0) * 0.4
    loop_score = d.get("sessions_with_loops", 0) / s * 50
    scores["perfectionist"] = perfect_score + loop_score

    # Whisperer: kind and effective
    nice = d.get("niceness_score", 5)
    scores["whisperer"] = max(0, nice - 5) * 10 + d.get("success_pct", 0) * 0.3

    # Commander: terse and direct
    scores["commander"] = max(0, 8 - d.get("median_words", 8)) * 8 + max(0, 5 - nice) * 8

    key = max(scores, key=scores.get)
    meta = ARCHETYPE_META[key]

    # Build 3 supporting stat lines per archetype
    stats = _archetype_stats(key, d, cats, s, percentiles)
    stats_html = "\n".join(f'<div style="font-size:0.72rem; color:var(--text-muted); margin-top:0.3rem;">{line}</div>' for line in stats)

    return key, meta["name"], meta["line"], meta["share"], stats_html


def _pct_tag(percentiles, key):
    """Return ' (top X%)' string if percentile >= 75, else ''."""
    if not percentiles:
        return ""
    p = percentiles.get(key, 0)
    if p >= 75:
        return f" (top {100 - p}%)"
    return ""


def _archetype_stats(key, d, cats, s, percentiles=None):
    """Return 3 supporting stat lines for the given archetype."""
    if key == "firefighter":
        fix_pct = round(cats.get("FIX", 0) / s * 100)
        return [
            f"{fix_pct}% of sessions were FIX sessions",
            f'{d.get("median_words", 0)}-word median prompts',
            f'{d.get("retried_pct", 0)}% of errors: retried same approach',
        ]
    elif key == "architect":
        build_pct = round(cats.get("BUILD", 0) / s * 100)
        return [
            f"{build_pct}% of sessions were BUILD sessions",
            f'{d.get("median_words", 0)}-word median prompts (you give context)',
            f'{d.get("success_pct", 0)}% success rate{_pct_tag(percentiles, "success")}',
        ]
    elif key == "speedrunner":
        return [
            f"{s} sessions{_pct_tag(percentiles, 'sessions')}",
            f'{d.get("median_words", 0)}-word median prompts (terse)',
            f'{d.get("deployments", 0)} deploy events{_pct_tag(percentiles, "deployments")}',
        ]
    elif key == "perfectionist":
        return [
            f'{d.get("pct_perfect", 0)}% of self-ratings were 10/10',
            f'{d.get("sessions_with_loops", 0)} sessions stuck in retry loops',
            f'{d.get("bugs_after_high", 0)} bugs found right after "perfect" scores',
        ]
    elif key == "whisperer":
        return [
            f'{d.get("niceness_score", 0)}/10 niceness score',
            f'{d.get("please_count", 0)} "please"s said',
            f'{d.get("success_pct", 0)}% success rate with kindness{_pct_tag(percentiles, "success")}',
        ]
    else:  # commander
        return [
            f'{d.get("median_words", 0)}-word median prompts (commands, not context)',
            f'{d.get("niceness_score", 0)}/10 niceness score',
            f'{d.get("pct_short", 0):.0f}% of prompts under 10 words',
        ]


def generate_html(d, rules, archetype=None, percentiles=None):
    """Read template, do string replacements, return HTML."""
    pcts = percentiles or {}
    template = TEMPLATE_PATH.read_text()

    # Archetype defaults
    if archetype:
        arch_key, arch_name, arch_line, arch_share, arch_stats_html = archetype
    else:
        arch_key, arch_name, arch_line, arch_share, arch_stats_html = (
            "architect", "THE ARCHITECT", "You think before you type.", "I'm an Architect coder", ""
        )

    # ── Date range formatting ──
    start = d["start_date"]
    end = d["end_date"]
    if hasattr(start, "strftime"):
        date_range = f'{start.strftime("%b %d")} &ndash; {end.strftime("%b %d, %Y")}'
    else:
        date_range = "Date range unavailable"

    # ── Author ──
    author = AUTHOR_NAME or "Claude Code User"
    initials = "".join(w[0].upper() for w in author.split()[:2]) if author else "CC"

    # ── Timezone label ──
    if TIMEZONE_OFFSET == 0:
        tz_label = "UTC"
    elif TIMEZONE_OFFSET > 0:
        tz_label = f"UTC+{TIMEZONE_OFFSET}"
    else:
        tz_label = f"UTC{TIMEZONE_OFFSET}"

    # ── Sessions split bar ──
    mc = d["machine_counts"]
    total_mc = sum(mc.values()) or 1
    split_bar_html = ""
    split_labels_html = ""
    sorted_machines = mc.most_common()
    for i, (name, count) in enumerate(sorted_machines[:2]):
        pct = count / total_mc * 100
        opacity = 1.0 if i == 0 else 0.3
        radius_l = "6px" if i == 0 else "0"
        radius_r = "6px" if i == len(sorted_machines[:2]) - 1 else "0"
        split_bar_html += f'<div class="split-bar-seg" style="width:{pct:.1f}%;background:rgba(34,197,94,{opacity});border-radius:{radius_l} {radius_r} {radius_r} {radius_l};"></div>\n'
        split_labels_html += f"<span>{count} {name}</span>\n"

    # ── Projects bar chart ──
    projects_html = ""
    if d["top_projects"]:
        max_count = d["top_projects"][0][1]
        opacities = [0.95, 0.55, 0.4, 0.3, 0.22]
        for i, (name, count) in enumerate(d["top_projects"]):
            pct = count / max_count * 100
            opacity = opacities[i] if i < len(opacities) else 0.15
            bg = f"linear-gradient(90deg,rgba(34,197,94,0.7),rgba(34,197,94,{opacity}))" if i == 0 else f"rgba(34,197,94,{opacity})"
            projects_html += f'''<div class="bar-row">
      <span class="bar-rank">#{i + 1}</span>
      <span class="bar-label">{html_mod.escape(name)}</span>
      <div class="bar-track"><div class="bar-fill" data-width="{pct:.1f}%" style="width:0%;background:{bg};">{count}</div></div>
    </div>\n'''
        top_name = d["top_projects"][0][0]
        top_count = d["top_projects"][0][1]
        projects_detail = f"{top_count} sessions on {html_mod.escape(top_name)}"
    else:
        projects_detail = "No project data"

    # ── Style tagline ──
    median = d.get("median_words", 0)
    if median < 8:
        style_tagline = "You speak in commands."
    elif median <= 20:
        style_tagline = "Brief but clear."
    elif median <= 50:
        style_tagline = "You give context."
    else:
        style_tagline = "You write specifications."

    # ── Style example pills ──
    style_pills = ""
    for ex in d.get("prompt_examples", [])[:5]:
        style_pills += f'<span class="prompt-example">&quot;{html_mod.escape(ex)}&quot;</span>\n'

    # ── Money ──
    cost = d["total_cost"]
    if cost >= 1000:
        money_hero = f"${cost / 1000:.0f}K"
    else:
        money_hero = f"${cost:.0f}"

    # ── Error chart data ──
    CATEGORY_COLORS = {
        "COMMAND_FAILED": "#f43f5e",
        "SIBLING_ERROR": "#a855f7",
        "FILE_NOT_FOUND": "#3b82f6",
        "UNKNOWN": "#52525b",
        "NETWORK_ERROR": "#f59e0b",
        "USER_REJECTED": "#06b6d4",
        "FILE_NOT_READ": "#22c55e",
        "EDIT_FAILED": "#f97316",
        "FILE_TOO_LARGE": "#818cf8",
        "HOOK_BLOCKED": "#ec4899",
        "PERMISSION_DENIED": "#ef4444",
        "MCP_ERROR": "#14b8a6",
        "TOOL_ERROR": "#64748b",
        "TOOL_NOT_FOUND": "#78716c",
    }
    CATEGORY_LABELS = {
        "COMMAND_FAILED": "Command Failed",
        "SIBLING_ERROR": "Sibling Error",
        "FILE_NOT_FOUND": "File Not Found",
        "UNKNOWN": "Unknown",
        "NETWORK_ERROR": "Network Error",
        "USER_REJECTED": "User Rejected",
        "FILE_NOT_READ": "File Not Read",
        "EDIT_FAILED": "Edit Failed",
        "FILE_TOO_LARGE": "File Too Large",
        "HOOK_BLOCKED": "Hook Blocked",
        "PERMISSION_DENIED": "Permission Denied",
        "MCP_ERROR": "MCP Error",
        "TOOL_ERROR": "Tool Error",
        "TOOL_NOT_FOUND": "Tool Not Found",
    }

    ec = d["error_categories"]
    sorted_cats = ec.most_common()
    # Show top 7, group rest as "Other"
    error_chart_items = []
    for cat, val in sorted_cats[:7]:
        label = CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
        color = CATEGORY_COLORS.get(cat, "#52525b")
        error_chart_items.append(f"{{ label: '{label}', value: {val}, color: '{color}' }}")
    if len(sorted_cats) > 7:
        other_val = sum(v for _, v in sorted_cats[7:])
        other_count = len(sorted_cats) - 7
        error_chart_items.append(f"{{ label: 'Other ({other_count})', value: {other_val}, color: '#1e293b' }}")
    error_chart_js = "[\n    " + ",\n    ".join(error_chart_items) + "\n  ]"

    # ── Loops tagline ──
    wasted_m = d["wasted_tokens_m"]
    if wasted_m > 10:
        loops_tagline = "Your AI doesn't give up. That's the problem."
    elif wasted_m > 1:
        loops_tagline = "Some circles were walked."
    elif wasted_m > 0:
        loops_tagline = "Remarkably efficient."
    else:
        loops_tagline = "No loops detected."

    # ── Scoring stars ──
    median_score = d.get("median_score", 0)
    star_path = 'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z'
    stars_html = ""
    for i in range(10):
        cls = "star-filled" if i < median_score else "star-empty"
        stars_html += f'<svg viewBox="0 0 24 24"><path class="{cls}" d="{star_path}"/></svg>\n'

    # ── Scoring tagline ──
    if d["full_success_pct"] > 0 and d["median_score"] > 0:
        overestimation = (d["median_score"] / 10) / (d["full_success_pct"] / 100)
        if overestimation > 2:
            scoring_tagline = "Your AI thinks it's a 10. It's not."
        elif overestimation > 1.5:
            scoring_tagline = "Slightly optimistic."
        else:
            scoring_tagline = "Surprisingly well-calibrated."
    else:
        scoring_tagline = "Not enough scoring data."

    # ── Misuse pills ──
    misuse_pills_html = ""
    for name, count in d.get("misuse_top", []):
        misuse_pills_html += f'''<div class="tool-pill">
      <span>{name}</span>
      <span class="tool-count">{count}x</span>
    </div>\n'''

    # ── Success ring offset ──
    # stroke-dasharray is 565.49 (circumference of r=90 circle)
    # dashoffset = circumference * (1 - pct/100)
    circumference = 565.49
    success_pct = d.get("success_pct", 0)
    ring_offset = round(circumference * (1 - success_pct / 100), 2)

    # ── Swear pills ──
    swear_pills_html = ""
    for word, count in d.get("top_swears", []):
        censored = censor_word(word)
        swear_pills_html += f'<div class="swear-pill"><span class="censored">{censored}</span><span class="uncensored">{word}</span><span class="swear-count">{count}x</span></div>\n'

    # ── Niceness tagline ──
    ns = d.get("niceness_score", 5)
    if ns < 3:
        nice_tagline = "Drill sergeant."
    elif ns < 5:
        nice_tagline = "Tough but fair."
    elif ns < 7:
        nice_tagline = "Pleasant to work with."
    else:
        nice_tagline = "Suspiciously nice."

    # ── Tone clock tagline ──
    swear_h = d.get("swear_peak_hour", 0)
    nice_h = d.get("nice_peak_hour", 12)
    if swear_h >= 18:
        tone_tagline = "Polite by day, unhinged by evening"
    elif swear_h >= 12:
        tone_tagline = "Afternoon frustration is real"
    else:
        tone_tagline = "Morning rage, afternoon zen"

    # ── Rules HTML ──
    rules_html = ""
    for i, (title, desc) in enumerate(rules):
        rules_html += f'''<div class="rule-item">
      <div class="rule-num">{i + 1:02d}</div>
      <div class="rule-content">
        <div class="rule-title">{title}</div>
        <div class="rule-desc">{desc}</div>
      </div>
    </div>\n'''

    # ── Share grid ──
    # Row 1: 3 stats
    grid_row1 = [
        (fmt_compact(d["sessions"]), "sessions", "gradient-green"),
        (fmt_compact(d["hours"]), "hours", "gradient-purple"),
        (fmt_compact(d["loc"]), "LOC", "gradient-blue"),
    ]
    # Row 2: 2 stats (centered)
    grid_row2 = [
        (f'{d["tokens_display"]}{d["tokens_suffix"]}', "tokens", "gradient-cyan"),
        (f'{d["success_pct"]}%', "success", "gradient-green"),
    ]
    grid_row1_html = ""
    for val, label, gradient in grid_row1:
        grid_row1_html += f'''<div class="summary-cell">
        <div class="s-val {gradient}">{val}</div>
        <div class="s-lbl">{label}</div>
      </div>\n'''
    grid_row2_html = ""
    for val, label, gradient in grid_row2:
        grid_row2_html += f'''<div class="summary-cell">
        <div class="s-val {gradient}">{val}</div>
        <div class="s-lbl">{label}</div>
      </div>\n'''

    # ── Archetype icon SVGs ──
    ARCHETYPE_ICONS = {
        "firefighter": '<svg viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="1.5"><path d="M12 2c.5 3-1.5 5-1.5 8 0 2.5 2 4 3.5 4s2.5-1 2.5-3c0-1.5-.5-3-1-4.5M8 14c-1.5 0-2.5-1-2.5-3 0-1.5.5-3 1-4.5C7 3 9.5 2 12 2c0 2-3 4-3 7 0 2 1.5 3.5 3 4"/><path d="M12 22c-4 0-7-2.5-7-6 0-2 1-3.5 2-4.5M12 22c4 0 7-2.5 7-6 0-2-1-3.5-2-4.5"/></svg>',
        "architect": '<svg viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="1.5"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><path d="M10 6.5h4M6.5 10v4M17.5 10v4M10 17.5h4"/></svg>',
        "speedrunner": '<svg viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="1.5"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
        "perfectionist": '<svg viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2" fill="#22c55e"/></svg>',
        "whisperer": '<svg viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="1.5"><path d="M12 2l1.5 4.5L18 8l-4.5 1.5L12 14l-1.5-4.5L6 8l4.5-1.5L12 2zM5 16l.75 2.25L8 19l-2.25.75L5 22l-.75-2.25L2 19l2.25-.75L5 16zM19 14l.5 1.5L21 16l-1.5.5L19 18l-.5-1.5L17 16l1.5-.5L19 14z"/></svg>',
        "commander": '<svg viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="1.5"><path d="M4 17l6-6-6-6"/><path d="M12 19h8"/></svg>',
    }
    archetype_icon = ARCHETYPE_ICONS.get(arch_key, ARCHETYPE_ICONS["architect"])

    # ── Percentile badges ──
    # Generate badge HTML for each metric (only shown if >= 75th percentile)
    badge_map = {
        "sessions": pcts.get("sessions", 0),
        "hours": pcts.get("hours", 0),
        "loc": pcts.get("loc", 0),
        "tokens": pcts.get("tokens", 0),
        "cost": pcts.get("cost", 0),
        "success": pcts.get("success", 0),
        "deployments": pcts.get("deployments", 0),
    }
    badge_html = {}
    for key, pval in badge_map.items():
        if pval >= 75:
            badge_html[key] = f'<span class="percentile-badge" title="Estimated from community benchmarks">Top {100 - pval}%*</span>'
        else:
            badge_html[key] = ""

    overall_pct = pcts.get("overall", 0)
    if overall_pct >= 50:
        overall_line = (
            f'<div class="share-overall-pct"><span class="gradient-text">Top {100 - overall_pct}%</span> of Claude Code users</div>'
            f'<div class="percentile-disclaimer">Estimated from community benchmarks</div>'
        )
    else:
        overall_line = ""

    # ── Share text ──
    pct_text = f" (top {100 - overall_pct}%)" if overall_pct >= 50 else ""
    share_text = (
        f"I've coded more with AI than {100 - overall_pct}% of developers.\\n\\n"
        f"{arch_share}{pct_text}. {fmt_number(d['sessions'])} sessions, "
        f"{fmt_number(d['hours'])} hours, {fmt_compact(d['loc'])} LOC.\\n\\n"
        f"#ClaudeEntropy #ClaudeCode"
    )

    # ── Hours detail ──
    if d["longest_session_days"] >= 1:
        hours_detail = f"Longest session: {d['longest_session_days']} days (start to finish)"
    else:
        hours_detail = f"Longest session: {d['longest_session_hours']} hours"

    # ── Cross-card connection texts ──
    # Sessions category line
    sc = d.get("session_categories", {})
    total_sc = sum(sc.values()) or 1
    top_cats = sc.most_common(3)
    sessions_cat_parts = []
    for cat_name, cat_count in top_cats:
        sessions_cat_parts.append(f"{cat_count} {cat_name.lower()}")
    sessions_category_line = ", ".join(sessions_cat_parts) if sessions_cat_parts else ""

    # LOC split
    la = d.get("loc_added", 0)
    lr = d.get("loc_removed", 0)
    loc_split = f"{fmt_compact(la)} added, {fmt_compact(lr)} removed across {fmt_number(d['files_edited_count'])} files"

    # Ships detail
    cpd = d.get("commits_per_deploy", 0)
    ships_detail = f"{cpd} commits per deploy on average" if cpd > 0 else f"{fmt_number(d['bash_commands'])} bash commands executed"

    # Errors recovery
    sw_pct = d.get("switched_pct", 0)
    rt_pct = d.get("retried_pct", 0)
    errors_recovery = f"Recovered {sw_pct}% by switching approach. Retried same thing {rt_pct}%."

    # Money savings from loops
    wc = d.get("wasted_cost", 0)
    tc = d.get("total_cost", 0)
    if wc > 0 and tc > 0:
        wc_pct = round(wc / tc * 100)
        money_savings = f"~${wc} wasted on retry loops ({wc_pct}% of total)"
    else:
        money_savings = ""

    # Comparison multipliers vs median (p50) user - normalized to monthly rates
    days = max(1, d.get("days", 1))
    months = days / 30.0
    sessions_monthly = d.get("sessions", 0) / months
    tokens_monthly = d.get("total_tokens", 0) / months
    cost_monthly = d.get("total_cost", 0) / months
    tokens_vs = tokens_monthly / BENCHMARKS["tokens_monthly"][1][1]  # p50
    cost_vs = cost_monthly / max(1, BENCHMARKS["cost_monthly"][1][1])  # p50
    sessions_vs = sessions_monthly / max(1, BENCHMARKS["sessions_monthly"][1][1])  # p50

    def _vs_line(mult, metric_label):
        if mult >= 2:
            return f"{mult:.0f}x the average dev's {metric_label}"
        return ""

    tokens_vs_line = _vs_line(tokens_vs, "token usage")
    cost_vs_line = _vs_line(cost_vs, "API spend")
    sessions_vs_line = _vs_line(sessions_vs, "monthly sessions")

    # ── Fun real-world comparisons ──
    # Sessions: compare to something relatable
    sess = d.get("sessions", 0)
    if sess >= 500:
        sessions_comparison = f"That's more sessions than a therapist sees in a year"
    elif sess >= 300:
        every_h = max(1, round(days * 24 / sess))
        sessions_comparison = f"A new session every {'hour' if every_h == 1 else f'{every_h} hours'}, day and night"
    elif sess >= 100:
        sessions_comparison = f"{round(sess / max(1, days))} sessions per day, every day"
    elif sess >= 30:
        sessions_comparison = f"About {round(sess / max(1, days / 7))} sessions per week"
    else:
        sessions_comparison = ""

    # Hours: compare to real-world durations
    hrs = d.get("hours", 0)
    if hrs >= 8760:
        sessions_comparison_hours = f"More than a full year of coding, across parallel sessions"
    elif hrs >= 4000:
        sessions_comparison_hours = f"That's {round(hrs / 2000)} years of full-time work, run in parallel"
    elif hrs >= 2000:
        sessions_comparison_hours = f"A full year at a desk job, parallelized into {days} days"
    elif hrs >= 500:
        flights = round(hrs / 11)
        sessions_comparison_hours = f"Like {flights} flights from NYC to London, back to back"
    elif hrs >= 100:
        movies = round(hrs / 2.5)
        sessions_comparison_hours = f"You could have watched {movies} movies instead"
    else:
        sessions_comparison_hours = ""

    # LOC: compare to famous codebases/projects
    loc = d.get("loc", 0)
    if loc >= 1_000_000:
        sessions_comparison_loc = f"More code than the original Windows NT kernel"
    elif loc >= 500_000:
        sessions_comparison_loc = f"More than the entire source of Photoshop 1.0"
    elif loc >= 200_000:
        sessions_comparison_loc = f"That's roughly the size of the original Doom engine"
    elif loc >= 100_000:
        sessions_comparison_loc = f"Enough to write the Linux 0.01 kernel, twice"
    elif loc >= 50_000:
        sessions_comparison_loc = f"More lines than the Apollo 11 guidance computer"
    elif loc >= 10_000:
        sessions_comparison_loc = f"About the size of a full-featured iOS app"
    else:
        sessions_comparison_loc = ""

    # Ships: compare to real software teams
    commits = d.get("commits", 0)
    deploys = d.get("deployments", 0)
    if deploys >= 1000:
        sessions_comparison_ships = f"That's every push, preview, and production deploy"
    elif deploys >= 100:
        sessions_comparison_ships = f"A deploy event every {max(1, round(days * 24 / deploys))} hours on average"
    elif commits >= 500:
        sessions_comparison_ships = f"More commits than the average open-source project gets in a year"
    elif commits >= 100:
        sessions_comparison_ships = f"Averaging {round(commits / max(1, days))} commits per day"
    else:
        sessions_comparison_ships = ""

    # Time saved estimate (AI generates code ~10x faster, 80% of time is coding)
    hours_saved = round(d["hours"] * 0.8)
    if hours_saved > 0:
        time_saved = f"~{fmt_number(hours_saved)} hours of manual coding saved"
    else:
        time_saved = ""

    # Success by prompt length
    sp = d.get("short_prompt_success_pct", 0)
    lp = d.get("long_prompt_success_pct", 0)
    if sp > 0 and lp > 0:
        success_prompt_link = f"Detailed prompts: {lp}% success vs {sp}% terse"
    else:
        success_prompt_link = ""

    # Sessions tagline based on category split
    if top_cats:
        top_cat = top_cats[0][0]
        top_cat_pct = round(top_cats[0][1] / total_sc * 100)
        if top_cat == "FIX" and top_cat_pct > 40:
            sessions_tagline = "More fixing than building"
        elif top_cat == "BUILD" and top_cat_pct > 40:
            sessions_tagline = "Mostly greenfield"
        elif top_cat == "EXPLORE" and top_cat_pct > 30:
            sessions_tagline = "Explorer mode"
        else:
            sessions_tagline = "Balanced workflow"
    else:
        sessions_tagline = ""

    # LOC ratio tagline
    if la > 0 and lr > 0:
        ratio = la / lr
        if ratio >= 3:
            loc_tagline = f"{ratio:.0f}:1 add-to-delete ratio: you're building"
        elif ratio >= 1.5:
            loc_tagline = f"{ratio:.1f}:1 add-to-delete ratio: mostly building"
        else:
            loc_tagline = f"{ratio:.1f}:1 ratio: equal parts building and tearing down"
    else:
        loc_tagline = ""

    # ── Perform all replacements ──
    replacements = {
        # Meta tags
        "__META_SESSIONS__": fmt_number(d["sessions"]),
        "__META_TOKENS__": f'{d["tokens_display"]} {d["tokens_suffix"].lower()}illion',
        "__META_DAYS__": str(d["days"]),
        "__META_COST__": money_hero,
        "__META_HOURS__": fmt_number(d["hours"]),
        "__META_MULTIPLIER__": "",

        # Title card
        "__DATE_RANGE__": date_range,
        "__TITLE_META__": f'{d["days"]} days &middot; {fmt_number(d["sessions"])} sessions',
        "__AVATAR_INITIALS__": initials,
        "__AUTHOR_NAME__": author,

        # Sessions
        "__SESSIONS_COUNT__": str(d["sessions"]),
        "__SESSIONS_SPLIT_BAR__": split_bar_html,
        "__SESSIONS_SPLIT_LABELS__": split_labels_html,
        "__SESSIONS_CATEGORY_LINE__": sessions_category_line,
        "__SESSIONS_TAGLINE__": sessions_tagline,
        "__SESSIONS_VS_AVERAGE__": sessions_vs_line,
        "__SESSIONS_COMPARISON__": sessions_comparison,

        # Hours
        "__HOURS_COUNT__": str(d["hours"]),
        "__HOURS_WATERMARK__": f'{d["hours_days"]} DAYS',
        "__HOURS_ACCENT__": f'{d["hours_days"]} days of cumulative session time.',
        "__HOURS_DETAIL__": hours_detail,
        "__HOURS_COMPARISON__": sessions_comparison_hours,

        # LOC
        "__LOC_COUNT__": str(d["loc"]),
        "__LOC_DETAIL__": loc_split,
        "__LOC_TAGLINE__": loc_tagline,
        "__LOC_COMPARISON__": sessions_comparison_loc,

        # Ships
        "__COMMITS_COUNT__": str(d["commits"]),
        "__DEPLOYS_COUNT__": str(d["deployments"]),
        "__BASH_COMMANDS__": fmt_number(d["bash_commands"]),
        "__SHIPS_DETAIL__": ships_detail,
        "__SHIPS_COMPARISON__": sessions_comparison_ships,

        # Projects
        "__PROJECTS_BARS__": projects_html,
        "__PROJECTS_DETAIL__": projects_detail,

        # Style (7-Word Trap)
        "__STYLE_PCT_SHORT__": str(round(d["pct_short"])),
        "__STYLE_BAR_SHORT__": f'{d["pct_short"]:.1f}%',
        "__STYLE_BAR_LONG__": f'{100 - d["pct_short"]:.1f}%',
        "__STYLE_MEDIAN_WORDS__": str(d["median_words"]),
        "__STYLE_SHORT_ERRORS__": str(d["short_prompt_errors"]),
        "__STYLE_LONG_ERRORS__": str(d["long_prompt_errors"]),
        "__STYLE_ERROR_RATIO__": d.get("error_ratio_text", ""),
        "__STYLE_TAGLINE__": style_tagline,
        "__STYLE_EXAMPLE_PILLS__": style_pills,

        # Tokens
        "__TOKENS_COUNT__": str(d["tokens_display"]),
        "__TOKENS_SUFFIX__": d["tokens_suffix"],
        "__TOKENS_COMPARISON__": d["tokens_reading_comparison"],
        "__TOKENS_COMPARISON_SUB__": f'Processed in {d["days"]} days.',
        "__TOKENS_MESSAGES__": fmt_number(d["total_messages"]),
        "__TOKENS_VS_AVERAGE__": tokens_vs_line,

        # Money
        "__MONEY_HERO__": money_hero,
        "__MONEY_PAID__": f"${MONEY_PAID:,}" if MONEY_PAID else "",
        "__MONEY_MULTIPLIER__": f"{round(cost / MONEY_PAID)}x" if MONEY_PAID and cost > 0 else "",
        "__MONEY_DETAIL__": MONEY_DETAIL or "",
        "__MONEY_SUB_DISPLAY__": "flex" if MONEY_PAID else "none",
        "__MONEY_SAVINGS__": money_savings,
        "__COST_VS_AVERAGE__": cost_vs_line,
        "__TIME_SAVED__": time_saved,

        # Errors
        "__ERRORS_COUNT__": str(d["total_errors"]),
        "__ERRORS_DETAIL__": errors_recovery,
        "__ERROR_CHART_DATA__": error_chart_js,

        # Loops (Patience Tax)
        "__LOOPS_WASTED_TOKENS__": str(d["wasted_tokens_m"]),
        "__LOOPS_SWITCH_RATE__": str(d["switch_rate"]),
        "__LOOPS_DETAIL__": f'{d["loop_count"]} retry loops in {d["sessions_with_loops"]} session{"s" if d["sessions_with_loops"] != 1 else ""}',
        "__LOOPS_TAGLINE__": loops_tagline,

        # Scoring (Delusion Score)
        "__SCORING_PCT_PERFECT__": str(d["pct_perfect"]),
        "__SCORING_STARS__": stars_html,
        "__SCORING_AI_MEDIAN__": str(d["median_score"]),
        "__SCORING_REALITY_PCT__": str(d["full_success_pct"]),
        "__SCORING_BUGS_AFTER__": str(d["bugs_after_high"]),
        "__SCORING_TAGLINE__": scoring_tagline,

        # Misuse
        "__MISUSE_COUNT__": str(d["misuse_total"]),
        "__MISUSE_PILLS__": misuse_pills_html,

        # Success
        "__SUCCESS_PCT__": str(d["success_pct"]),
        "__RING_DASHOFFSET__": str(ring_offset),
        "__SUCCESS_DETAIL__": f'{d["full_success_pct"]}% full success &middot; {d["partial_success_pct"]}% partial success',
        "__SUCCESS_PROMPT_LINK__": success_prompt_link,

        # Niceness
        "__NICE_SCORE__": str(d["niceness_score"]),
        "__NICE_BAR_FILL__": str(round(d["niceness_score"] * 10)),
        "__NICE_WORDS_COUNT__": str(d["user_nice_total"]),
        "__NICE_PLEASE_COUNT__": str(d["please_count"]),
        "__NICE_TO_HARSH__": d["nice_to_harsh"],
        "__NICE_CLAUDE_RATIO__": f'Claude is {d["claude_nice_ratio"]}x nicer than you',
        "__NICE_CLAUDE_DETAIL__": f'{d["assistant_nice_total"]} nice words from Claude vs your {d["user_nice_total"]}',

        # Tone clock
        "__SWEARS_BY_HOUR__": str(d["swears_by_hour"]),
        "__NICE_BY_HOUR__": str(d["nice_by_hour"]),
        "__TONE_PEAK_SWEAR_HOUR__": hour_label(d["swear_peak_hour"]),
        "__TONE_PEAK_SWEAR_DETAIL__": f'{d["swear_peak_count"]} swears at {hour_label(d["swear_peak_hour"])} {tz_label}',
        "__TONE_PEAK_NICE_HOUR__": hour_label(d["nice_peak_hour"]),
        "__TONE_PEAK_NICE_DETAIL__": f'{d["nice_peak_count"]} nice words at {hour_label(d["nice_peak_hour"])} {tz_label}',
        "__TONE_TAGLINE__": tone_tagline,
        "__TONE_QUOTE__": f'"{html_mod.escape(d["swear_example_quote"])}"' if d["swear_example_quote"] else "",

        # Swears
        "__SWEARS_COUNT__": str(d["user_swears_total"]),
        "__SWEAR_PILLS__": swear_pills_html,
        "__SWEAR_AI_LINE__": f'The AI swore {d["assistant_swears_total"]} times too',
        "__SWEAR_DETAIL__": f'{d["swear_pct"]}% of messages. {"You\'re actually not that bad." if d["swear_pct"] < 1 else "Getting heated."}',

        # Rules / Archetype
        "__RULES_HEADING__": arch_name,
        "__ARCHETYPE_NAME__": arch_name,
        "__ARCHETYPE_LINE__": arch_line,
        "__ARCHETYPE_STATS__": arch_stats_html,
        "__RULES_ITEMS__": rules_html,

        # Percentile badges
        "__PERCENTILE_BADGE_sessions__": badge_html.get("sessions", ""),
        "__PERCENTILE_BADGE_hours__": badge_html.get("hours", ""),
        "__PERCENTILE_BADGE_loc__": badge_html.get("loc", ""),
        "__PERCENTILE_BADGE_tokens__": badge_html.get("tokens", ""),
        "__PERCENTILE_BADGE_cost__": badge_html.get("cost", ""),
        "__PERCENTILE_BADGE_success__": badge_html.get("success", ""),
        "__PERCENTILE_BADGE_deployments__": badge_html.get("deployments", ""),
        "__PERCENTILE_OVERALL__": overall_line,

        # Share
        "__SHARE_GRID_ROW1__": grid_row1_html,
        "__SHARE_GRID_ROW2__": grid_row2_html,
        "__ARCHETYPE_ICON__": archetype_icon,
        "__SHARE_TEXT__": share_text.replace("'", "\\'"),
        "__SHARE_URL__": SHARE_URL,
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, str(value))

    return html


def sanitize_html_for_publish(html):
    """Strip private data from HTML for public sharing.

    Returns (sanitized_html, strip_counts) where strip_counts tracks
    how many of each category were replaced.
    """
    counts = {"projects": 0, "prompts": 0, "swear_quote": 0, "swear_words": 0, "machines": 0}

    # 1. Project names: <span class="bar-label">ProjectName</span> -> "Project N"
    project_idx = [0]
    def _replace_project(m):
        project_idx[0] += 1
        counts["projects"] += 1
        return f'<span class="bar-label">Project {project_idx[0]}</span>'
    html = re.sub(r'<span class="bar-label">[^<]+</span>', _replace_project, html)

    # Also sanitize the projects_detail text: "N sessions on ProjectName"
    html = re.sub(
        r'(\d+ sessions on )[^<"]+',
        lambda m: m.group(1) + "Project 1",
        html,
    )

    # 2. Prompt examples: strip all <span class="prompt-example">...</span> from inside the container.
    #    Using a targeted approach that doesn't depend on div nesting.
    counts["prompts"] = len(re.findall(r'<span class="prompt-example">[^<]*</span>', html))
    html = re.sub(r'<span class="prompt-example">[^<]*</span>\s*', '', html)

    # 3. Swear quote: the __TONE_QUOTE__ replacement produces "quoted text" in a label-detail div
    orig = html
    html = re.sub(
        r'(<div class="fade-up label-detail">)"[^"]*"(</div>)',
        r'\1\2',
        html,
    )
    if html != orig:
        counts["swear_quote"] = 1

    # 4. Swear word pills: strip all individual pill divs from inside the container.
    #    Each pill: <div class="swear-pill">...<span class="censored">...</span>...</div>
    #    The container has nested divs, so we can't use .*? to match the outer div.
    #    Instead, strip each pill individually, then empty the container.
    counts["swear_words"] = len(re.findall(r'<div class="swear-pill">', html))
    # Remove each swear-pill div (single nesting level, no nested divs inside pills)
    html = re.sub(r'<div class="swear-pill">.*?</div>\s*', '', html, flags=re.DOTALL)

    # 5. PII: phone numbers, emails, IPs in visible text (not script/style)
    # Phone: require + prefix or explicit separator pattern to avoid catastrophic
    # backtracking on numeric JS data (Chart.js arrays, timestamps, etc.)
    html = re.sub(r'\+\d[\d\s\-()]{7,}\d', '[redacted]', html)
    html = re.sub(r'\b\d{3}[\s\-]\d{3}[\s\-]\d{4}\b', '[redacted]', html)
    html = re.sub(r'[\w.+-]+@[\w.-]+\.\w{2,}', '[redacted]', html)
    html = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[redacted]', html)

    # 6. Machine names: scoped to the sessions split labels container only.
    #    Container: <div ...flex...font-size:0.7rem...>LABELS</div>
    #    Labels are: <span>COUNT MACHINENAME</span>
    #    We extract the container, replace inside it, then reassemble.
    SPLIT_CONTAINER_RE = re.compile(
        r'(display:flex;\s*gap:2rem;[^"]*font-size:0\.7rem[^>]*>)(.*?)(</div>)',
        re.DOTALL,
    )
    machine_idx = [0]
    def _replace_split_container(container_match):
        prefix, inner, suffix = container_match.group(1), container_match.group(2), container_match.group(3)
        def _replace_machine(m):
            machine_idx[0] += 1
            counts["machines"] += 1
            return f'<span>{m.group(1)} Machine {machine_idx[0]}</span>'
        sanitized_inner = re.sub(
            r'<span>(\d+) ([A-Za-z][A-Za-z0-9_-]*)</span>',
            _replace_machine,
            inner,
        )
        return prefix + sanitized_inner + suffix
    html = SPLIT_CONTAINER_RE.sub(_replace_split_container, html)

    return html, counts


def verify_sanitization(html, project_names, machine_names, prompt_examples=None, swear_quote=None):
    """Defense-in-depth: verify no known private data survives in sanitized HTML.

    Returns list of leak descriptions. Empty list = clean.
    """
    leaks = []
    for name in project_names:
        if not name:
            continue
        # Check in bar-label context to avoid false positives from common words
        escaped = html_mod.escape(name)
        if f'bar-label">{escaped}</span>' in html or f'sessions on {escaped}' in html:
            leaks.append(f"project name '{name}' still present")
    for name in machine_names:
        if re.search(r'<span>\d+\s+' + re.escape(name) + r'</span>', html):
            leaks.append(f"machine name '{name}' still present")
    if prompt_examples:
        for ex in prompt_examples:
            escaped = html_mod.escape(ex)
            if f'prompt-example">&quot;{escaped}&quot;</span>' in html:
                leaks.append(f"prompt example '{ex}' still present")
    if swear_quote:
        escaped = html_mod.escape(swear_quote)
        if f'label-detail">"' in html and escaped in html:
            leaks.append("swear quote text still present")
    if '<span class="uncensored">' in html:
        leaks.append("uncensored swear spans still present")
    if '<span class="censored">' in html:
        leaks.append("censored swear spans still present")
    if re.search(r'<span class="prompt-example">', html):
        leaks.append("prompt example spans still present")
    return leaks


def publish_to_supabase(html, author_name, hash_val, metrics=None):
    """Upload HTML to Supabase wrapped_reports table. Returns the public URL."""
    import urllib.request
    import urllib.error

    SUPABASE_URL = os.environ.get("WRAPPED_SUPABASE_URL", "https://cbhbfutssknfjvgvavnt.supabase.co")
    SUPABASE_ANON_KEY = os.environ.get("WRAPPED_SUPABASE_KEY")
    if not SUPABASE_ANON_KEY:
        print("Warning: WRAPPED_SUPABASE_KEY not set. Publish skipped.")
        return None
    api_key = SUPABASE_ANON_KEY
    BASE_URL = "https://entropy.buildingopen.org"

    payload = {
        "hash": hash_val,
        "html_content": html,
        "author_name": author_name,
    }
    if metrics:
        payload["metrics"] = metrics

    try:
        req = urllib.request.Request(
            url=f"{SUPABASE_URL}/rest/v1/wrapped_reports",
            data=json.dumps(payload).encode(),
            headers={
                "apikey": api_key,
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            method="POST",
        )
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print(f"Warning: publish failed ({e.code}). Local HTML still generated.")
        return None
    except Exception as e:
        print(f"Warning: publish failed ({e}). Local HTML still generated.")
        return None

    return f"{BASE_URL}/entropy/{hash_val}"


def get_community_count():
    """Query how many reports have been published."""
    import urllib.request
    import urllib.error

    SUPABASE_URL = os.environ.get("WRAPPED_SUPABASE_URL", "https://cbhbfutssknfjvgvavnt.supabase.co")
    api_key = os.environ.get("WRAPPED_SUPABASE_KEY")
    if not api_key:
        return 0

    try:
        req = urllib.request.Request(
            url=f"{SUPABASE_URL}/rest/v1/wrapped_reports?select=hash",
            headers={
                "apikey": api_key,
                "Authorization": f"Bearer {api_key}",
                "Prefer": "count=exact",
            },
        )
        resp = urllib.request.urlopen(req)
        count = resp.headers.get("content-range", "").split("/")[-1]
        return int(count) if count and count != "*" else None
    except Exception:
        return None


def _step(label, value="", width=38):
    """Print a progress step with checkmark."""
    padding = max(1, width - len(label))
    check = "\u2713" if value else "\u2026"
    sys.stdout.write(f"  {check} {label}{'.' * padding} {value}\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Generate Claude Code Stats report")
    parser.add_argument("--publish", action="store_true", help="Publish to entropy.buildingopen.org (auto-sanitized)")
    parser.add_argument("--no-publish", action="store_true", help="(deprecated, now the default)")
    parser.add_argument("--sanitize", action="store_true", help="Anonymize local HTML (project names, prompts, swears, machines)")
    args = parser.parse_args()

    should_publish = args.publish
    should_sanitize_local = args.sanitize or os.environ.get("WRAPPED_SANITIZE") == "1"

    # Welcome banner
    print()
    print("  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510")
    print("  \u2502                                          \u2502")
    print("  \u2502   Claude Code Stats                      \u2502")
    print("  \u2502   Your AI coding story, visualized.       \u2502")
    print("  \u2502                                          \u2502")
    print("  \u2502   \u2714 100% local analysis                  \u2502")
    print("  \u2502   \u2714 No AI calls, no network requests      \u2502")
    print("  \u2502   \u2714 Pure Python, zero dependencies        \u2502")
    print("  \u2502                                          \u2502")
    print("  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518")
    print()

    data = collect_data()
    _step("Scanning sessions", f"{data['session_count']} found")

    d = compute_aggregates(data)
    _step("Analyzing patterns", f"{d['hours']} hours of coding")

    rules = compute_rules(d)
    percentiles = compute_percentiles(d)
    archetype = compute_archetype(d, percentiles)
    arch_key, arch_name, arch_line, arch_share, arch_stats_html = archetype
    _step("Computing your archetype", arch_name)

    author = AUTHOR_NAME or "Claude Code User"

    # Always generate hash (used for og:image placeholder even in local mode)
    raw = f"{author}:{datetime.now().isoformat()}"
    hash_val = hashlib.sha256(raw.encode()).hexdigest()[:8]

    if should_publish:
        global SHARE_URL
        SHARE_URL = f"https://entropy.buildingopen.org/entropy/{hash_val}"

    html = generate_html(d, rules, archetype, percentiles)

    # Replace OG image placeholder with dynamic URL or fallback
    if should_publish:
        html = html.replace("__SHARE_HASH__", hash_val)
    else:
        html = html.replace("__SHARE_HASH__", "preview")

    _step("Building report", "done")

    # Apply local sanitization if requested
    if should_sanitize_local:
        html, local_counts = sanitize_html_for_publish(html)
        _step("Sanitizing", "done")

    OUTPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(html)

    # Summary box
    money = os.environ.get("WRAPPED_MONEY_PAID", "200")
    money_detail = os.environ.get("WRAPPED_MONEY_DETAIL", "")
    overall_pct = percentiles.get("overall", 0)
    roi = round(d["total_cost"] / max(int(money), 1))
    pct_label = f"top {100 - overall_pct}%" if overall_pct >= 50 else ""

    def _box(text):
        """Print a line inside the summary box, auto-padded to 42 chars."""
        t = text[:40]
        print(f"  \u2502  {t:<40}\u2502")

    sessions_line = f"{d['sessions']} sessions, {d['hours']} hours, {fmt_compact(d['loc'])} LOC"
    tokens_line = f"{d['tokens_display']}{d['tokens_suffix']} tokens, ${d['total_cost']:,.0f} est. cost"
    roi_line = f"{roi}x return on ${money}/mo" + (f" ({money_detail})" if money_detail else "")
    out_line = f"\u2192 ./wrapped.html"

    print()
    print("  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510")
    _box(author)
    _box(arch_name)
    if pct_label:
        _box(pct_label)
    print(f"  \u2502{'':42}\u2502")
    _box(sessions_line)
    _box(tokens_line)
    _box(roi_line)
    print(f"  \u2502{'':42}\u2502")
    _box(out_line)
    print("  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518")
    print()

    if should_publish:
        # Sanitize before publishing (always, even if local was also sanitized)
        sanitized_html, strip_counts = sanitize_html_for_publish(
            # Re-sanitize from original if local was sanitized (idempotent)
            html
        )

        print("  Publishing (auto-sanitized)...")
        parts = []
        if strip_counts["projects"]:
            parts.append(f'{strip_counts["projects"]} project names')
        if strip_counts["prompts"]:
            parts.append(f'{strip_counts["prompts"]} prompt examples')
        if strip_counts["swear_quote"]:
            parts.append(f'{strip_counts["swear_quote"]} swear quote')
        if strip_counts["swear_words"]:
            parts.append(f'{strip_counts["swear_words"]} swear word pills')
        if strip_counts["machines"]:
            parts.append(f'{strip_counts["machines"]} machine names')
        if parts:
            print(f"    Stripped: {', '.join(parts)}")

        # Warn if nothing was stripped (template may have changed)
        if sum(strip_counts.values()) == 0:
            print("    Warning: nothing was stripped. Template may have changed.")

        # Defense-in-depth: verify no known private data survives
        project_names = [name for name, _ in d.get("top_projects", [])]
        machine_names = list(d.get("machine_counts", {}).keys())
        prompt_examples = d.get("prompt_examples", [])
        swear_quote = d.get("swear_example_quote", "")
        leaks = verify_sanitization(sanitized_html, project_names, machine_names, prompt_examples, swear_quote=swear_quote)
        if leaks:
            print(f"    ERROR: sanitization incomplete!")
            for leak in leaks:
                print(f"      - {leak}")
            print("    Aborting publish. Local HTML still available.")
            return

        metrics = {
            "archetype_key": arch_key,
            "archetype_name": arch_name,
            "sessions": d["sessions"],
            "hours": d["hours"],
            "loc": d["loc"],
            "tokens_display": f'{d["tokens_display"]}{d["tokens_suffix"]}',
            "success_pct": d["success_pct"],
            "overall_percentile": overall_pct,
            "author_name": author,
        }
        url = publish_to_supabase(sanitized_html, author, hash_val, metrics=metrics)
        if url:
            import urllib.parse
            pct_text_share = f" (top {100 - overall_pct}%)" if overall_pct >= 50 else ""
            share_msg = (
                f"I've coded more with AI than {100 - overall_pct}% of developers.\n\n"
                f"{arch_share}{pct_text_share}. {fmt_number(d['sessions'])} sessions, "
                f"{fmt_number(d['hours'])} hours, {fmt_compact(d['loc'])} LOC.\n\n"
                f"#ClaudeEntropy #ClaudeCode\n{url}"
            )
            encoded_text = urllib.parse.quote(share_msg)
            encoded_url = urllib.parse.quote(url)

            print()
            print("  ============================================")
            print()
            print(f"    LIVE: {url}")
            print()
            print(f"    Share on X:")
            print(f"    https://x.com/intent/tweet?text={encoded_text}")
            print()
            print(f"    Share on LinkedIn:")
            print(f"    https://linkedin.com/sharing/share-offsite/?url={encoded_url}")

            count = get_community_count()
            if count:
                print()
                print(f"    Developer #{count} to share their Entropy.")

            print()
            print("  ============================================")
        print()
    else:
        print("  ---")
        print("  Want to share? Run with --publish")
        print("  (project names and prompts are auto-sanitized)")
        print()


if __name__ == "__main__":
    main()
