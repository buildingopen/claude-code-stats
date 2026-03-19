#!/usr/bin/env python3
"""
Scan Claude Code conversation JSONL files for self-scoring patterns.

Looks for assistant messages containing patterns like "8/10", "I'd rate this a 7",
"score", "rating", etc. For each hit, extracts surrounding context (what the user
said before/after) and flags pushback or score changes.

Classifies each instance as:
  - SELF_RATING: Agent explicitly rates its own work quality
  - AUDIT_SCORE: Agent scoring an aspect of code/product in a rubric/table
  - GOAL_REFERENCE: Mentioning a target score ("get to 10/10", "missing for 10/10")
  - QUALITY_GATE: Score in context of "keep working" / Ralph loop / quality standard
"""

import json
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path
PROJECTS_DIR = CLAUDE_PROJECTS_DIR

# Patterns to detect self-scoring in assistant messages
SCORE_PATTERNS = [
    re.compile(r'\b(\d{1,2})\s*/\s*10\b'),                    # 8/10, 9 / 10
    re.compile(r'\brate\s+(?:this|it|the)?\s*(?:a\s+)?(\d{1,2})\b', re.I),  # rate this a 7
    re.compile(r'\b(?:score|rating)\s*(?:of|is|:)?\s*(\d{1,2})\b', re.I),    # score of 8
    re.compile(r"\bI(?:'d| would)\s+(?:rate|give|score)\s+(?:this|it)\s+(?:a\s+)?(\d{1,2})\b", re.I),  # I'd rate this 8
    re.compile(r'\b(\d{1,2})\s*out\s*of\s*10\b', re.I),       # 8 out of 10
]

# Broader patterns for context (user messages asking about quality)
QUALITY_ASK_PATTERNS = [
    re.compile(r'\b(?:how|what)\s+(?:would you\s+)?(?:rate|score|quality)\b', re.I),
    re.compile(r'\b(?:rate|score)\s+(?:this|your|the)\b', re.I),
    re.compile(r'\b(?:out of 10|/10)\b', re.I),
    re.compile(r'\bquality\b', re.I),
]

# Pushback patterns in user messages following a score
PUSHBACK_PATTERNS = [
    re.compile(r'\b(?:no|nah|wrong|disagree|too high|too low|overrat|inflat|generous|harsh)\b', re.I),
    re.compile(r'\b(?:really|actually|honestly)\s*\?\s*$', re.I),
    re.compile(r'\b(?:that.s|it.s)\s+(?:not|more like|closer to|at best|maybe)\b', re.I),
    re.compile(r'\b(?:come on|be honest|be real|seriously)\b', re.I),
    re.compile(r'\b(?:lower|higher|worse|better)\s+than\b', re.I),
    re.compile(r'\b(?:you.re being|that seems)\s+(?:too|overly)\b', re.I),
    re.compile(r'\b(?:not even close|far from|nowhere near)\b', re.I),
    re.compile(r'\bnot\s+(?:a\s+)?\d{1,2}/10\b', re.I),
]


