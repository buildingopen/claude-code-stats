#!/usr/bin/env node

const { execSync, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const PLANS = {
  'pro':   { cost: '20',  label: 'Pro' },
  'max5':  { cost: '100', label: 'Max 5x' },
  'max':   { cost: '200', label: 'Max 20x' },
  'max20': { cost: '200', label: 'Max 20x' },
};

const HELP = `
Claude Recap v2 - Operational stats for Claude Code.

Usage: npx claude-recap [options]

Options:
  --days N             Last N days only (default: all time)
  --project NAME       Filter to one project
  --plan PLAN          pro | max5 | max (default: max) for ROI calc
  --json               Machine-readable JSON output
  --html FILE          Export HTML dashboard to FILE
  -v, --version        Show version
  -h, --help           Show this help

Examples:
  npx claude-recap                          Full dashboard, all time
  npx claude-recap --days 7                 Last 7 days
  npx claude-recap --project "OpenChat V4"  Single project
  npx claude-recap --plan pro               ROI with $20/mo plan
  npx claude-recap --json                   JSON output
  npx claude-recap --html recap.html        Export HTML (open in browser for PDF)
`.trim();

function parseArgs(argv) {
  const args = { _: [] };
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];
    if (arg === '-h' || arg === '--help') {
      args.help = true;
    } else if (arg === '-v' || arg === '--version') {
      args.version = true;
    } else if (arg === '--json') {
      args.json = true;
    } else if (arg === '--html' && i + 1 < argv.length) {
      args.html = argv[++i];
    } else if (arg === '--days' && i + 1 < argv.length) {
      args.days = argv[++i];
    } else if (arg === '--project' && i + 1 < argv.length) {
      args.project = argv[++i];
    } else if (arg === '--plan' && i + 1 < argv.length) {
      args.plan = argv[++i].toLowerCase();
    } else {
      args._.push(arg);
    }
    i++;
  }
  return args;
}

function findPython() {
  const candidates = [
    'python3', 'python',
    '/usr/bin/python3', '/usr/local/bin/python3',
    '/opt/homebrew/bin/python3',
    path.join(os.homedir(), '.pyenv/shims/python3'),
  ];
  for (const cmd of candidates) {
    try {
      const ver = execSync(cmd + ' --version', { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
      const match = ver.match(/Python (\d+)\.(\d+)/);
      if (match && (parseInt(match[1]) > 3 || (parseInt(match[1]) === 3 && parseInt(match[2]) >= 8))) {
        return cmd;
      }
    } catch {
      // not found or too old
    }
  }
  return null;
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) {
    console.log(HELP);
    process.exit(0);
  }

  if (args.version) {
    const pkg = require('../package.json');
    console.log('claude-recap ' + pkg.version);
    process.exit(0);
  }

  // Find Python 3.8+
  const pythonCmd = findPython();
  if (!pythonCmd) {
    console.error('Error: Python 3.8+ is required but not found.\n');
    if (process.platform === 'darwin') {
      console.error('Install via Homebrew:  brew install python3');
    } else if (process.platform === 'win32') {
      console.error('Install via winget:    winget install Python.Python.3.12');
    } else {
      console.error('Install via your package manager:');
      console.error('  Ubuntu/Debian:  sudo apt install python3');
      console.error('  Fedora:         sudo dnf install python3');
      console.error('  Arch:           sudo pacman -S python');
    }
    process.exit(1);
  }

  // Check Claude Code data exists
  const dataDir = process.env.CLAUDE_PROJECTS_DIR || path.join(os.homedir(), '.claude', 'projects');
  const dataDirs = dataDir.split(process.platform === 'win32' ? ';' : ':');
  const anyExists = dataDirs.some(d => fs.existsSync(d.trim()));
  if (!anyExists) {
    console.error('Error: No Claude Code data found at ' + dataDir);
    console.error('Make sure you have used Claude Code at least once.');
    console.error('Override with CLAUDE_PROJECTS_DIR env var if data is elsewhere.');
    process.exit(1);
  }

  // Build env vars
  const env = { ...process.env };

  // Plan cost
  if (args.plan) {
    const plan = PLANS[args.plan];
    if (!plan) {
      console.error('Unknown plan: ' + args.plan);
      console.error('Available: ' + Object.keys(PLANS).join(', '));
      process.exit(1);
    }
    env.RECAP_PLAN_COST = plan.cost;
  }

  // Build python args
  const scriptDir = path.join(__dirname, '..');
  const scriptPath = path.join(scriptDir, 'generate_recap.py');
  const pyArgs = ['-u', scriptPath];

  if (args.days) pyArgs.push('--days', args.days);
  if (args.project) pyArgs.push('--project', args.project);
  if (args.plan) pyArgs.push('--plan', args.plan);
  if (args.json) pyArgs.push('--json');
  if (args.html) pyArgs.push('--html', args.html);

  const result = spawnSync(pythonCmd, pyArgs, {
    cwd: scriptDir,
    env,
    stdio: 'inherit',
  });

  process.exit(result.status || 0);
}

main();
