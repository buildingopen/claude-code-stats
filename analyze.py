#!/usr/bin/env python3
"""
Analyze Claude Code transcripts using Gemini for pattern detection.
Sends extracted conversation data to Gemini and gets back insights
about prompting quality, common issues, and instruction effectiveness.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types

from extract import (
    extract_conversation,
    format_conversation_for_analysis,
    list_conversations,
)


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if not GEMINI_API_KEY:
    print("ERROR: Set GEMINI_API_KEY or GOOGLE_API_KEY environment variable")
    sys.exit(1)

ANALYSIS_PROMPT = """You are an expert at analyzing human-AI coding agent interactions. You're reviewing transcripts from Claude Code sessions (an AI coding assistant used in the terminal).

Your job is to find actionable patterns, issues, and insights. Be specific and cite evidence from the transcripts.

Analyze the following {count} conversation transcripts and produce a report covering:

## 1. FAILURE PATTERNS
- Where did the agent go wrong? (wrong assumptions, retry loops, hallucinated paths, wrong tools)
- What caused tool rejections? Were they avoidable?
- Were there error cascades (one mistake leading to multiple follow-up errors)?
- Did the agent ever get stuck in a loop doing the same thing repeatedly?

## 2. INSTRUCTION COMPLIANCE
- Did the agent follow the user's CLAUDE.md instructions? (if visible in system messages)
- Which rules were violated? Which were followed well?
- Were there cases where better instructions would have prevented mistakes?

## 3. PROMPTING QUALITY
- How clear/effective were the user's prompts?
- Did vague prompts lead to wasted effort?
- Were there cases where the user had to repeat themselves or correct the agent?
- What prompt patterns led to the best outcomes?

## 4. EFFICIENCY
- Token waste: where were tokens spent unproductively? (reading files that weren't needed, generating code that got rejected, unnecessary explanations)
- Tool usage: were the right tools used? (e.g., Bash instead of Read, excessive file reads)
- Were there tasks that could have been done faster with a different approach?

## 5. TIME SINKS
- Which types of tasks took disproportionately long?
- What patterns correlate with long sessions?

## 6. TOP 5 ACTIONABLE RECOMMENDATIONS
Based on everything above, what are the top 5 specific, actionable changes the user could make to their workflow, instructions (CLAUDE.md), or prompting style to get better results?

Be specific. Reference actual examples from the transcripts. Don't be generic.

---

TRANSCRIPTS:

{transcripts}
"""

BATCH_ANALYSIS_PROMPT = """You are analyzing aggregate statistics from {count} Claude Code sessions. These are summary stats, not full transcripts.

Analyze these patterns and provide insights:

## SESSION STATISTICS:
{stats}

Provide:
1. **Usage Patterns**: When and how is the tool being used? Session length distribution, tool preferences.
2. **Error Hotspots**: Which sessions had the most errors/rejections? What correlates with high error rates?
3. **Efficiency Metrics**: Token usage per session, tool call ratios, estimated productivity.
4. **Trends**: Any patterns over time? Improving or degrading?
5. **Recommendations**: Top 3 actionable changes based on the aggregate data.

Be data-driven. Cite specific numbers.
"""


def analyze_with_gemini(prompt, model_name="gemini-3-flash-preview"):
    """Send analysis prompt to Gemini and return response."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=16384,
            temperature=0.3,
            thinking_config=types.ThinkingConfig(thinking_budget=8192),
        ),
    )
    return response.text