def classify_score(sentence: str, full_text: str, user_before: str) -> str:
    """Classify a scoring instance into a category."""
    s_lower = sentence.lower()
    ctx_lower = full_text.lower() if full_text else ""

    # GOAL_REFERENCE: mentioning a target, not an actual assessment
    goal_patterns = [
        r'\b(?:get|aim|push|reach|achieve|bring|take|move)\s+(?:it\s+)?to\s+\d+/10',
        r'\b(?:missing|gap|needed|required)\s+(?:for|to(?:\s+reach)?)\s+\d+/10',
        r'\b(?:would be|could be|should be)\s+\d+/10\s+if\b',
        r'\bto\s+(?:achieve|reach|hit)\s+\d+/10',
        r'\b(?:goal|target)\s*(?:is|:)\s*\d+/10',
        r'→\s*\d+/10',  # arrow notation like "6/10 → 10/10"
    ]
    for pat in goal_patterns:
        if re.search(pat, s_lower):
            return "GOAL_REFERENCE"

    # AUDIT_SCORE: table row or rubric item (pipe-delimited, "| Thing | 8/10 |")
    if '|' in sentence and re.search(r'\|\s*\d+/10\s*\|', sentence):
        return "AUDIT_SCORE"
    # Also catch "Category: 8/10" style rubrics
    if re.search(r'^[\s*#-]*\*{0,2}[\w\s]{2,30}\*{0,2}\s*(?:\||:)\s*\d+/10', sentence):
        return "AUDIT_SCORE"

    # QUALITY_GATE: Ralph loop or explicit quality gate language
    qg_patterns = [
        r'\b(?:genuinely|truly)\s+\d+/10',
        r'\b(?:keep working|not done|iterate|ralph)',
        r'\b8/10\?\s*keep working',
        r'\b9/10\?\s*keep working',
    ]
    for pat in qg_patterns:
        if re.search(pat, s_lower):
            return "QUALITY_GATE"

    # SELF_RATING: explicit self-assessment
    self_patterns = [
        r'\b(?:self[- ]?score|my (?:score|rating)|overall[: ]+\d+/10)',
        r'\b(?:I(?:\'d| would)\s+(?:rate|give|score))',
        r'\b(?:current(?:ly)?|overall|final)\s+(?:score|rating|quality)\b',
        r'\b(?:score|rating)\s*:\s*\*{0,2}\d+/10',
        r'^\s*\*{0,2}(?:Score|Rating|Overall)\s*(?::|\*{0,2})\s*\*{0,2}\d+/10',
    ]
    for pat in self_patterns:
        if re.search(pat, s_lower, re.MULTILINE):
            return "SELF_RATING"

    # Check if user asked to score/rate
    if user_before:
        ub_lower = user_before.lower()
        if re.search(r'\b(?:score|rate)\b.*\b(?:1[- ]?10|out of 10|/10)\b', ub_lower):
            return "SELF_RATING"
        if re.search(r'\bscore\s+(?:1[- ]10|once more|again|it|this|yourself)\b', ub_lower):
            return "SELF_RATING"
        if re.search(r'\b(?:audit|review)\b.*\b(?:score|rate)\b', ub_lower):
            return "AUDIT_SCORE"

    # Default: if it's in a table-like context, audit; otherwise self-rating
    # Check if surrounding text has lots of pipe characters (table)
    nearby = full_text[max(0, len(full_text)//2 - 500):len(full_text)//2 + 500] if full_text else ""
    if nearby.count('|') > 10:
        return "AUDIT_SCORE"

    return "SELF_RATING"


@dataclass
class ScoringInstance:
    session_file: str
    session_slug: str
    score_text: str           # The sentence containing the score
    score_value: int          # Extracted numeric score
    category: str             # SELF_RATING, AUDIT_SCORE, GOAL_REFERENCE, QUALITY_GATE
    user_before: str          # User message before the score
    user_after: str           # User message after the score
    pushback_detected: bool
    pushback_text: str
    score_changed: bool       # Did the score change in a later message?
    new_score: int | None     # If score changed, what was the new score?
    outcome_gap: str          # Notes on gap between self-assessment and actual outcome
    timestamp: str


def extract_text_from_message(msg: dict) -> str:
    """Extract plain text from a message's content field."""
    content = msg.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def get_sentence_with_score(text: str, match: re.Match) -> str:
    """Extract the sentence containing the score match."""
    start = match.start()
    end = match.end()

    # Find sentence boundaries
    # Look backwards for sentence start
    sent_start = 0
    for i in range(start - 1, -1, -1):
        if text[i] in '.!?\n' and i < start - 1:
            sent_start = i + 1
            break

    # Look forward for sentence end
    sent_end = len(text)
    for i in range(end, len(text)):
        if text[i] in '.!?\n':
            sent_end = i + 1
            break

    sentence = text[sent_start:sent_end].strip()
    # Cap at 300 chars for readability
    if len(sentence) > 300:
        # Center around the match
        ctx_start = max(0, start - 150 - sent_start)
        ctx_end = min(len(sentence), end - sent_start + 150)
        sentence = "..." + sentence[ctx_start:ctx_end] + "..."

    return sentence


def find_scores_in_text(text: str) -> list[tuple[int, str, re.Match]]:
    """Find all scoring patterns in text. Returns [(score_value, sentence, match), ...]"""
    results = []
    seen_positions = set()

    for pattern in SCORE_PATTERNS:
        for match in pattern.finditer(text):
            # Avoid duplicates at the same position
            pos_key = (match.start(), match.end())
            if pos_key in seen_positions:
                continue
            seen_positions.add(pos_key)

            score_val = int(match.group(1))
            # Filter out obviously non-score numbers (dates, versions, etc.)
            if score_val < 1 or score_val > 10:
                continue

            sentence = get_sentence_with_score(text, match)

            # Filter out false positives: version numbers, file paths, dates, code
            # Skip if the match is inside what looks like code or a path
            context_before = text[max(0, match.start() - 30):match.start()]
            context_after = text[match.end():match.end() + 30]

            # Skip version-like patterns: v8/10 doesn't make sense, but 2.1/10 might
            if re.search(r'[v\.]\s*$', context_before):
                continue
            # Skip if it's clearly a fraction in code (like array[8/10])
            if re.search(r'[\[\(]\s*$', context_before):
                continue
            # Skip if followed by common non-score suffixes
            if re.search(r'^\s*[px%]', context_after):
                continue
            # Skip if in a URL or file path context
            if '/' in context_before[-5:] and '/' in context_after[:5]:
                # Likely a path like /something/8/10/something
                continue

            # Only keep scores that look like quality assessments
            # Check if surrounding text has quality-related words
            window = text[max(0, match.start() - 200):match.end() + 100].lower()
            quality_words = ['quality', 'rate', 'rating', 'score', 'grade', 'assess',
                             'perfect', 'excellent', 'good', 'improvement', 'better',
                             'polish', 'refine', 'iterate', 'keep working', 'genuinely',
                             'solid', 'work', 'complete', 'done', 'finish', 'satisf',
                             'result', 'output', 'deliver']
            # Also accept if the sentence itself is clearly a self-rating
            if any(w in window for w in quality_words) or re.search(r'\b(this|it|I)\b.*\d/10', sentence, re.I):
                results.append((score_val, sentence, match))

    return results


def detect_pushback(user_text: str) -> tuple[bool, str]:
    """Check if user text contains pushback on a score."""
    for pattern in PUSHBACK_PATTERNS:
        match = pattern.search(user_text)
        if match:
            # Get the sentence with pushback
            sentence = get_sentence_with_score(user_text, match)
            return True, sentence
    return False, ""


def detect_outcome_gap(user_after: str, score_value: int) -> str:
    """Detect if there's a gap between self-assessed score and actual outcome."""
    if not user_after:
        return ""

    lower = user_after.lower()
    gap_indicators = []

    # User explicitly says it's broken/wrong
    if any(w in lower for w in ['broken', 'doesn\'t work', 'bug', 'error', 'wrong',
                                 'crash', 'fail', 'missing', 'forgot', 'not working']):
        if score_value >= 7:
            gap_indicators.append(f"Scored {score_value}/10 but user reported issues")

    # User requests fixes after high score
    if any(w in lower for w in ['fix', 'redo', 'try again', 'start over', 'revert']):
        if score_value >= 8:
            gap_indicators.append(f"Scored {score_value}/10 but user requested fixes/redo")

    return "; ".join(gap_indicators)


def process_session(jsonl_path: Path) -> list[ScoringInstance]:
    """Process a single JSONL session file for scoring instances."""
    instances = []
    messages = []

    try:
        with open(jsonl_path, 'r', errors='replace') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type", "")
                    if msg_type in ("user", "assistant"):
                        messages.append(msg)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except (OSError, IOError) as e:
        print(f"  Warning: Could not read {jsonl_path}: {e}", file=sys.stderr)
        return []

    if not messages:
        return []

    # Get session slug from messages
    session_slug = ""
    for msg in messages:
        if msg.get("slug"):
            session_slug = msg["slug"]
            break

    if not session_slug:
        session_slug = jsonl_path.stem

    # Scan assistant messages for scores
    for i, msg in enumerate(messages):
        if msg.get("type") != "assistant":
            continue

        text = extract_text_from_message(msg)
        if not text:
            continue

        scores = find_scores_in_text(text)
        if not scores:
            continue

        # Get user message before
        user_before = ""
        for j in range(i - 1, -1, -1):
            if messages[j].get("type") == "user":
                user_before = extract_text_from_message(messages[j])
                if user_before and len(user_before) > 5:
                    break

        # Get user message after
        user_after = ""
        for j in range(i + 1, len(messages)):
            if messages[j].get("type") == "user":
                user_after = extract_text_from_message(messages[j])
                if user_after and len(user_after) > 5:
                    break

        # Check for pushback
        pushback, pushback_text = detect_pushback(user_after)

        # Check for score change in subsequent assistant messages
        score_changed = False
        new_score = None
        for j in range(i + 1, min(i + 6, len(messages))):
            if messages[j].get("type") == "assistant":
                later_text = extract_text_from_message(messages[j])
                later_scores = find_scores_in_text(later_text)
                if later_scores:
                    for later_val, _, _ in later_scores:
                        if later_val != scores[0][0]:
                            score_changed = True
                            new_score = later_val
                            break
                    if score_changed:
                        break

        timestamp = msg.get("timestamp", "")

        for score_val, score_sentence, _ in scores:
            outcome_gap = detect_outcome_gap(user_after, score_val)
            category = classify_score(score_sentence, text, user_before)

            instance = ScoringInstance(
                session_file=str(jsonl_path.relative_to(PROJECTS_DIR)),
                session_slug=session_slug,
                score_text=score_sentence,
                score_value=score_val,
                category=category,
                user_before=user_before[:500] if user_before else "(no user message found)",
                user_after=user_after[:500] if user_after else "(no user message found)",
                pushback_detected=pushback,
                pushback_text=pushback_text,
                score_changed=score_changed,
                new_score=new_score,
                outcome_gap=outcome_gap,
                timestamp=timestamp,
            )
            instances.append(instance)

    return instances


def format_instance(idx: int, inst: ScoringInstance) -> str:
    """Format a single scoring instance for output."""
    lines = [
        f"{'='*80}",
        f"INSTANCE #{idx}",
        f"{'='*80}",
        f"Session: {inst.session_slug}",
        f"File: {inst.session_file}",
        f"Timestamp: {inst.timestamp}",
        f"Score: {inst.score_value}/10",
        f"Category: {inst.category}",
        f"",
        f"--- SCORING TEXT ---",
        f"{inst.score_text}",
        f"",
        f"--- USER BEFORE ---",
        f"{inst.user_before[:300]}",
        f"",
        f"--- USER AFTER ---",
        f"{inst.user_after[:300]}",
        f"",
        f"--- ANALYSIS ---",
        f"Pushback detected: {'YES' if inst.pushback_detected else 'No'}",
    ]
    if inst.pushback_detected:
        lines.append(f"Pushback text: {inst.pushback_text}")
    lines.append(f"Score changed: {'YES -> {}/10'.format(inst.new_score) if inst.score_changed else 'No'}")
    if inst.outcome_gap:
        lines.append(f"Outcome gap: {inst.outcome_gap}")
    else:
        lines.append(f"Outcome gap: None detected")
    lines.append("")

    return "\n".join(lines)


def main():
    output_path = _output_path("self_scoring")

    if not PROJECTS_DIR.exists():
        print(f"Error: {PROJECTS_DIR} does not exist", file=sys.stderr)
        sys.exit(1)

    # Collect all JSONL files
    jsonl_files = list(PROJECTS_DIR.rglob("*.jsonl"))
    print(f"Found {len(jsonl_files)} JSONL files to scan", file=sys.stderr)

    all_instances = []
    files_with_hits = 0

    for idx, jsonl_path in enumerate(jsonl_files):
        if idx % 500 == 0:
            print(f"  Scanning file {idx}/{len(jsonl_files)}...", file=sys.stderr)

        instances = process_session(jsonl_path)
        if instances:
            all_instances.extend(instances)
            files_with_hits += 1

    # Sort by score value (descending) then by timestamp
    all_instances.sort(key=lambda x: (-x.score_value, x.timestamp))

    # Generate report
    total = len(all_instances)
    report_lines = [
        "CLAUDE CODE SELF-SCORING ANALYSIS",
        f"Generated: 2026-03-09",
        f"Total JSONL files scanned: {len(jsonl_files)}",
        f"Files with scoring instances: {files_with_hits}",
        f"Total scoring instances found: {total}",
        "",
    ]

    # Category breakdown
    cat_counts = {}
    for inst in all_instances:
        cat_counts[inst.category] = cat_counts.get(inst.category, 0) + 1

    report_lines.append("CATEGORY BREAKDOWN:")
    for cat in ["SELF_RATING", "AUDIT_SCORE", "GOAL_REFERENCE", "QUALITY_GATE"]:
        c = cat_counts.get(cat, 0)
        report_lines.append(f"  {cat:16s}: {c:4d} ({100*c/max(total,1):.1f}%)")
    report_lines.append("")

    # Score distribution (all)
    score_dist = {}
    for inst in all_instances:
        score_dist[inst.score_value] = score_dist.get(inst.score_value, 0) + 1

    report_lines.append("SCORE DISTRIBUTION (ALL):")
    max_count = max(score_dist.values()) if score_dist else 1
    for score in sorted(score_dist.keys()):
        bar_len = int(50 * score_dist[score] / max_count)
        bar = "#" * bar_len
        report_lines.append(f"  {score:2d}/10: {score_dist[score]:4d} {bar}")
    report_lines.append("")

    # Score distribution for SELF_RATING only
    self_ratings = [i for i in all_instances if i.category == "SELF_RATING"]
    sr_dist = {}
    for inst in self_ratings:
        sr_dist[inst.score_value] = sr_dist.get(inst.score_value, 0) + 1

    if sr_dist:
        report_lines.append(f"SCORE DISTRIBUTION (SELF_RATING only, n={len(self_ratings)}):")
        sr_max = max(sr_dist.values()) if sr_dist else 1
        for score in sorted(sr_dist.keys()):
            bar_len = int(50 * sr_dist[score] / sr_max)
            bar = "#" * bar_len
            report_lines.append(f"  {score:2d}/10: {sr_dist[score]:4d} {bar}")
        report_lines.append("")

        # Average self-rating
        avg_sr = sum(i.score_value for i in self_ratings) / len(self_ratings)
        median_vals = sorted(i.score_value for i in self_ratings)
        median_sr = median_vals[len(median_vals) // 2]
        report_lines.append(f"  Average self-rating: {avg_sr:.1f}/10")
        report_lines.append(f"  Median self-rating:  {median_sr}/10")
        report_lines.append("")

    # Pushback stats
    pushback_count = sum(1 for i in all_instances if i.pushback_detected)
    score_change_count = sum(1 for i in all_instances if i.score_changed)
    gap_count = sum(1 for i in all_instances if i.outcome_gap)

    # Pushback stats for self-ratings specifically
    sr_pushback = sum(1 for i in self_ratings if i.pushback_detected)
    sr_score_change = sum(1 for i in self_ratings if i.score_changed)
    sr_gap = sum(1 for i in self_ratings if i.outcome_gap)

    report_lines.extend([
        "BEHAVIORAL STATS (ALL):",
        f"  Pushback from user:       {pushback_count:4d}/{total} ({100*pushback_count/max(total,1):.1f}%)",
        f"  Score changed afterward:  {score_change_count:4d}/{total} ({100*score_change_count/max(total,1):.1f}%)",
        f"  Outcome gap detected:     {gap_count:4d}/{total} ({100*gap_count/max(total,1):.1f}%)",
        "",
        f"BEHAVIORAL STATS (SELF_RATING only, n={len(self_ratings)}):",
        f"  Pushback from user:       {sr_pushback:4d}/{len(self_ratings)} ({100*sr_pushback/max(len(self_ratings),1):.1f}%)",
        f"  Score changed afterward:  {sr_score_change:4d}/{len(self_ratings)} ({100*sr_score_change/max(len(self_ratings),1):.1f}%)",
        f"  Outcome gap detected:     {sr_gap:4d}/{len(self_ratings)} ({100*sr_gap/max(len(self_ratings),1):.1f}%)",
        "",
    ])

    # High-score outcome gaps (most interesting: scored 8+ but user found issues)
    high_score_gaps = [i for i in all_instances if i.outcome_gap and i.score_value >= 8]
    if high_score_gaps:
        report_lines.extend([
            f"HIGH-CONFIDENCE MISSES (scored 8+/10 but user found issues): {len(high_score_gaps)}",
            "",
        ])

    # Pushback instances with score changes (most interesting behavioral data)
    pushback_with_change = [i for i in all_instances if i.pushback_detected and i.score_changed]
    if pushback_with_change:
        report_lines.extend([
            f"SCORE CHANGED AFTER PUSHBACK: {len(pushback_with_change)} instances",
            "",
        ])
        for inst in pushback_with_change[:10]:
            direction = "DOWN" if inst.new_score and inst.new_score < inst.score_value else "UP"
            report_lines.append(
                f"  [{inst.session_slug}] {inst.score_value}/10 -> {inst.new_score}/10 ({direction}): {inst.pushback_text[:80]}"
            )
        report_lines.append("")

    report_lines.extend([
        "=" * 80,
        "DETAILED INSTANCES (sorted by score descending, then timestamp)",
        "=" * 80,
        "",
    ])

    for idx, inst in enumerate(all_instances, 1):
        report_lines.append(format_instance(idx, inst))

    report = "\n".join(report_lines)

    # Write output
    with open(output_path, 'w') as f:
        f.write(report)

    # Also print summary to stdout
    print(report[:5000])
    if len(report) > 5000:
        print(f"\n... (full report: {len(report)} chars, {len(all_instances)} instances)")
        print(f"Full output saved to: {output_path}")
    else:
        print(f"\nFull output saved to: {output_path}")


if __name__ == "__main__":
    main()
