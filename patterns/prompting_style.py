#!/usr/bin/env python3
"""
Analyze Claude Code conversation JSONL files to study the user's prompting patterns and style.
Reads all sessions from ~/.claude/projects/, extracts user text messages, and produces
a comprehensive markdown report.
"""

import json
import os
import glob
import re
import math
from collections import Counter, defaultdict
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
try:
    from patterns.config import CLAUDE_PROJECTS_DIR, output_path as _output_path
except ImportError:
    from config import CLAUDE_PROJECTS_DIR, output_path as _output_path
PROJECTS_DIR = str(CLAUDE_PROJECTS_DIR)
OUTPUT_FILE = str(_output_path("prompting_style"))

# ── Stopwords (common English words to exclude from phrase analysis) ──────────
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "is", "it", "this", "that", "be", "as", "are", "was",
    "were", "been", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "not", "no", "if", "then",
    "so", "up", "out", "about", "into", "over", "after", "just", "also", "than",
    "more", "some", "any", "all", "each", "every", "both", "few", "most", "other",
    "new", "old", "its", "my", "your", "his", "her", "our", "their", "we", "you",
    "he", "she", "they", "i", "me", "him", "us", "them", "what", "which", "who",
    "whom", "when", "where", "how", "why", "there", "here", "now", "very", "too",
    "only", "own", "same", "still", "such", "because", "between", "before",
    "while", "during", "through", "above", "below", "these", "those", "been",
    "being", "get", "got", "let", "like", "make", "made", "need", "see", "use",
    "using", "used", "one", "two", "don't", "doesn't", "didn't", "won't", "can't",
    "isn't", "aren't", "wasn't", "weren't", "hasn't", "haven't", "hadn't",
    "wouldn't", "couldn't", "shouldn't", "it's", "i'm", "i've", "i'll", "i'd",
    "you're", "you've", "you'll", "you'd", "he's", "she's", "we're", "we've",
    "they're", "they've", "that's", "there's", "here's", "what's", "who's",
    "let's", "how's", "where's", "when's", "why's", "much", "many", "well",
    "back", "even", "give", "go", "going", "good", "keep", "know", "look",
    "put", "say", "said", "take", "tell", "think", "want", "way", "work",
    # Domain-specific: coding-session noise words that tell nothing about style
    "claude", "code", "file", "error", "add", "update", "create", "run",
    "test", "check", "set", "sure", "use", "make", "need", "get", "fix",
    "change", "try", "also", "like", "just", "now", "new", "one", "thing",
    "see", "let", "please", "thanks", "hi", "hey", "yes", "no", "ok", "okay",
    # System vocabulary: model/tool names and generic terms
    "gemini", "user", "project", "output",
}

# ── Frustration signal patterns ───────────────────────────────────────────────
CORRECTION_PATTERNS = [
    r"\bno[,.\s!]",
    r"\bwrong\b",
    r"\bnot what i (meant|wanted|asked)\b",
    r"\bi said\b",
    r"\bactually[,\s]",
    r"\bstop\b",
    r"\binstead\b",
    r"\bthat's not\b",
    r"\bthat is not\b",
    r"\bdon'?t do\b",
    r"\bnever\b",
    r"\bplease don'?t\b",
    r"\bwhat i (meant|want)\b",
]

FRUSTRATION_PATTERNS = {
    "why_questions": r"\bwhy\b",
    "again": r"\bagain\b",
    "already_told": r"\balready told you\b",
    "stop": r"\bstop\b",
    "no_exclamation": r"\bno!",
    "exclamation_marks": r"!{2,}",
    "all_caps_words": None,  # handled specially
}

# ── First message categories ─────────────────────────────────────────────────
FIRST_MSG_PATTERNS = {
    "bug_report": [
        r"\bbug\b", r"\bbroken\b", r"\berror\b", r"\bfail", r"\bcrash",
        r"\bnot working\b", r"\bdoesn'?t work\b", r"\bissue\b", r"\bfix\b",
    ],
    "feature_request": [
        r"\badd\b", r"\bimplement\b", r"\bcreate\b", r"\bbuild\b", r"\bnew\b",
        r"\bfeature\b", r"\bwant\b", r"\bsupport for\b",
    ],
    "question": [
        r"^(what|how|why|where|when|can|does|is|are|do|did|will|would|could)\b",
        r"\?$", r"\?[)\s]*$",
    ],
    "vague_direction": [
        r"^(look at|check|review|explore|investigate|analyze|read)\b",
        r"^(help|assist)\b",
    ],
    "specific_instruction": [
        r"^(run|execute|install|deploy|push|commit|delete|remove|rename|move|copy|update|change|set|configure)\b",
        r"^(edit|modify|write|rewrite|refactor)\b",
        r"```",
    ],
    "continuation": [
        r"^(this session is being continued|continuing from|context was compacted|summary below)",
        r"^\[continuation from previous session\]$",
        r"^(ok|yes|no|sure|go ahead|continue|proceed|do it|next|done)\b",
        r"^(yes|yep|yeah|ok|okay|sure|right|correct|exactly|perfect)\s*[.,!]?\s*$",
    ],
}

