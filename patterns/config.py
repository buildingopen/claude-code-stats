"""Shared configuration for pattern analysis scripts."""

import os
from pathlib import Path

_raw = os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects"))
_sep = ";" if os.name == "nt" else ":"
CLAUDE_PROJECTS_DIRS = [Path(p) for p in _raw.split(_sep) if p.strip()]
# Keep single CLAUDE_PROJECTS_DIR for backward compat (first entry)
CLAUDE_PROJECTS_DIR = CLAUDE_PROJECTS_DIRS[0] if CLAUDE_PROJECTS_DIRS else Path.home() / ".claude" / "projects"
PATTERNS_DIR = Path(__file__).parent
OUTPUT_DIR = PATTERNS_DIR  # outputs live alongside scripts
MIN_SESSION_SIZE = 100 * 1024  # 100KB default
MAX_SESSIONS = 50

# Rejection detection patterns (shared across extract.py, project_stats.py, retry_loops.py)
REJECTION_PATTERNS = [
    "the user rejected",
    "user doesn't want to proceed",
    "tool use was rejected",
    "request interrupted by user",
]

# Map directory basenames to friendly project names
PROJECT_NAME_MAP = {
    "openchat-v4": "OpenChat V4",
    "openpaper-upstream": "OpenPaper",
    "openpaper-team": "OpenPaper Team",
    "rocketlist-minimal": "Rocketlist",
    "opendraft-v3": "OpenDraft V3",
    "opendraft-v2": "OpenDraft V2",
    "hyperniche-scaile": "HyperNiche",
    "baradona-comfort-studio": "Baradona",
    "transcript-analyzer": "Transcript Analyzer",
    "ussd-ai-railway": "USSD AI",
    "linkedin-posts": "LinkedIn Posts",
    "linkedin-engine": "LinkedIn Engine",
    "claude-config-sync": "Claude Config Sync",
    "ax41-setup": "AX41 Setup",
    "signalaudit-repo": "SignalAudit",
    "fede-vault": "Context Vault",
    "execution-layer": "Execution Layer",
    "openpaper-video-engine": "OpenPaper Video",
    "openqueen": "OpenQueen",
    "queen": "OpenQueen",
    "openqueen-wa-listener": "OpenQueen WA",
    "runit": "RunIt",
    "root": "AX41 General",
}

# Encoded directory names that map to friendly names (for paths like -root, -)
_ENCODED_DIR_MAP = {
    "-root": "AX41 General",
    "-": "Mac General",
}

# Known path prefixes to strip when decoding encoded directory names
def _build_encoded_prefixes():
    """Build path prefixes dynamically from current user's home directory."""
    import getpass
    username = getpass.getuser()
    prefixes = [
        f"-Users-{username}-Downloads-",
        f"-Users-{username}-Documents-",
        f"-Users-{username}-",
        "-root-Downloads-",
        "-root-tmp-",
        "-root-",
    ]
    return prefixes

_ENCODED_PREFIXES = _build_encoded_prefixes()


def resolve_project_name(cwd_or_path):
    """Resolve a working directory, path, or encoded dir name to a friendly project name.

    Handles:
    - Real paths: /Users/alice/my-project/ or /root/my-project
    - Encoded dir names: -Users-alice-my-project
    - Worktree variants: openchat-v4-wt-agent-viz
    - None/empty: returns "Unknown"
    """
    if not cwd_or_path:
        return "Unknown"

    s = str(cwd_or_path).rstrip("/")

    # If it looks like a real path (starts with /), extract basename
    if s.startswith("/"):
        basename = Path(s).name
        # Direct match
        if basename in PROJECT_NAME_MAP:
            return PROJECT_NAME_MAP[basename]
        # Check if any key is a prefix of basename (worktree variants)
        for key, name in PROJECT_NAME_MAP.items():
            if basename.startswith(key):
                return name
        return basename

    # Exact match on encoded dir name (e.g. "-root", "-")
    if s in _ENCODED_DIR_MAP:
        return _ENCODED_DIR_MAP[s]

    # Encoded dir name: strip known prefixes, then match
    remainder = s
    for prefix in _ENCODED_PREFIXES:
        if s.startswith(prefix):
            remainder = s[len(prefix):]
            break

    # Direct match on remainder
    if remainder in PROJECT_NAME_MAP:
        return PROJECT_NAME_MAP[remainder]

    # Prefix match for worktree variants (e.g. openchat-v4-wt-something)
    for key, name in PROJECT_NAME_MAP.items():
        if remainder.startswith(key):
            return name

    # Fallback: return the remainder (last meaningful component)
    return remainder


def find_sessions(min_size=MIN_SESSION_SIZE, max_sessions=MAX_SESSIONS,
                  include_subagents=False):
    """Find recent JSONL sessions above min_size, sorted by recency."""
    seen = set()
    sessions = []
    for projects_dir in CLAUDE_PROJECTS_DIRS:
        if not projects_dir.exists():
            continue
        for jsonl in projects_dir.rglob("*.jsonl"):
            if not include_subagents and "subagent" in str(jsonl):
                continue
            try:
                resolved = jsonl.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                stat = jsonl.stat()
                if stat.st_size >= min_size:
                    sessions.append((stat.st_mtime, stat.st_size, jsonl))
            except OSError:
                continue
    sessions.sort(reverse=True)
    return sessions[:max_sessions]


def output_path(name, ext=".md"):
    """Return the output path for a pattern analysis."""
    return OUTPUT_DIR / f"{name}{ext}"