def deep_analysis(limit=5, min_size_kb=500, max_size_kb=None, model_name="gemini-3-flash-preview", include_subagents=False):
    """Run deep analysis on recent large conversations."""
    size_desc = f"min {min_size_kb}KB"
    if max_size_kb:
        size_desc += f", max {max_size_kb}KB"
    print(f"Finding {limit} recent conversations ({size_desc})...")
    convos = list_conversations(min_size_kb=min_size_kb, max_size_kb=max_size_kb, limit=limit, include_subagents=include_subagents)

    if not convos:
        print("No conversations found matching criteria.")
        return

    print(f"Found {len(convos)} conversations. Extracting...")
    transcripts = []
    for mtime, size, fp in convos:
        print(f"  Extracting {fp.name} ({size/1024/1024:.1f}MB)...")
        convo = extract_conversation(fp)
        text = format_conversation_for_analysis(convo, max_messages=150)
        transcripts.append(text)

    combined = "\n\n" + ("=" * 80 + "\n\n").join(transcripts)

    # Check total size - Gemini can handle ~1M tokens but let's be reasonable
    char_count = len(combined)
    print(f"\nTotal transcript size: {char_count:,} chars (~{char_count//4:,} tokens)")

    if char_count > 2_000_000:
        print("Transcripts too large, truncating to ~2M chars...")
        combined = combined[:2_000_000]

    prompt = ANALYSIS_PROMPT.format(count=len(convos), transcripts=combined)

    print(f"\nSending to Gemini ({model_name})...")
    start = time.time()
    result = analyze_with_gemini(prompt, model_name)
    elapsed = time.time() - start

    print(f"Analysis complete in {elapsed:.1f}s\n")
    print("=" * 80)
    print(result)
    print("=" * 80)

    # Save report
    report_dir = Path(__file__).parent / "reports"
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / f"analysis-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    with open(report_file, "w") as f:
        f.write(f"# Transcript Analysis Report\n")
        f.write(f"Date: {datetime.now().isoformat()}\n")
        f.write(f"Sessions analyzed: {len(convos)}\n")
        f.write(f"Model: {model_name}\n\n")
        f.write(result)
    print(f"\nReport saved to: {report_file}")

    return result


def batch_stats(limit=50, min_size_kb=50, model_name="gemini-3-flash-preview", include_subagents=False):
    """Run aggregate stats analysis across many sessions."""
    print(f"Collecting stats from up to {limit} conversations...")
    convos = list_conversations(min_size_kb=min_size_kb, limit=limit, include_subagents=include_subagents)

    stats_list = []
    for mtime, size, fp in convos:
        convo = extract_conversation(fp)
        meta = convo["metadata"]
        s = convo["stats"]
        stats_list.append({
            "slug": meta.get("slug", fp.name),
            "date": meta.get("start_time", "?")[:10],
            "model": meta.get("model", "?"),
            "duration_min": s.get("duration_minutes"),
            "messages": s["message_count"],
            "errors": s["errors"],
            "rejections": s["rejections"],
            "input_tokens": s["total_input_tokens"],
            "output_tokens": s["total_output_tokens"],
            "tools": s["tool_usage"],
            "size_mb": round(size / 1024 / 1024, 1),
        })

    stats_text = json.dumps(stats_list, indent=2)
    prompt = BATCH_ANALYSIS_PROMPT.format(count=len(stats_list), stats=stats_text)

    print(f"\nSending {len(stats_list)} session stats to Gemini ({model_name})...")
    start = time.time()
    result = analyze_with_gemini(prompt, model_name)
    elapsed = time.time() - start

    print(f"Analysis complete in {elapsed:.1f}s\n")
    print("=" * 80)
    print(result)
    print("=" * 80)

    # Save report
    report_dir = Path(__file__).parent / "reports"
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / f"batch-stats-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    with open(report_file, "w") as f:
        f.write(f"# Batch Stats Analysis Report\n")
        f.write(f"Date: {datetime.now().isoformat()}\n")
        f.write(f"Sessions analyzed: {len(stats_list)}\n")
        f.write(f"Model: {model_name}\n\n")
        f.write(result)
    print(f"\nReport saved to: {report_file}")

    return result


