#!/usr/bin/env python3
"""
Analyze communication tone in Claude Code conversations.

Extracts:
- Swear word usage (by user and assistant)
- Politeness/niceness indicators (please, thanks, sorry, etc.)
- Overall tone score per session
- Frustration vs appreciation ratio
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path, find_sessions
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path, find_sessions

OUTPUT_PATH = _output_path("communication_tone")

# Swear words / profanity (common English)
SWEAR_WORDS = {
    "fuck", "fucking", "fucked", "fucker", "fucks",
    "shit", "shitty", "shits", "bullshit",
    "damn", "damned", "dammit", "goddamn",
    "ass", "asshole", "asses",
    "hell",
    "crap", "crappy",
    "bastard", "bastards",
    "bitch", "bitches",
    "wtf", "stfu", "lmao", "lmfao",
    "piss", "pissed",
    "suck", "sucks", "sucked",
}

# Words that look like swears but aren't in code context
SWEAR_EXCEPTIONS = {
    "assert", "assertion", "class", "pass", "shell", "crashes",
    "assess", "assessment", "asset", "assets", "assign", "assigned",
    "assume", "assumed", "assist", "assistant", "associate",
    "assembled", "assembly", "bass", "bypass", "compass", "embarrass",
    "harass", "surpass", "trespass", "amass", "sass", "mass",
    "classic", "massage", "passage", "passenger",
    "success", "successive", "successor",
    "cassette", "chassis", "hassle",
}

# Niceness / politeness words
NICE_WORDS = {
    "please", "thanks", "thank", "appreciate", "appreciated",
    "sorry", "apologies", "apologize",
    "great", "awesome", "excellent", "wonderful", "amazing", "fantastic",
    "perfect", "beautiful", "brilliant", "impressive", "incredible",
    "nice", "cool", "love", "loved",
    "well done", "good job", "great job", "nice work", "good work",
}

# Single-word nice words for word-boundary matching
NICE_SINGLE = {
    "please", "thanks", "thank", "appreciate", "appreciated",
    "sorry", "apologies", "apologize",
    "great", "awesome", "excellent", "wonderful", "amazing", "fantastic",
    "perfect", "beautiful", "brilliant", "impressive", "incredible",
    "nice", "cool", "love", "loved",
}

# Multi-word nice phrases
NICE_PHRASES = [
    "well done", "good job", "great job", "nice work", "good work",
    "thank you", "thanks for", "thanks a lot", "much appreciated",
]

# Harsh/negative words (beyond swears)
HARSH_WORDS = {
    "stupid", "idiot", "dumb", "useless", "terrible", "horrible",
    "awful", "garbage", "trash", "worst", "ruined",
    "pathetic", "incompetent", "ridiculous", "absurd",
    "annoying", "frustrating", "frustrated",
    # "broken" excluded: too common as code description ("the build is broken")
}


def extract_text_from_content(content):
    """Extract plain text from message content blocks."""
    if isinstance(content, str):
        return content
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
    return "\n".join(texts)


def count_swears(text):
    """Count swear words in text, avoiding false positives from code/technical terms."""
    text_lower = text.lower()
    # Strip code blocks to avoid counting swears in variable names, comments, etc.
    text_clean = re.sub(r"```[\s\S]*?```", "", text_lower)
    text_clean = re.sub(r"`[^`]+`", "", text_clean)
    # Strip URLs
    text_clean = re.sub(r"https?://\S+", "", text_clean)
    # Strip file paths
    text_clean = re.sub(r"[/~][\w./-]{3,}", "", text_clean)

    words = re.findall(r"\b[a-z]+\b", text_clean)
    count = 0
    found = []
    for word in words:
        if word in SWEAR_WORDS and word not in SWEAR_EXCEPTIONS:
            count += 1
            found.append(word)
    return count, found


def count_nice(text):
    """Count niceness/politeness indicators in text."""
    text_lower = text.lower()
    # Strip code blocks
    text_clean = re.sub(r"```[\s\S]*?```", "", text_lower)
    text_clean = re.sub(r"`[^`]+`", "", text_clean)

    count = 0
    found = []

    # Single words
    words = re.findall(r"\b[a-z]+\b", text_clean)
    for word in words:
        if word in NICE_SINGLE:
            count += 1
            found.append(word)

    # Multi-word phrases
    for phrase in NICE_PHRASES:
        occurrences = text_clean.count(phrase)
        if occurrences > 0:
            count += occurrences
            found.append(phrase)

    return count, found


def count_harsh(text):
    """Count harsh/negative words."""
    text_lower = text.lower()
    text_clean = re.sub(r"```[\s\S]*?```", "", text_lower)
    text_clean = re.sub(r"`[^`]+`", "", text_clean)

    words = re.findall(r"\b[a-z]+\b", text_clean)
    count = 0
    found = []
    for word in words:
        if word in HARSH_WORDS:
            count += 1
            found.append(word)
    return count, found


def parse_hour(timestamp_str):
    """Parse hour (0-23) from ISO timestamp string."""
    if not timestamp_str:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return dt.hour
    except (ValueError, TypeError):
        return None


def parse_date(timestamp_str):
    """Parse date string from ISO timestamp."""
    if not timestamp_str:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def analyze_session(filepath):
    """Analyze tone of a single session."""
    user_swears = 0
    user_nice = 0
    user_harsh = 0
    assistant_swears = 0
    assistant_nice = 0
    user_msg_count = 0
    assistant_msg_count = 0
    swear_examples = []
    nice_examples = []
    harsh_examples = []

    user_swear_words = Counter()
    user_nice_words = Counter()
    user_harsh_words = Counter()

    # Time-based tracking
    swears_by_hour = defaultdict(int)
    nice_by_hour = defaultdict(int)
    first_timestamp = None
    swear_examples_with_hour = []  # (excerpt, words, hour)

    with open(filepath, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            timestamp = obj.get("timestamp", "")
            hour = parse_hour(timestamp)

            # Track first timestamp for session date
            if not first_timestamp and timestamp:
                first_timestamp = timestamp

            content = obj.get("message", {}).get("content", [])
            text = extract_text_from_content(content)
            if not text or len(text) < 3:
                continue

            if msg_type == "user":
                # Skip tool results (system-generated)
                if isinstance(content, list):
                    has_human_text = False
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            has_human_text = True
                            break
                        elif isinstance(block, str):
                            has_human_text = True
                            break
                    if not has_human_text:
                        continue

                # Skip continuation summaries and auto-generated messages
                if text.startswith("This session is being continued"):
                    continue
                if "# Ralph Loop Command" in text or "stop hook is now active" in text:
                    continue
                if re.match(r"^# Ralph Loop", text):
                    continue
                if text.startswith("Implement the following plan:"):
                    continue

                user_msg_count += 1
                sc, sf = count_swears(text)
                nc, nf = count_nice(text)
                hc, hf = count_harsh(text)
                user_swears += sc
                user_nice += nc
                user_harsh += hc
                user_swear_words.update(sf)
                user_nice_words.update(nf)
                user_harsh_words.update(hf)

                # Track by hour
                if hour is not None:
                    swears_by_hour[hour] += sc
                    nice_by_hour[hour] += nc

                if sf and len(swear_examples) < 5:
                    excerpt = text[:200].replace("\n", " ").strip()
                    swear_examples.append((excerpt, sf))
                    if hour is not None:
                        swear_examples_with_hour.append((excerpt, sf, hour))
                if nf and len(nice_examples) < 5:
                    excerpt = text[:200].replace("\n", " ").strip()
                    nice_examples.append((excerpt, nf))
                if hf and len(harsh_examples) < 3:
                    excerpt = text[:200].replace("\n", " ").strip()
                    harsh_examples.append((excerpt, hf))

            elif msg_type == "assistant":
                assistant_msg_count += 1
                sc, _ = count_swears(text)
                nc, _ = count_nice(text)
                assistant_swears += sc
                assistant_nice += nc

                # Track assistant by hour too
                if hour is not None:
                    nice_by_hour[hour] += nc

    if user_msg_count == 0:
        return None

    # Niceness score: 0-10 scale
    nice_rate = user_nice / max(user_msg_count, 1)
    harsh_rate = (user_harsh + user_swears) / max(user_msg_count, 1)
    niceness_score = min(10, max(0, 5 + (nice_rate * 3) - (harsh_rate * 5)))

    # Session start date
    start_date = parse_date(first_timestamp) if first_timestamp else None

    return {
        "file": str(filepath),
        "user_msg_count": user_msg_count,
        "assistant_msg_count": assistant_msg_count,
        "user_swears": user_swears,
        "user_nice": user_nice,
        "user_harsh": user_harsh,
        "assistant_swears": assistant_swears,
        "assistant_nice": assistant_nice,
        "niceness_score": round(niceness_score, 1),
        "user_swear_words": dict(user_swear_words.most_common()),
        "user_nice_words": dict(user_nice_words.most_common()),
        "user_harsh_words": dict(user_harsh_words.most_common()),
        "swear_examples": swear_examples,
        "nice_examples": nice_examples,
        "harsh_examples": harsh_examples,
        "swears_by_hour": dict(swears_by_hour),
        "nice_by_hour": dict(nice_by_hour),
        "start_date": start_date,
        "swear_examples_with_hour": swear_examples_with_hour,
    }


def generate_report(results):
    """Generate markdown report."""
    lines = []
    lines.append("# Communication Tone Analysis")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Sessions analyzed:** {len(results)}")
    lines.append("")

    total_user_swears = sum(r["user_swears"] for r in results)
    total_user_nice = sum(r["user_nice"] for r in results)
    total_user_harsh = sum(r["user_harsh"] for r in results)
    total_assistant_swears = sum(r["assistant_swears"] for r in results)
    total_assistant_nice = sum(r["assistant_nice"] for r in results)
    total_user_msgs = sum(r["user_msg_count"] for r in results)
    total_assistant_msgs = sum(r["assistant_msg_count"] for r in results)

    avg_niceness = sum(r["niceness_score"] for r in results) / len(results) if results else 0
    nicest = max(results, key=lambda r: r["niceness_score"]) if results else None
    harshest = min(results, key=lambda r: r["niceness_score"]) if results else None

    # Aggregate word counts
    all_swear_words = Counter()
    all_nice_words = Counter()
    all_harsh_words = Counter()
    for r in results:
        all_swear_words.update(r["user_swear_words"])
        all_nice_words.update(r["user_nice_words"])
        all_harsh_words.update(r["user_harsh_words"])

    lines.append("## Summary")
    lines.append("")
    lines.append("### User Communication")
    lines.append(f"- **Total user messages:** {total_user_msgs}")
    lines.append(f"- **Swear words:** {total_user_swears} ({total_user_swears/max(total_user_msgs,1)*100:.1f}% of messages)")
    lines.append(f"- **Nice/polite words:** {total_user_nice} ({total_user_nice/max(total_user_msgs,1)*100:.1f}% of messages)")
    lines.append(f"- **Harsh words:** {total_user_harsh} ({total_user_harsh/max(total_user_msgs,1)*100:.1f}% of messages)")
    lines.append(f"- **Nice-to-harsh ratio:** {total_user_nice/max(total_user_harsh+total_user_swears,1):.1f}x")
    lines.append(f"- **Average niceness score:** {avg_niceness:.1f}/10")
    lines.append("")

    lines.append("### Assistant Communication")
    lines.append(f"- **Total assistant messages:** {total_assistant_msgs}")
    lines.append(f"- **Swear words:** {total_assistant_swears}")
    lines.append(f"- **Nice/polite words:** {total_assistant_nice}")
    lines.append("")

    lines.append("## Swear Word Breakdown")
    lines.append("")
    if all_swear_words:
        lines.append("| Word | Count |")
        lines.append("|------|------:|")
        for word, count in all_swear_words.most_common(15):
            lines.append(f"| {word} | {count} |")
    else:
        lines.append("No swear words detected.")
    lines.append("")

    lines.append("## Nice Word Breakdown")
    lines.append("")
    lines.append("| Word/Phrase | Count |")
    lines.append("|-------------|------:|")
    for word, count in all_nice_words.most_common(15):
        lines.append(f"| {word} | {count} |")
    lines.append("")

    lines.append("## Harsh Word Breakdown")
    lines.append("")
    if all_harsh_words:
        lines.append("| Word | Count |")
        lines.append("|------|------:|")
        for word, count in all_harsh_words.most_common(15):
            lines.append(f"| {word} | {count} |")
    else:
        lines.append("No harsh words detected.")
    lines.append("")

    lines.append("## Niceness Score Distribution")
    lines.append("")
    score_buckets = Counter()
    for r in results:
        bucket = int(r["niceness_score"])
        score_buckets[bucket] += 1
    lines.append("| Score | Sessions |")
    lines.append("|------:|---------:|")
    for score in range(11):
        count = score_buckets.get(score, 0)
        lines.append(f"| {score}/10 | {count} |")
    lines.append("")

    # Swear examples
    lines.append("## Sample Messages with Swear Words")
    lines.append("")
    all_swear_examples = []
    for r in results:
        all_swear_examples.extend(r["swear_examples"])
    if all_swear_examples:
        for excerpt, words in all_swear_examples[:10]:
            lines.append(f"- [{', '.join(words)}] `{excerpt[:150]}`")
    else:
        lines.append("No swear word examples found.")
    lines.append("")

    # Nice examples
    lines.append("## Sample Nice Messages")
    lines.append("")
    all_nice_examples = []
    for r in results:
        all_nice_examples.extend(r["nice_examples"])
    if all_nice_examples:
        for excerpt, words in all_nice_examples[:10]:
            lines.append(f"- [{', '.join(words[:3])}] `{excerpt[:150]}`")
    else:
        lines.append("No niceness examples found.")
    lines.append("")

    # Top sessions by swearing
    swearing_sessions = sorted(results, key=lambda r: r["user_swears"], reverse=True)[:10]
    lines.append("## Top Sessions by Swear Count")
    lines.append("")
    lines.append("| Session | Swears | Nice | Harsh | Score |")
    lines.append("|---------|-------:|-----:|------:|------:|")
    for r in swearing_sessions:
        if r["user_swears"] == 0:
            break
        sid = Path(r["file"]).stem[:16]
        lines.append(f"| `{sid}` | {r['user_swears']} | {r['user_nice']} | {r['user_harsh']} | {r['niceness_score']}/10 |")
    lines.append("")

    return "\n".join(lines)


def main():
    print("Finding sessions...")
    sessions = find_sessions(max_sessions=9999, min_size=10 * 1024)
    print(f"Found {len(sessions)} sessions")

    results = []
    for i, (mtime, size, path) in enumerate(sessions):
        if (i + 1) % 100 == 0:
            print(f"  Processing {i+1}/{len(sessions)}...")
        try:
            result = analyze_session(path)
            if result:
                results.append(result)
        except Exception as e:
            print(f"  Error: {path.name}: {e}")

    print(f"Analyzed {len(results)} sessions")

    report = generate_report(results)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(report)
    print(f"Report saved to {OUTPUT_PATH}")

    # Print key stats
    total_user_swears = sum(r["user_swears"] for r in results)
    total_user_nice = sum(r["user_nice"] for r in results)
    total_assistant_swears = sum(r["assistant_swears"] for r in results)
    total_assistant_nice = sum(r["assistant_nice"] for r in results)
    avg_niceness = sum(r["niceness_score"] for r in results) / len(results) if results else 0
    print(f"\n=== KEY STATS ===")
    print(f"User swear words: {total_user_swears}")
    print(f"User nice words: {total_user_nice}")
    print(f"Assistant swear words: {total_assistant_swears}")
    print(f"Assistant nice words: {total_assistant_nice}")
    print(f"Avg niceness: {avg_niceness:.1f}/10")
    if total_user_nice > 0 and total_assistant_nice > 0:
        ratio = total_assistant_nice / max(total_user_nice, 1)
        print(f"Claude nice / User nice ratio: {ratio:.1f}x")

    # Aggregate swears by hour
    agg_swears_hour = defaultdict(int)
    agg_nice_hour = defaultdict(int)
    for r in results:
        for h, c in r.get("swears_by_hour", {}).items():
            agg_swears_hour[int(h)] += c
        for h, c in r.get("nice_by_hour", {}).items():
            agg_nice_hour[int(h)] += c

    print(f"\n=== SWEARS BY HOUR (UTC) ===")
    for h in range(24):
        bar = "#" * agg_swears_hour.get(h, 0)
        print(f"  {h:02d}:00  {agg_swears_hour.get(h, 0):3d}  {bar}")

    peak_swear_hour = max(range(24), key=lambda h: agg_swears_hour.get(h, 0)) if agg_swears_hour else None
    peak_nice_hour = max(range(24), key=lambda h: agg_nice_hour.get(h, 0)) if agg_nice_hour else None
    print(f"\nPeak swear hour: {peak_swear_hour}:00 UTC ({agg_swears_hour.get(peak_swear_hour, 0)} swears)")
    print(f"Peak nice hour: {peak_nice_hour}:00 UTC ({agg_nice_hour.get(peak_nice_hour, 0)} nice words)")

    print(f"\n=== NICE WORDS BY HOUR (UTC) ===")
    for h in range(24):
        count = agg_nice_hour.get(h, 0)
        bar = "#" * min(count, 80)
        print(f"  {h:02d}:00  {count:4d}  {bar}")

    # Niceness by week
    week_scores = defaultdict(list)
    for r in results:
        d = r.get("start_date")
        if d:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                week_label = dt.strftime("%Y-W%U")
                week_scores[week_label].append(r["niceness_score"])
            except ValueError:
                pass

    print(f"\n=== NICENESS BY WEEK ===")
    for week in sorted(week_scores.keys()):
        scores = week_scores[week]
        avg = sum(scores) / len(scores)
        print(f"  {week}: {avg:.1f}/10 ({len(scores)} sessions)")

    # Swear examples with hour for quote picking
    all_swear_hour_examples = []
    for r in results:
        all_swear_hour_examples.extend(r.get("swear_examples_with_hour", []))
    if all_swear_hour_examples and peak_swear_hour is not None:
        peak_examples = [e for e in all_swear_hour_examples if e[2] == peak_swear_hour]
        if peak_examples:
            print(f"\n=== SWEAR EXAMPLES AT PEAK HOUR ({peak_swear_hour}:00) ===")
            for excerpt, words, hour in peak_examples[:5]:
                print(f"  [{', '.join(words)}] {excerpt[:120]}")

    # JSON data for wrapped.html (easy copy-paste)
    print(f"\n=== JSON FOR WRAPPED ===")
    swears_arr = [agg_swears_hour.get(h, 0) for h in range(24)]
    nice_arr = [agg_nice_hour.get(h, 0) for h in range(24)]
    print(f"swears_by_hour = {swears_arr}")
    print(f"nice_by_hour = {nice_arr}")

    # Please count
    all_nice_words = Counter()
    for r in results:
        all_nice_words.update(r["user_nice_words"])
    print(f"please_count = {all_nice_words.get('please', 0)}")
    print(f"thanks_count = {all_nice_words.get('thanks', 0) + all_nice_words.get('thank', 0)}")


if __name__ == "__main__":
    main()
