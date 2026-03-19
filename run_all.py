#!/usr/bin/env python3
"""
Unified runner for all transcript analysis scripts.
Runs all pattern analyses and generates the full report suite.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PATTERNS_DIR = SCRIPT_DIR / "patterns"

PATTERN_SCRIPTS = [
    ("error_taxonomy", "Error taxonomy and root causes"),
    ("hook_rejections", "Hook rejection analysis"),
    ("large_file_errors", "Large file read errors"),
    ("project_stats", "Project and repo usage stats"),
    ("prompting_style", "User prompting style"),
    ("retry_loops", "Retry loops and wasted effort"),
    ("self_scoring", "Self-scoring patterns"),
    ("session_outcomes", "Session outcome classification"),
    ("tool_misuse", "Tool misuse patterns"),
    ("communication_tone", "Communication tone and niceness"),
]


def run_pattern(name, description):
    """Run a single pattern analysis script."""
    script = PATTERNS_DIR / f"{name}.py"
    if not script.exists():
        print(f"  SKIP: {script} not found")
        return False

    print(f"  Running {description}...")
    start = time.time()
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=600,
        cwd=str(SCRIPT_DIR),
    )
    elapsed = time.time() - start

    if result.returncode != 0:
        print(f"  FAIL ({elapsed:.1f}s): {result.stderr[:200]}")
        return False

    print(f"  OK ({elapsed:.1f}s)")
    return True


def run_all_patterns():
    """Run all pattern analysis scripts."""
    print(f"Running {len(PATTERN_SCRIPTS)} pattern analyses...\n")
    results = {}
    total_start = time.time()

    for name, desc in PATTERN_SCRIPTS:
        results[name] = run_pattern(name, desc)

    total_elapsed = time.time() - total_start
    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)

    print(f"\nDone in {total_elapsed:.0f}s: {passed} passed, {failed} failed")
    return results


def run_generate_findings():
    """Generate FINDINGS.md from pattern outputs."""
    print("\nGenerating FINDINGS.md...")
    script = SCRIPT_DIR / "generate_findings.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=30,
        cwd=str(SCRIPT_DIR),
    )
    if result.returncode != 0:
        print(f"  FAIL: {result.stderr[:200]}")
        return False
    print(f"  {result.stdout.strip()}")
    return True


def run_gemini_analysis(mode="batch", **kwargs):
    """Run Gemini-powered analysis."""
    print(f"\nRunning Gemini {mode} analysis...")
    cmd = [sys.executable, str(SCRIPT_DIR / "analyze.py"), mode]
    for k, v in kwargs.items():
        cmd.extend([f"--{k.replace('_', '-')}", str(v)])

    result = subprocess.run(cmd, timeout=600, cwd=str(SCRIPT_DIR))
    return result.returncode == 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run transcript analysis suite")
    parser.add_argument("--patterns-only", action="store_true",
                        help="Only run pattern scripts (no Gemini)")
    parser.add_argument("--gemini-only", action="store_true",
                        help="Only run Gemini analysis")
    parser.add_argument("--wrapped", action="store_true",
                        help="Generate wrapped.html after patterns")
    parser.add_argument("--soul", action="store_true",
                        help="Generate soul.html personality profile")
    parser.add_argument("--pattern", type=str,
                        help="Run a specific pattern (e.g., 'self_scoring')")
    parser.add_argument("--mode", choices=["deep", "batch", "local"],
                        default="batch", help="Gemini analysis mode")
    args = parser.parse_args()

    if args.wrapped:
        print("\nGenerating wrapped.html...")
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "generate_wrapped.py")],
            timeout=600, cwd=str(SCRIPT_DIR),
        )
        sys.exit(result.returncode)

    if args.soul:
        print("\nGenerating soul.html...")
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "generate_soul.py")],
            timeout=600, cwd=str(SCRIPT_DIR),
        )
        sys.exit(result.returncode)

    if args.pattern:
        # Run single pattern
        match = [(n, d) for n, d in PATTERN_SCRIPTS if n == args.pattern]
        if match:
            run_pattern(*match[0])
        else:
            print(f"Unknown pattern: {args.pattern}")
            print(f"Available: {', '.join(n for n, _ in PATTERN_SCRIPTS)}")
            sys.exit(1)
    elif args.gemini_only:
        run_gemini_analysis(args.mode)
    elif args.patterns_only:
        run_all_patterns()
        run_generate_findings()
    else:
        # Run everything
        run_all_patterns()
        run_generate_findings()
        run_gemini_analysis(args.mode)
