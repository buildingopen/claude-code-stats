#!/usr/bin/env node

const { execSync, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const HELP = `
Claude Code Entropy - Your AI coding story, visualized.

Usage: npx claude-entropy [options]

Options:
  --author NAME        Display name (default: git user.name)
  --tz HOURS           UTC offset for local time (default: auto-detect)
  --money USD          Total subscription cost for ROI slide
  --money-detail DESC  Subscription description
  --sanitize           Anonymize project names in local HTML
  --publish            Publish to entropy.buildingopen.org (auto-sanitized)
  -v, --version        Show version
  -h, --help           Show this help

100% local analysis. Data never leaves your machine unless you --publish.
Project names, prompts, and swear words are auto-stripped before publishing.

Output: ./wrapped.html (auto-opens in browser)

For more reports (prompt-coach, user-profile, soul, portrait):
  npx claude-entropy-lab <command>
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
    } else if (arg === '--sanitize') {
      args.sanitize = true;
    } else if (arg === '--publish') {
      args.publish = true;
    } else if (arg === '--no-publish') {
      // deprecated, now the default - silently accept
      args.noPublish = true;
    } else if (arg === '--author' && i + 1 < argv.length) {
      args.author = argv[++i];
    } else if (arg === '--tz' && i + 1 < argv.length) {
      args.tz = argv[++i];
    } else if (arg === '--money' && i + 1 < argv.length) {
      args.money = argv[++i];
    } else if (arg === '--money-detail' && i + 1 < argv.length) {
      args.moneyDetail = argv[++i];
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
    console.log('claude-entropy ' + pkg.version);
    process.exit(0);
  }

  // Redirect old subcommands to claude-entropy-lab
  const OLD_COMMANDS = ['prompt-coach', 'user-profile', 'soul', 'portrait'];
  if (args._[0] && OLD_COMMANDS.includes(args._[0].toLowerCase())) {
    console.error('The "' + args._[0] + '" report has moved to a separate package.');
    console.error('Run: npx claude-entropy-lab ' + args._[0]);
    process.exit(1);
  }

  // Find a working Python 3.8+
  const pythonCmd = findPython();
  if (!pythonCmd) {
    console.error('Error: Python 3.8+ is required but not found.\n');
    if (process.platform === 'darwin') {
      console.error('Install via Homebrew:  brew install python3');
    } else if (process.platform === 'win32') {
      console.error('Install via winget:    winget install Python.Python.3.12');
      console.error('  or download from:    https://python.org/downloads/');
    } else {
      console.error('Install via your package manager:');
      console.error('  Ubuntu/Debian:  sudo apt install python3');
      console.error('  Fedora:         sudo dnf install python3');
      console.error('  Arch:           sudo pacman -S python');
    }
    process.exit(1);
  }

  // Check Claude Code data exists (supports colon-separated paths)
  const dataDir = process.env.CLAUDE_PROJECTS_DIR || path.join(os.homedir(), '.claude', 'projects');
  const dataDirs = dataDir.split(process.platform === 'win32' ? ';' : ':');
  const anyExists = dataDirs.some(d => fs.existsSync(d.trim()));
  if (!anyExists) {
    console.error('Error: No Claude Code data found at ' + dataDir);
    console.error('Make sure you have used Claude Code at least once.');
    console.error('Override with CLAUDE_PROJECTS_DIR env var if data is elsewhere.');
    console.error('Multiple directories supported with : separator (e.g. /path/one:/path/two)');
    process.exit(1);
  }

  // Build env vars
  const env = { ...process.env };

  // Author: CLI flag > env > git user.name > default
  if (args.author) {
    env.WRAPPED_AUTHOR = args.author;
  } else if (!env.WRAPPED_AUTHOR) {
    try {
      const name = execSync('git config user.name', { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
      if (name) env.WRAPPED_AUTHOR = name;
    } catch {
      // git not available or no user.name set, will use Python default
    }
  }

  // Timezone: CLI flag > env > auto-detect
  if (args.tz !== undefined) {
    env.WRAPPED_TZ_OFFSET = String(args.tz);
  } else if (!env.WRAPPED_TZ_OFFSET) {
    env.WRAPPED_TZ_OFFSET = String(-new Date().getTimezoneOffset() / 60);
  }

  if (args.money) env.WRAPPED_MONEY_PAID = args.money;
  if (args.moneyDetail) env.WRAPPED_MONEY_DETAIL = args.moneyDetail;
  if (args.sanitize) env.WRAPPED_SANITIZE = '1';

  // Build python args
  const pyArgs = [];
  if (args.publish) pyArgs.push('--publish');

  // Run the Python script from the package directory
  const scriptDir = path.join(__dirname, '..');
  const scriptPath = path.join(scriptDir, 'generate_wrapped.py');

  const result = spawnSync(pythonCmd, [scriptPath, ...pyArgs], {
    cwd: scriptDir,
    env,
    stdio: 'inherit',
  });

  if (result.status !== 0) {
    console.error('\nGeneration failed (exit code ' + result.status + ')');
    process.exit(result.status || 1);
  }

  // Copy output to CWD
  const src = path.join(scriptDir, 'dist', 'wrapped.html');
  if (!fs.existsSync(src)) {
    console.error('Error: Expected output not found at ' + src);
    process.exit(1);
  }

  const dest = path.join(process.cwd(), 'wrapped.html');
  // Don't copy if CWD is the package dir (running from repo checkout)
  if (path.resolve(src) !== path.resolve(dest)) {
    fs.copyFileSync(src, dest);
  }

  // Open in browser
  try {
    const cmd = process.platform === 'darwin' ? 'open'
              : process.platform === 'win32' ? 'start ""'
              : 'xdg-open';
    execSync(cmd + ' "' + dest + '"', { stdio: 'ignore' });
  } catch {
    // Browser open is best-effort
  }
}

main();