# ── Language detection heuristics ─────────────────────────────────────────────
LANG_PATTERNS = {
    "German": [
        r"\b(bitte|danke|ja|nein|und|oder|aber|nicht|auch|noch|schon|mal|doch|das|die|der|den|dem|des|ein|eine|einen|einem|einer|mit|von|auf|für|ist|sind|hat|haben|wird|werden|kann|können|muss|müssen|soll|sollen|ich|du|er|sie|wir|ihr)\b",
    ],
    "Italian": [
        r"\b(per favore|grazie|si|no|anche|ancora|già|non|come|dove|quando|perché|questo|questa|quello|quella|il|lo|la|le|gli|un|uno|una|di|da|in|con|su|per|tra|fra|che|è|sono|ha|hanno|fare|dire|andare|stare|potere|volere|dovere|io|tu|lui|lei|noi|voi|loro)\b",
    ],
    "Spanish": [
        r"\b(por favor|gracias|sí|no|también|todavía|ya|como|donde|cuando|por qué|este|esta|ese|esa|el|la|los|las|un|una|unos|unas|de|en|con|para|por|que|es|son|tiene|tienen|hacer|decir|ir|estar|poder|querer|deber|yo|tú|él|ella|nosotros|ellos)\b",
    ],
    "French": [
        r"\b(s'il vous plaît|merci|oui|non|aussi|encore|déjà|pas|comment|où|quand|pourquoi|ce|cette|ces|le|la|les|un|une|des|de|du|en|dans|avec|pour|sur|par|que|est|sont|a|ont|faire|dire|aller|être|pouvoir|vouloir|devoir|je|tu|il|elle|nous|vous|ils|elles)\b",
    ],
}


def find_all_session_files():
    """Find all JSONL files in .claude/projects, including nested directories."""
    pattern = os.path.join(PROJECTS_DIR, "**", "*.jsonl")
    return glob.glob(pattern, recursive=True)


def is_subagent_file(filepath):
    return "/subagents/" in filepath


def extract_user_text(message_obj):
    """
    Extract human-written text from a user message.
    Returns the text string or None if it's not actual human text.
    Filters out:
      - tool_result blocks (these are system-generated)
      - [Request interrupted by user ...] messages
      - <task-notification> blocks
      - Context continuation summaries (huge pasted transcripts)
    """
    if message_obj.get("type") != "user":
        return None

    content = message_obj.get("message", {}).get("content")
    if content is None:
        return None

    # String content is direct user text
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        # Extract text blocks, skip tool_result blocks
        text_parts = []
        has_only_tool_results = True
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    t = item.get("text", "").strip()
                    if t:
                        text_parts.append(t)
                        has_only_tool_results = False
                elif item.get("type") == "tool_result":
                    pass  # skip
            elif isinstance(item, str):
                text_parts.append(item)
                has_only_tool_results = False
        if has_only_tool_results or not text_parts:
            return None
        text = "\n".join(text_parts)
    else:
        return None

    if not text:
        return None

    # Filter out system-generated messages
    if text.startswith("[Request interrupted"):
        return None
    if text.startswith("<task-notification>"):
        return None

    # Context continuation summaries contain huge pasted transcripts that pollute
    # word frequency, language detection, etc.
    if text.startswith("This session is being continued from a previous conversation"):
        return "[continuation from previous session]"

    # "Implement the following plan:" messages are auto-generated from plan mode.
    # The plan itself is machine-structured. Extract just "Implement the following plan"
    # as a marker, since the user did trigger it.
    if text.startswith("Implement the following plan:"):
        return "Implement the following plan"

    # Ralph Loop / stop hook feedback messages are auto-generated by the hook system,
    # not typed by the user. Filter them out.
    if "# Ralph Loop Command" in text or "stop hook is now active" in text:
        return None
    # Also filter the auto-fed-back prompt pattern from ralph loops
    if re.match(r"^# Ralph Loop", text):
        return None

    # Skip pasted Claude Code output (copy-pasted terminal output)
    if re.match(r"^▗\s*▗\s*▖\s*▖.*Claude Code", text, re.DOTALL):
        return None
    # Also catch "pls read" + pasted terminal output
    pls_read_match = re.match(r"^(pls read|please read)\s+▗", text, re.DOTALL | re.IGNORECASE)
    if pls_read_match:
        return "pls read [pasted terminal output]"

    return text


def extract_session_data(filepath):
    """Parse a session JSONL file and return structured data."""
    messages = []
    errors = 0
    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                messages.append(obj)
            except (json.JSONDecodeError, ValueError):
                errors += 1
    return messages, errors


def classify_first_message(text):
    """Classify the first message of a session into categories."""
    text_lower = text.lower().strip()

    # Check continuation first (highest priority)
    for pattern in FIRST_MSG_PATTERNS["continuation"]:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return "continuation"

    scores = defaultdict(int)
    for category, patterns in FIRST_MSG_PATTERNS.items():
        if category == "continuation":
            continue
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE | re.MULTILINE):
                scores[category] += 1

    if not scores:
        return "other"

    return max(scores, key=scores.get)


def detect_corrections(text):
    """Check if a message contains correction signals.
    Only analyze short-to-medium messages (under 500 chars) to avoid false
    positives from pasted content, plans, and continuation summaries.
    """
    if len(text) > 500:
        return []
    text_lower = text.lower()
    found = []
    for pattern in CORRECTION_PATTERNS:
        if re.search(pattern, text_lower):
            found.append(pattern)
    return found


