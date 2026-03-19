# Claude Code Stats

Spotify Wrapped for Claude Code. Analyzes your `~/.claude/projects/` session data and generates a visual HTML report with usage stats, token costs, coding patterns, and personalized insights.

## Quick Start

```bash
npx claude-code-stats
```

Requires: Node.js 14+ and Python 3.8+ on PATH.

## What You Get

A self-contained HTML report with 20+ slides covering:

- **Sessions & hours** coded with Claude, daily streaks, peak coding days
- **Lines of code** generated across all projects
- **Token usage** and estimated cost (input, output, cache)
- **Project breakdown** with per-project stats
- **Prompting style** analysis: length, specificity, effectiveness
- **Error patterns**: taxonomy of 14 error categories
- **Retry loops**: wasted tokens from stuck patterns
- **Communication tone**: niceness score, swear tracking
- **Self-scoring bias**: how accurately Claude rates its own work
- **Tool usage**: misuse detection (Bash vs Read, etc.)
- **Coding personality**: archetype based on your usage patterns
- **Percentile ranking**: how you compare to other Claude Code users

## Options

```bash
npx claude-code-stats --author "Your Name"         # Display name (default: git user.name)
npx claude-code-stats --tz 1                        # UTC offset (default: auto-detect)
npx claude-code-stats --money 600                   # Subscription cost for ROI slide
npx claude-code-stats --money-detail "3 Max"        # Subscription description
npx claude-code-stats --sanitize                    # Anonymize project names for sharing
npx claude-code-stats --publish                     # Publish to entropy.buildingopen.org
npx claude-code-stats --help                        # Show all options
```

## Output

Generates `./wrapped.html` in your current directory and opens it in your browser.

## How It Works

1. Reads Claude Code session files from `~/.claude/projects/` (JSONL format)
2. Runs 10 pattern analyzers in parallel (pure Python, no pip dependencies)
3. Computes aggregated stats, percentiles, and a personality archetype
4. Generates a single self-contained HTML file with animated slides

All processing happens locally. No data is sent anywhere unless you use `--publish`.

## Requirements

- **Node.js** 14+ (for `npx`)
- **Python** 3.8+ (for analysis, uses only stdlib)
- **Claude Code** session data in `~/.claude/projects/`

Override the data directory with `CLAUDE_PROJECTS_DIR` env var. Supports multiple directories with `:` separator (`;` on Windows):

```bash
CLAUDE_PROJECTS_DIR="/path/to/mac-sessions:/path/to/server-sessions" npx claude-code-stats
```

## Privacy

Your session data never leaves your machine. The `--sanitize` flag strips identifying data: project names, prompt examples, swear quotes, uncensored swear words, and machine names. The `--publish` flag uploads only the final HTML report (not raw session data) to a public URL, and always auto-sanitizes.

## License

MIT