def local_stats(limit=100, min_size_kb=50, include_subagents=False):
    """Run local-only stats analysis (no Gemini call)."""
    convos = list_conversations(min_size_kb=min_size_kb, limit=limit, include_subagents=include_subagents)

    total_tokens_in = 0
    total_tokens_out = 0
    total_errors = 0
    total_rejections = 0
    total_messages = 0
    total_duration = 0
    tool_totals = {}
    model_counts = {}
    sessions_by_date = {}
    error_sessions = []

    for mtime, size, fp in convos:
        convo = extract_conversation(fp)
        meta = convo["metadata"]
        s = convo["stats"]

        total_tokens_in += s["total_input_tokens"]
        total_tokens_out += s["total_output_tokens"]
        total_errors += s["errors"]
        total_rejections += s["rejections"]
        total_messages += s["message_count"]
        if s["duration_minutes"]:
            total_duration += s["duration_minutes"]

        for tool, count in s["tool_usage"].items():
            tool_totals[tool] = tool_totals.get(tool, 0) + count

        model = meta.get("model", "unknown")
        model_counts[model] = model_counts.get(model, 0) + 1

        date = (meta.get("start_time") or "")[:10]
        if date:
            sessions_by_date[date] = sessions_by_date.get(date, 0) + 1

        if s["errors"] > 5:
            error_sessions.append({
                "slug": meta.get("slug", fp.name),
                "errors": s["errors"],
                "rejections": s["rejections"],
                "duration": s["duration_minutes"],
            })

    print(f"\n{'='*60}")
    print(f"  CLAUDE CODE USAGE STATS ({len(convos)} sessions)")
    print(f"{'='*60}\n")

    if not convos:
        print("No conversations found matching criteria.")
        return

    print(f"Total sessions:      {len(convos)}")
    print(f"Total messages:      {total_messages:,}")
    print(f"Total duration:      {total_duration:.0f} min ({total_duration/60:.1f} hrs)")
    print(f"Avg session:         {total_duration/len(convos):.0f} min")
    print(f"Total tokens in:     {total_tokens_in:,}")
    print(f"Total tokens out:    {total_tokens_out:,}")
    print(f"Total errors:        {total_errors}")
    print(f"Total rejections:    {total_rejections}")
    print(f"Error rate:          {total_errors/total_messages*100:.1f}% of messages")

    print(f"\nTool usage:")
    for tool, count in sorted(tool_totals.items(), key=lambda x: -x[1]):
        print(f"  {count:>6}  {tool}")

    print(f"\nModels used:")
    for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
        print(f"  {count:>4}  {model}")

    print(f"\nSessions by date (recent):")
    for date in sorted(sessions_by_date.keys(), reverse=True)[:15]:
        count = sessions_by_date[date]
        bar = "#" * count
        print(f"  {date}  {count:>3}  {bar}")

    if error_sessions:
        print(f"\nHigh-error sessions (>5 errors):")
        error_sessions.sort(key=lambda x: -x["errors"])
        for s in error_sessions[:10]:
            slug = s['slug'] or 'unknown'
            dur = s['duration'] if s['duration'] is not None else '?'
            print(f"  {slug:40s}  errors={s['errors']:>3}  rej={s['rejections']:>2}  {dur} min")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Analyze Claude Code transcripts")
    parser.add_argument("mode", choices=["deep", "batch", "local"],
                        help="Analysis mode: deep (few sessions, full transcripts), "
                             "batch (many sessions, stats only), local (no Gemini)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Number of conversations (default: 5 for deep, 50 for batch, 100 for local)")
    parser.add_argument("--min-size", type=int, default=None,
                        help="Minimum file size in KB (default: 500 for deep, 50 for batch/local)")
    parser.add_argument("--max-size", type=int, default=None,
                        help="Maximum file size in KB (e.g., 10240 for 10MB)")
    parser.add_argument("--model", type=str, default="gemini-3-flash-preview",
                        help="Gemini model to use")
    parser.add_argument("--include-subagents", action="store_true",
                        help="Include subagent conversations in analysis")
    args = parser.parse_args()

    if args.mode == "deep":
        deep_analysis(
            limit=args.limit or 5,
            min_size_kb=args.min_size or 500,
            max_size_kb=args.max_size,
            model_name=args.model,
            include_subagents=args.include_subagents,
        )
    elif args.mode == "batch":
        batch_stats(
            limit=args.limit or 50,
            min_size_kb=args.min_size or 50,
            model_name=args.model,
            include_subagents=args.include_subagents,
        )
    elif args.mode == "local":
        local_stats(
            limit=args.limit or 100,
            min_size_kb=args.min_size or 50,
            include_subagents=args.include_subagents,
        )
