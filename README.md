# Claude Code Stats

Spotify Wrapped for Claude Code. Analyzes your `~/.claude/projects/` session data and generates a visual HTML report with usage stats, token costs, coding patterns, and personalized insights.

## Quick Start

```bash
npx cc-stats
```

That's it. Author name, timezone, and plan ($200 Max) are auto-detected. Generates `./wrapped.html` and opens it in your browser.

Requires: Node.js 14+ and Python 3.8+ on PATH.

## What You Get

A self-contained HTML report with 20+ animated slides:

- **Sessions & hours** coded with Claude, daily streaks, peak coding days
- **Lines of code** generated across all projects
- **Token usage** and estimated cost (input, output, cache)
- **ROI calculation** based on your plan ($20 Pro, $100 Max 5x, $200 Max 20x)
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
npx cc-stats                    # Just works (defaults to Max 20x plan)
npx cc-stats --plan pro         # Pro plan ($20/month)
npx cc-stats --plan max5        # Max 5x ($100/month)
npx cc-stats --sanitize         # Anonymize project names for sharing
npx cc-stats --publish          # Publish to entropy.buildingopen.org
npx cc-stats --help             # Show all options
```

## How It Works

1. Reads Claude Code session files from `~/.claude/projects/` (JSONL format)
2. Runs 10 pattern analyzers in parallel (pure Python, no pip dependencies)
3. Computes aggregated stats, percentiles, and a personality archetype
4. Generates a single self-contained HTML file with animated slides

All processing happens locally. No data is sent anywhere unless you use `--publish`.

## Multiple Machines

If you use Claude Code on more than one machine, combine session directories:

```bash
CLAUDE_PROJECTS_DIR="/path/to/mac-sessions:/path/to/server-sessions" npx cc-stats
```

## Privacy

Your session data never leaves your machine. The `--sanitize` flag strips project names, prompt examples, and machine names. The `--publish` flag uploads only the final HTML report (not raw data) and always auto-sanitizes.

## License

MIT