def detect_frustration(text):
    """Detect frustration signals in a message.
    Only analyze short-to-medium messages to avoid false positives from
    pasted plans, code, and continuation summaries.
    """
    # Skip very long messages (likely pasted content, not frustration)
    if len(text) > 500:
        return {}

    text_lower = text.lower()
    signals = {}

    ACRONYMS = {
        "API", "URL", "CSS", "HTML", "JSON", "SQL", "SSH", "CLI", "DNS",
        "HTTP", "HTTPS", "UI", "UX", "PDF", "SVG", "NPM", "PNPM", "ENV",
        "EOF", "README", "JSONL", "CORS", "SMTP", "IMAP", "TLS", "SSL",
        "GPT", "LLM", "MCP", "SDK", "CDN", "AWS", "GCP", "VPS", "DSGVO",
        "MECE", "YAGNI", "KISS", "DRY", "SOLID", "TODO", "CRUD", "TLDR",
        "MAX", "DONE", "HEAD", "GET", "POST", "PUT", "DELETE", "PATCH",
        "OPEN", "CLOSE", "FIXED", "VERIFIED", "FIXING", "CTA", "SEO",
        "CREATED", "MODIFIED", "APA", "LATEST", "NEW", "FIX", "NOT",
        "TSX", "JSX", "IDE", "VSC", "CMD", "CTRL", "ALT", "ESC",
        "RGB", "HEX", "HSL", "TCP", "UDP", "FTP", "SSH", "SCP",
    }

    for name, pattern in FRUSTRATION_PATTERNS.items():
        if name == "all_caps_words":
            # Count words of 3+ chars that are ALL CAPS
            caps_words = re.findall(r"\b[A-Z]{3,}\b", text)
            caps_words = [w for w in caps_words if w not in ACRONYMS]
            if caps_words:
                signals[name] = caps_words
        else:
            matches = re.findall(pattern, text_lower)
            if matches:
                signals[name] = matches
    return signals


def detect_slash_commands(text):
    """Find actual Claude Code slash commands in user messages.
    Excludes file paths like /Users, /tmp, /var, /etc, /home, /opt, /root, /dev, /private.
    Slash commands appear at the start of a line or message and are short single words.
    """
    # Known file path prefixes and URL paths to exclude
    path_prefixes = {
        "/users", "/tmp", "/var", "/etc", "/home", "/opt", "/root", "/dev",
        "/private", "/bin", "/sbin", "/usr", "/lib", "/proc", "/sys", "/mnt",
        "/volumes", "/applications", "/library", "/system",
        # URL route paths (not CLI commands)
        "/login", "/logout", "/app", "/api", "/dashboard", "/analytics",
        "/status", "/generate", "/public", "/examples", "/mentions",
        "/km", "/feed", "/settings", "/admin", "/auth", "/callback",
        "/search", "/profile", "/account", "/register", "/signup",
        "/health", "/metrics", "/webhook", "/webhooks", "/static",
        "/assets", "/images", "/fonts", "/scripts", "/styles",
    }
    # Match /command at start of line or after whitespace
    commands = re.findall(r"(?:^|\s)(\/[a-zA-Z][\w-]*)", text, re.MULTILINE)
    # Filter out file paths
    filtered = []
    for cmd in commands:
        cmd_lower = cmd.lower()
        if cmd_lower in path_prefixes:
            continue
        # Also skip if it looks like a file path (followed by / in the original text)
        idx = text.find(cmd)
        if idx >= 0 and idx + len(cmd) < len(text) and text[idx + len(cmd)] == "/":
            continue
        # Also skip URL paths like /api/v1, /login/callback
        # Only keep if it looks like a standalone command (not part of a path)
        if re.search(re.escape(cmd) + r"/", text):
            continue
        filtered.append(cmd)
    return filtered


