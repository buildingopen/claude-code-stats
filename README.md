# Claude Recap

Operational stats dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Pure numbers, terminal or HTML export.

## Quick Start

```bash
npx claude-recap
```

Requires Python 3.8+ and Node.js 14+.

## What You Get

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CLAUDE RECAP                              Mar 1 - Mar 20
                                                     20 days
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OVERVIEW

  Sessions            482        Active Days   18/20
  Total Hours       187.0h        Projects      12
  Avg Session          23 min     Messages      14,291

COST & ROI

  API Value            $1,247.83
  Plan Cost              $200.00    (Max)
  ROI                       6.2x

MODELS / TOKENS / PROJECTS / ACTIVITY / ERRORS / TREND
```

## Options

```bash
npx claude-recap                          # Full dashboard, all time
npx claude-recap --days 7                 # Last 7 days only
npx claude-recap --days 30               # Last 30 days
npx claude-recap --project "OpenChat V4"  # Filter to one project
npx claude-recap --plan pro               # ROI calc with $20/mo
npx claude-recap --json                   # Machine-readable JSON
npx claude-recap --html recap.html        # Export HTML (Cmd+P for PDF)
npx claude-recap --help
```

## Plans for ROI

| Flag | Monthly Cost | Plan |
|------|-------------|------|
| `--plan pro` | $20 | Pro |
| `--plan max5` | $100 | Max 5x |
| `--plan max` | $200 | Max 20x (default) |

## Custom Data Directory

By default, sessions are read from `~/.claude/projects/`. Override with:

```bash
CLAUDE_PROJECTS_DIR=/path/to/projects npx claude-recap
```

Multiple directories (colon-separated):

```bash
CLAUDE_PROJECTS_DIR=/path/one:/path/two npx claude-recap
```

## JSON Output

```bash
npx claude-recap --json | python3 -m json.tool
npx claude-recap --json --days 7 > stats.json
```

## How It Works

Reads Claude Code JSONL session transcripts locally. Extracts token usage, timestamps, error patterns, and model info. Computes costs using published API pricing. All processing is local, no data leaves your machine.

## License

MIT