def strip_code_and_pasted_content(text):
    """Remove code blocks, file paths, URLs, HTML, and other non-natural-language content."""
    # Remove code blocks
    text = re.sub(r"```[\s\S]*?```", "", text)
    # Remove inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove file paths
    text = re.sub(r"[~/][\w./-]{3,}", "", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Remove CSS-like content (class names, selectors)
    text = re.sub(r"\b[\w-]+:\s*[\w#%.]+;", "", text)
    # Remove import/export statements
    text = re.sub(r"^\s*(import|export|from|require)\b.*$", "", text, flags=re.MULTILINE)
    # Remove JSON-like content
    text = re.sub(r"\{[^}]{20,}\}", "", text)
    return text


def detect_language(text):
    """Detect non-English language usage in natural language portions of the text.
    Requires strong signals to avoid false positives from pasted web content.
    """
    # Skip synthetic markers
    if text.startswith("[continuation") or text == "Implement the following plan":
        return {}
    if text.startswith("pls read [pasted"):
        return {}

    # Strip code/technical content first to avoid false positives
    clean_text = strip_code_and_pasted_content(text)
    # Skip very short remaining text
    if len(clean_text.strip()) < 20:
        return {}

    # Skip if the clean text is mostly numbers/symbols (pasted web garbage)
    alpha_chars = sum(1 for c in clean_text if c.isalpha())
    if alpha_chars < len(clean_text.strip()) * 0.4:
        return {}

    detected = {}
    text_lower = clean_text.lower()

    # German: most reliable detection since the words are very distinctive
    german_distinctive = {"bitte", "danke", "nicht", "auch", "noch", "schon", "doch",
                          "können", "müssen", "sollen", "werden", "bereits", "sehr",
                          "eimsbüttel", "speisekarte", "wohnzimmer", "stammgäste",
                          "unsere", "unseren", "legendären", "preiswerte"}
    german_pattern = LANG_PATTERNS["German"][0]
    german_matches = re.findall(german_pattern, text_lower)
    german_dist = [m for m in german_matches if m in german_distinctive]
    if len(german_dist) >= 2 or len(german_matches) >= 6:
        detected["German"] = len(german_matches)

    # For Romance languages, require VERY strong signals since short words
    # (de, la, le, un, per, con, etc.) appear everywhere in English text too.
    # Only detect if there are 3+ distinctive words that are unambiguously that language.

    italian_strong = {"anche", "ancora", "questo", "questa", "quello", "quella",
                      "hanno", "andare", "stare", "grazie", "perché", "già",
                      "senza", "sempre", "tutto", "tutti", "prima", "dopo",
                      "oggi", "ieri", "domani", "buon", "buona"}
    italian_matches = re.findall(LANG_PATTERNS["Italian"][0], text_lower)
    italian_dist = [m for m in italian_matches if m in italian_strong]
    if len(italian_dist) >= 3:
        detected["Italian"] = len(italian_matches)

    french_strong = {"aussi", "encore", "déjà", "pourquoi", "comment",
                     "cette", "dans", "avec", "sont", "merci", "bonjour",
                     "aujourd'hui", "beaucoup", "toujours", "jamais",
                     "peut", "tout", "tous", "rien", "quelque"}
    french_matches = re.findall(LANG_PATTERNS["French"][0], text_lower)
    french_dist = [m for m in french_matches if m in french_strong]
    if len(french_dist) >= 3:
        detected["French"] = len(french_matches)

    spanish_strong = {"también", "todavía", "donde", "cuando",
                      "tiene", "tienen", "hacer", "decir", "estar",
                      "poder", "querer", "deber", "nosotros", "gracias",
                      "siempre", "nunca", "todo", "nada", "algo",
                      "bueno", "buena", "mucho", "poco"}
    spanish_matches = re.findall(LANG_PATTERNS["Spanish"][0], text_lower)
    spanish_dist = [m for m in spanish_matches if m in spanish_strong]
    if len(spanish_dist) >= 3:
        detected["Spanish"] = len(spanish_matches)

    return detected


def make_histogram(values, bucket_count=10):
    """Create a simple text histogram."""
    if not values:
        return "No data"

    min_val = min(values)
    max_val = max(values)

    if min_val == max_val:
        return f"All values = {min_val}"

    # Use log scale for wide ranges
    use_log = (max_val / max(min_val, 1)) > 100

    if use_log:
        # Predefined buckets for log scale
        boundaries = [0, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 50000]
        boundaries = [b for b in boundaries if b <= max_val * 1.1]
        if boundaries[-1] < max_val:
            boundaries.append(int(max_val) + 1)
    else:
        step = (max_val - min_val) / bucket_count
        boundaries = [min_val + step * i for i in range(bucket_count + 1)]

    buckets = []
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        count = sum(1 for v in values if lo <= v < hi)
        if i == len(boundaries) - 2:  # last bucket is inclusive
            count = sum(1 for v in values if lo <= v <= hi)
        buckets.append((lo, hi, count))

    # Filter empty buckets
    buckets = [(lo, hi, c) for lo, hi, c in buckets if c > 0]

    max_count = max(c for _, _, c in buckets) if buckets else 1
    bar_max = 40

    lines = []
    for lo, hi, count in buckets:
        bar_len = int((count / max_count) * bar_max) if max_count > 0 else 0
        bar = "#" * bar_len
        pct = (count / len(values)) * 100
        lines.append(f"  {int(lo):>6} - {int(hi):>6} | {bar:<{bar_max}} {count:>5} ({pct:.1f}%)")

    return "\n".join(lines)


def analyze_prompt_specificity(text):
    """
    Score a prompt's specificity (0-10) based on heuristics:
    - Contains code blocks (+2)
    - Contains file paths (+2)
    - Contains specific technical terms (+1)
    - Length > 200 chars (+1)
    - Contains numbered steps (+2)
    - Contains bullet points (+1)
    - Very short / vague (-2)
    """
    score = 5  # baseline
    text_lower = text.lower()

    if "```" in text:
        score += 2
    if re.search(r"[/~][\w.-]+/[\w.-]+", text):
        score += 2
    if re.search(r"\b(function|class|component|api|endpoint|route|database|schema|type|interface|import|export)\b", text_lower):
        score += 1
    if len(text) > 200:
        score += 1
    if re.search(r"^\s*\d+[.)]\s", text, re.MULTILINE):
        score += 2
    if re.search(r"^\s*[-*]\s", text, re.MULTILINE):
        score += 1
    if len(text) < 30:
        score -= 2
    if len(text) < 15:
        score -= 2

    return max(0, min(10, score))


def main():
    print("Finding session files...")
    all_files = find_all_session_files()
    # Only analyze main session files, not subagent files
    main_files = [f for f in all_files if not is_subagent_file(f)]
    print(f"Found {len(all_files)} total JSONL files, {len(main_files)} main sessions")

    # ── Collect all user messages ──────────────────────────────────────────────
    all_user_texts = []          # (session_file, msg_index_in_session, text, timestamp)
    first_messages = []          # first human text per session
    session_stats = []           # per-session stats
    total_parse_errors = 0
    total_lines = 0
    files_processed = 0

    for filepath in main_files:
        messages, errors = extract_session_data(filepath)
        total_parse_errors += errors
        total_lines += len(messages) + errors
        files_processed += 1

        session_user_texts = []
        session_assistant_count = 0
        session_error_count = 0

        for msg in messages:
            if msg.get("type") == "assistant":
                session_assistant_count += 1
            if msg.get("type") == "user":
                # Count tool errors as session errors
                content = msg.get("message", {}).get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("is_error"):
                            session_error_count += 1

            text = extract_user_text(msg)
            if text:
                ts = msg.get("timestamp", "")
                session_user_texts.append((filepath, len(session_user_texts), text, ts))

        all_user_texts.extend(session_user_texts)

        if session_user_texts:
            first_messages.append(session_user_texts[0][2])

        session_stats.append({
            "file": filepath,
            "user_msg_count": len(session_user_texts),
            "assistant_msg_count": session_assistant_count,
            "error_count": session_error_count,
            "total_msg_count": len(messages),
            "first_msg": session_user_texts[0][2] if session_user_texts else None,
            "first_msg_specificity": analyze_prompt_specificity(session_user_texts[0][2]) if session_user_texts else 0,
            "first_msg_length": len(session_user_texts[0][2]) if session_user_texts else 0,
        })

    print(f"Extracted {len(all_user_texts)} user text messages from {files_processed} files")
    print(f"Parse errors: {total_parse_errors}")

    # ── 1. Prompt Length Distribution ──────────────────────────────────────────
    char_lengths = [len(t) for _, _, t, _ in all_user_texts]
    word_lengths = [len(t.split()) for _, _, t, _ in all_user_texts]

    avg_chars = sum(char_lengths) / len(char_lengths) if char_lengths else 0
    avg_words = sum(word_lengths) / len(word_lengths) if word_lengths else 0
    median_chars = sorted(char_lengths)[len(char_lengths) // 2] if char_lengths else 0
    median_words = sorted(word_lengths)[len(word_lengths) // 2] if word_lengths else 0
    p90_chars = sorted(char_lengths)[int(len(char_lengths) * 0.9)] if char_lengths else 0
    p90_words = sorted(word_lengths)[int(len(word_lengths) * 0.9)] if word_lengths else 0

    # ── 2. First Message Analysis ─────────────────────────────────────────────
    first_msg_categories = Counter()
    first_msg_examples = defaultdict(list)
    for msg in first_messages:
        cat = classify_first_message(msg)
        first_msg_categories[cat] += 1
        if len(first_msg_examples[cat]) < 3:
            # Truncate long examples
            example = msg[:150].replace("\n", " ").strip()
            if len(msg) > 150:
                example += "..."
            first_msg_examples[cat].append(example)

    # ── 3. Correction Frequency ───────────────────────────────────────────────
    correction_count = 0
    correction_examples = []
    for _, idx, text, _ in all_user_texts:
        corrections = detect_corrections(text)
        if corrections:
            correction_count += 1
            if len(correction_examples) < 10:
                example = text[:150].replace("\n", " ").strip()
                if len(text) > 150:
                    example += "..."
                correction_examples.append(example)

    correction_rate = (correction_count / len(all_user_texts) * 100) if all_user_texts else 0

    # ── 4. Prompt-to-Outcome Ratio ────────────────────────────────────────────
    # Group sessions by specificity and compare
    specificity_buckets = defaultdict(list)
    for s in session_stats:
        if s["first_msg"] is None:
            continue
        spec = s["first_msg_specificity"]
        bucket = "low (0-3)" if spec <= 3 else "medium (4-6)" if spec <= 6 else "high (7-10)"
        specificity_buckets[bucket].append(s)

    prompt_outcome_data = {}
    for bucket, sessions in specificity_buckets.items():
        avg_session_len = sum(s["total_msg_count"] for s in sessions) / len(sessions) if sessions else 0
        avg_errors = sum(s["error_count"] for s in sessions) / len(sessions) if sessions else 0
        avg_user_msgs = sum(s["user_msg_count"] for s in sessions) / len(sessions) if sessions else 0
        avg_first_len = sum(s["first_msg_length"] for s in sessions) / len(sessions) if sessions else 0
        prompt_outcome_data[bucket] = {
            "count": len(sessions),
            "avg_session_length": avg_session_len,
            "avg_errors": avg_errors,
            "avg_user_msgs": avg_user_msgs,
            "avg_first_prompt_chars": avg_first_len,
        }

    # Also correlate first prompt length with session length
    length_buckets = defaultdict(list)
    for s in session_stats:
        if s["first_msg"] is None:
            continue
        flen = s["first_msg_length"]
        if flen < 50:
            bucket = "short (<50 chars)"
        elif flen < 200:
            bucket = "medium (50-200 chars)"
        elif flen < 1000:
            bucket = "long (200-1000 chars)"
        else:
            bucket = "very long (1000+ chars)"
        length_buckets[bucket].append(s)

    length_outcome_data = {}
    for bucket, sessions in sorted(length_buckets.items()):
        avg_session_len = sum(s["total_msg_count"] for s in sessions) / len(sessions) if sessions else 0
        avg_errors = sum(s["error_count"] for s in sessions) / len(sessions) if sessions else 0
        avg_user_msgs = sum(s["user_msg_count"] for s in sessions) / len(sessions) if sessions else 0
        length_outcome_data[bucket] = {
            "count": len(sessions),
            "avg_session_length": avg_session_len,
            "avg_errors": avg_errors,
            "avg_user_msgs": avg_user_msgs,
        }

    # ── 5. Common User Phrases ────────────────────────────────────────────────
    word_counter = Counter()
    bigram_counter = Counter()
    trigram_counter = Counter()

    # Additional words to exclude: technical terms from pasted content
    TECH_STOPWORDS = {
        "div", "span", "class", "text", "flex", "items", "center", "true", "false",
        "null", "undefined", "const", "var", "return", "function", "string", "number",
        "boolean", "type", "interface", "import", "export", "default", "extends",
        "implements", "async", "await", "promise", "then", "catch", "try", "error",
        "tsx", "jsx", "css", "src", "dist", "node", "modules", "package",
        "props", "state", "component", "render", "children", "style", "width",
        "height", "margin", "padding", "border", "color", "background", "display",
        "position", "relative", "absolute", "fixed", "overflow", "hidden",
        "font", "size", "weight", "bold", "italic", "none", "block", "inline",
        "grid", "gap", "space", "rounded", "shadow", "opacity", "transition",
        "hover", "focus", "active", "disabled", "index", "value", "name",
        "content", "data", "config", "options", "params", "args", "result",
        "response", "request", "handler", "callback", "event", "listener",
        "neutral", "gray", "white", "black", "blue", "red", "green", "yellow",
        "expand", "collapse", "ctrl", "lines", "bash", "output",
        "users", "downloads", "documents", "desktop",
        "claude", "projects",
        "commit", "branch", "merge", "diff", "HEAD",
    }

    combined_stops = STOPWORDS | TECH_STOPWORDS

    # Track seen messages to deduplicate repeated prompts (e.g., ralph loop auto-feed)
    seen_texts = set()

    for _, _, text, _ in all_user_texts:
        # Skip synthetic markers
        if text.startswith("[continuation") or text == "Implement the following plan":
            continue
        if text.startswith("pls read [pasted"):
            continue
        # Skip very long messages (likely pasted content, not user's natural phrasing)
        if len(text) > 2000:
            continue
        # Deduplicate: only count each unique text once for phrase analysis
        text_hash = hash(text)
        if text_hash in seen_texts:
            continue
        seen_texts.add(text_hash)
        # Strip code blocks and pasted content for cleaner phrase analysis
        clean = strip_code_and_pasted_content(text)
        # Normalize: lowercase, remove special chars but keep apostrophes
        words = re.findall(r"[a-zA-Z']+", clean.lower())
        # Filter stopwords and very short words
        meaningful_words = [w for w in words if w not in combined_stops and len(w) > 2]
        word_counter.update(meaningful_words)

        # Bigrams and trigrams from meaningful words
        # Use sets per message to count each bigram/trigram only once per message
        # (avoids a single long message dominating the counts)
        msg_bigrams = set()
        msg_trigrams = set()
        for i in range(len(meaningful_words) - 1):
            msg_bigrams.add(f"{meaningful_words[i]} {meaningful_words[i+1]}")
        for i in range(len(meaningful_words) - 2):
            msg_trigrams.add(f"{meaningful_words[i]} {meaningful_words[i+1]} {meaningful_words[i+2]}")
        bigram_counter.update(msg_bigrams)
        trigram_counter.update(msg_trigrams)

    # ── 6. Frustration Signals ────────────────────────────────────────────────
    frustration_counts = defaultdict(int)
    frustration_messages = []
    total_exclamations = 0
    total_caps_words = 0

    for _, idx, text, _ in all_user_texts:
        signals = detect_frustration(text)
        if signals:
            msg_signals = []
            for name, matches in signals.items():
                if name == "all_caps_words":
                    total_caps_words += len(matches)
                    frustration_counts[name] += len(matches)
                    msg_signals.append(f"ALL_CAPS: {', '.join(matches[:3])}")
                elif name == "exclamation_marks":
                    total_exclamations += len(matches)
                    frustration_counts[name] += len(matches)
                    msg_signals.append(f"!!: {len(matches)}x")
                else:
                    frustration_counts[name] += len(matches)
                    msg_signals.append(name)

            if msg_signals and len(frustration_messages) < 10:
                example = text[:120].replace("\n", " ").strip()
                if len(text) > 120:
                    example += "..."
                frustration_messages.append((example, msg_signals))

    # ── 7. Slash Command Usage ────────────────────────────────────────────────
    slash_command_counter = Counter()
    for _, _, text, _ in all_user_texts:
        commands = detect_slash_commands(text)
        slash_command_counter.update(commands)

    # ── 8. Language Patterns ──────────────────────────────────────────────────
    language_detections = defaultdict(int)
    language_examples = defaultdict(list)
    for _, _, text, _ in all_user_texts:
        detected = detect_language(text)
        for lang, count in detected.items():
            language_detections[lang] += 1
            if len(language_examples[lang]) < 3:
                example = text[:150].replace("\n", " ").strip()
                if len(text) > 150:
                    example += "..."
                language_examples[lang].append(example)

    # ── Generate Report ───────────────────────────────────────────────────────
    report = []
    report.append("# Prompting Style Analysis")
    report.append("")
    report.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report.append(f"**Sessions analyzed**: {files_processed} main session files ({len(all_files)} total including subagents)")
    report.append(f"**User messages extracted**: {len(all_user_texts)}")
    report.append(f"**Parse errors (malformed JSON lines)**: {total_parse_errors}")
    report.append("")

    # 1. Prompt Length
    report.append("---")
    report.append("")
    report.append("## 1. Prompt Length Distribution")
    report.append("")
    report.append("### Character Length")
    report.append(f"- **Mean**: {avg_chars:.0f} chars")
    report.append(f"- **Median**: {median_chars} chars")
    report.append(f"- **90th percentile**: {p90_chars} chars")
    report.append(f"- **Min**: {min(char_lengths) if char_lengths else 0} chars")
    report.append(f"- **Max**: {max(char_lengths) if char_lengths else 0} chars")
    report.append("")
    report.append("```")
    report.append(make_histogram(char_lengths))
    report.append("```")
    report.append("")
    report.append("### Word Count")
    report.append(f"- **Mean**: {avg_words:.0f} words")
    report.append(f"- **Median**: {median_words} words")
    report.append(f"- **90th percentile**: {p90_words} words")
    report.append("")
    report.append("```")
    report.append(make_histogram(word_lengths))
    report.append("```")
    report.append("")

    # 2. First Message Analysis
    report.append("---")
    report.append("")
    report.append("## 2. First Message Analysis")
    report.append("")
    report.append("How sessions typically begin:")
    report.append("")
    report.append("| Category | Count | % |")
    report.append("|----------|------:|--:|")
    total_first = sum(first_msg_categories.values())
    for cat, count in first_msg_categories.most_common():
        pct = (count / total_first * 100) if total_first else 0
        report.append(f"| {cat} | {count} | {pct:.1f}% |")
    report.append("")

    report.append("### Examples by Category")
    report.append("")
    for cat in first_msg_categories.most_common():
        cat_name = cat[0]
        report.append(f"**{cat_name}**:")
        for ex in first_msg_examples[cat_name]:
            report.append(f"- `{ex}`")
        report.append("")

    # 3. Correction Frequency
    report.append("---")
    report.append("")
    report.append("## 3. Correction Frequency")
    report.append("")
    report.append(f"- **Messages containing correction signals**: {correction_count} / {len(all_user_texts)} ({correction_rate:.1f}%)")
    report.append("")
    if correction_examples:
        report.append("### Sample Corrections")
        report.append("")
        for ex in correction_examples[:8]:
            report.append(f"- `{ex}`")
        report.append("")

    # 4. Prompt-to-Outcome Ratio
    report.append("---")
    report.append("")
    report.append("## 4. Prompt-to-Outcome Ratio")
    report.append("")
    report.append("### By Prompt Specificity (scored 0-10)")
    report.append("")
    report.append("| Specificity | Sessions | Avg Session Msgs | Avg Errors | Avg User Msgs |")
    report.append("|-------------|:--------:|:----------------:|:----------:|:-------------:|")
    for bucket in ["low (0-3)", "medium (4-6)", "high (7-10)"]:
        if bucket in prompt_outcome_data:
            d = prompt_outcome_data[bucket]
            report.append(f"| {bucket} | {d['count']} | {d['avg_session_length']:.0f} | {d['avg_errors']:.1f} | {d['avg_user_msgs']:.1f} |")
    report.append("")

    report.append("### By First Prompt Length")
    report.append("")
    report.append("| Prompt Length | Sessions | Avg Session Msgs | Avg Errors | Avg User Msgs |")
    report.append("|--------------|:--------:|:----------------:|:----------:|:-------------:|")
    for bucket in ["short (<50 chars)", "medium (50-200 chars)", "long (200-1000 chars)", "very long (1000+ chars)"]:
        if bucket in length_outcome_data:
            d = length_outcome_data[bucket]
            report.append(f"| {bucket} | {d['count']} | {d['avg_session_length']:.0f} | {d['avg_errors']:.1f} | {d['avg_user_msgs']:.1f} |")
    report.append("")

    # 5. Common Phrases
    report.append("---")
    report.append("")
    report.append("## 5. Common User Phrases")
    report.append("")
    report.append("### Top 30 Words (excluding stopwords)")
    report.append("")
    report.append("| Rank | Word | Count |")
    report.append("|-----:|------|------:|")
    for i, (word, count) in enumerate(word_counter.most_common(30), 1):
        report.append(f"| {i} | {word} | {count} |")
    report.append("")

    report.append("### Top 20 Bigrams")
    report.append("")
    report.append("| Phrase | Count |")
    report.append("|--------|------:|")
    for phrase, count in bigram_counter.most_common(20):
        report.append(f"| {phrase} | {count} |")
    report.append("")

    report.append("### Top 15 Trigrams")
    report.append("")
    report.append("| Phrase | Count |")
    report.append("|--------|------:|")
    for phrase, count in trigram_counter.most_common(15):
        report.append(f"| {phrase} | {count} |")
    report.append("")

    # 6. Frustration Signals
    report.append("---")
    report.append("")
    report.append("## 6. User Frustration Signals")
    report.append("")
    report.append("| Signal | Occurrences |")
    report.append("|--------|------------:|")
    for name, count in sorted(frustration_counts.items(), key=lambda x: -x[1]):
        label = name.replace("_", " ").title()
        report.append(f"| {label} | {count} |")
    report.append("")

    if frustration_messages:
        report.append("### Sample Messages with Frustration Signals")
        report.append("")
        for example, signals in frustration_messages[:8]:
            signal_str = ", ".join(signals)
            report.append(f"- [{signal_str}] `{example}`")
        report.append("")

    # 7. Slash Commands
    report.append("---")
    report.append("")
    report.append("## 7. Slash Command Usage")
    report.append("")
    if slash_command_counter:
        report.append("| Command | Count |")
        report.append("|---------|------:|")
        for cmd, count in slash_command_counter.most_common(20):
            report.append(f"| `{cmd}` | {count} |")
    else:
        report.append("No slash commands detected in user messages.")
    report.append("")

    # 8. Language Patterns
    report.append("---")
    report.append("")
    report.append("## 8. Language Patterns")
    report.append("")
    total_msgs = len(all_user_texts)
    if language_detections:
        report.append(f"Out of {total_msgs} messages:")
        report.append("")
        report.append("| Language | Messages Detected | % of Total |")
        report.append("|----------|:-----------------:|:----------:|")
        for lang, count in sorted(language_detections.items(), key=lambda x: -x[1]):
            pct = (count / total_msgs * 100) if total_msgs else 0
            report.append(f"| {lang} | {count} | {pct:.1f}% |")
        report.append("")
        for lang in sorted(language_detections.keys(), key=lambda x: -language_detections[x]):
            if language_examples[lang]:
                report.append(f"**{lang} examples**:")
                for ex in language_examples[lang]:
                    report.append(f"- `{ex}`")
                report.append("")
    else:
        report.append("No significant non-English language usage detected. The user communicates primarily in English.")
    report.append("")

    # ── Key Insights ──────────────────────────────────────────────────────────
    report.append("---")
    report.append("")
    report.append("## Key Insights")
    report.append("")

    insights = []

    # Prompt length insight
    if median_chars < 50:
        insights.append("The user tends to write very short, terse prompts (median under 50 chars). Communication is command-like and direct.")
    elif median_chars < 150:
        insights.append(f"The user writes moderately concise prompts (median {median_chars} chars), balancing brevity with context.")
    else:
        insights.append(f"The user writes detailed prompts (median {median_chars} chars), providing substantial context upfront.")

    # First message insight
    top_cat = first_msg_categories.most_common(1)[0] if first_msg_categories else ("unknown", 0)
    insights.append(f"Sessions most commonly begin with **{top_cat[0]}** messages ({top_cat[1]}/{total_first}, {top_cat[1]/total_first*100:.0f}%).")

    # Correction insight
    if correction_rate > 20:
        insights.append(f"High correction rate ({correction_rate:.1f}%) suggests frequent course corrections. The user actively steers the agent.")
    elif correction_rate > 10:
        insights.append(f"Moderate correction rate ({correction_rate:.1f}%). The user occasionally redirects the agent.")
    else:
        insights.append(f"Low correction rate ({correction_rate:.1f}%). The agent generally follows instructions well, or the user prefers to accept and iterate.")

    # Prompt-outcome insight
    if "high (7-10)" in prompt_outcome_data and "low (0-3)" in prompt_outcome_data:
        high = prompt_outcome_data["high (7-10)"]
        low = prompt_outcome_data["low (0-3)"]
        if high["avg_errors"] < low["avg_errors"]:
            insights.append("More specific prompts correlate with fewer errors per session.")
        elif high["avg_errors"] > low["avg_errors"]:
            insights.append("Interestingly, more specific prompts correlate with MORE errors, possibly because specific prompts target more complex tasks.")

    # Slash command insight
    if slash_command_counter:
        top_cmd = slash_command_counter.most_common(1)[0]
        insights.append(f"Most used slash command: `{top_cmd[0]}` ({top_cmd[1]}x). The user leverages slash commands for workflow efficiency.")
    else:
        insights.append("No significant slash command usage detected.")

    # Language insight
    if language_detections:
        langs = ", ".join(sorted(language_detections.keys(), key=lambda x: -language_detections[x]))
        insights.append(f"Multilingual user: detected {langs} alongside English.")
    else:
        insights.append("Primarily English-only communication.")

    for insight in insights:
        report.append(f"- {insight}")
    report.append("")

    # Write report
    output = "\n".join(report)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(output)

    print(f"\nReport saved to {OUTPUT_FILE}")
    print(f"Report length: {len(output)} chars, {len(report)} lines")


if __name__ == "__main__":
    main()
