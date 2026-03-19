import html as html_mod
import os
import re
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from generate_wrapped import (
    fmt_number, fmt_compact, hour_label, censor_word,
    compute_aggregates, compute_rules, compute_archetype, get_proj_dir_name,
    compute_percentile, compute_percentiles, generate_html,
    sanitize_html_for_publish, verify_sanitization,
    collect_data,
)


class TestFmtNumber:
    def test_small(self):
        assert fmt_number(42) == "42"

    def test_thousands(self):
        assert fmt_number(1234) == "1,234"

    def test_millions(self):
        assert fmt_number(1234567) == "1,234,567"


class TestFmtCompact:
    def test_small(self):
        assert fmt_compact(42) == "42"

    def test_thousands(self):
        assert fmt_compact(1500) == "2K"

    def test_millions(self):
        assert fmt_compact(1_500_000) == "1.5M"

    def test_billions(self):
        assert fmt_compact(11_300_000_000) == "11.3B"


class TestHourLabel:
    def test_midnight(self):
        assert hour_label(0) == "12am"

    def test_noon(self):
        assert hour_label(12) == "12pm"

    def test_morning(self):
        assert hour_label(9) == "9am"

    def test_evening(self):
        assert hour_label(18) == "6pm"


class TestCensorWord:
    def test_known(self):
        assert censor_word("fuck") == "f**k"
        assert censor_word("shit") == "sh*t"

    def test_unknown(self):
        assert censor_word("darn") == "darn"


class TestGetProjDirName:
    def test_standard_path(self):
        p = Path("/root/.claude/projects/my-project/session.jsonl")
        assert get_proj_dir_name(p) == "my-project"

    def test_no_projects_dir(self):
        p = Path("/tmp/session.jsonl")
        assert get_proj_dir_name(p) == "unknown"


class TestComputeAggregates:
    """Test compute_aggregates with minimal synthetic data."""

    def _make_data(self, **overrides):
        base = {
            "outcomes": [],
            "tones": [],
            "misuses": [],
            "scoring_instances": [],
            "error_sessions": [],
            "loop_findings": [],
            "project_sessions": [],
            "all_user_texts": [],
            "session_word_counts": {},
            "session_count": 0,
        }
        base.update(overrides)
        return base

    def test_empty_data(self):
        d = compute_aggregates(self._make_data())
        assert d["sessions"] == 0
        assert d["hours"] == 0
        assert d["loc"] == 0
        assert d["success_pct"] == 0
        assert d["total_errors"] == 0

    def test_session_count(self):
        d = compute_aggregates(self._make_data(session_count=42))
        assert d["sessions"] == 42

    def test_prompting_stats(self):
        texts = ["fix it", "yes", "do the thing please", "a longer prompt with more words in it"]
        d = compute_aggregates(self._make_data(all_user_texts=texts))
        assert d["median_words"] > 0
        assert 0 <= d["pct_short"] <= 100

    def test_outcome_success(self):
        outcomes = [
            {"outcome": "SUCCESS", "start": datetime(2026, 1, 1), "end": datetime(2026, 1, 2),
             "duration_min": 60, "loc_changed": 100, "commits": 1, "deployments": 0,
             "bash_commands": 5, "files_edited": 3, "total_tool_uses": 10, "error_rate": 0.1},
            {"outcome": "FAILURE", "start": datetime(2026, 1, 2), "end": datetime(2026, 1, 3),
             "duration_min": 30, "loc_changed": 0, "commits": 0, "deployments": 0,
             "bash_commands": 2, "files_edited": 0, "total_tool_uses": 5, "error_rate": 0.5},
        ]
        d = compute_aggregates(self._make_data(outcomes=outcomes, session_count=2))
        assert d["success_pct"] == 50
        assert d["loc"] == 100
        assert d["commits"] == 1

    def test_niceness_defaults_without_tones(self):
        d = compute_aggregates(self._make_data())
        assert d["niceness_score"] == 5.0
        assert d["user_swears_total"] == 0

    def test_tokens(self):
        sessions = [
            {"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 200,
             "cache_creation_tokens": 100, "message_count": 10, "project_name": "Test",
             "model": "claude-sonnet-4-20250514"}
        ]
        d = compute_aggregates(self._make_data(project_sessions=sessions))
        assert d["total_tokens"] == 1800
        assert d["total_messages"] == 10


class TestComputeRules:
    def test_fallback_rules(self):
        d = {"median_words": 20, "pct_perfect": 0, "switch_rate": 50,
             "sessions": 10, "sessions_with_loops": 0, "misuse_total": 0,
             "short_prompt_errors": 0, "long_prompt_errors": 0, "error_ratio": "N/A"}
        rules = compute_rules(d)
        assert len(rules) == 3
        # Should be fallback rules
        assert "Run tests" in rules[0][0]

    def test_short_prompts_rule(self):
        # Rule only fires when short prompts cause more errors than long
        d = {"median_words": 5, "pct_perfect": 0, "switch_rate": 50,
             "sessions": 10, "sessions_with_loops": 0, "misuse_total": 0,
             "short_prompt_errors": 20, "long_prompt_errors": 5, "error_ratio": "N/A"}
        rules = compute_rules(d)
        assert any("longer prompts" in r[0] for r in rules)

    def test_short_prompts_rule_not_when_contradictory(self):
        # Rule should NOT fire when long prompts cause more errors
        d = {"median_words": 5, "pct_perfect": 0, "switch_rate": 50,
             "sessions": 10, "sessions_with_loops": 0, "misuse_total": 0,
             "short_prompt_errors": 5, "long_prompt_errors": 20, "error_ratio": "N/A"}
        rules = compute_rules(d)
        assert not any("longer prompts" in r[0] for r in rules)

    def test_misuse_rule(self):
        d = {"median_words": 20, "pct_perfect": 0, "switch_rate": 50,
             "sessions": 10, "sessions_with_loops": 0, "misuse_total": 500,
             "short_prompt_errors": 0, "long_prompt_errors": 0, "error_ratio": "N/A"}
        rules = compute_rules(d)
        assert any("right tool" in r[0] for r in rules)

    def test_max_three_rules(self):
        d = {"median_words": 3, "pct_perfect": 50, "switch_rate": 2,
             "sessions": 100, "sessions_with_loops": 50, "misuse_total": 1000,
             "short_prompt_errors": 10, "long_prompt_errors": 5, "error_ratio": "2.0x"}
        rules = compute_rules(d)
        assert len(rules) == 3


class TestComputeArchetype:
    """Test archetype computation."""

    def _base_metrics(self, **overrides):
        base = {
            "sessions": 100, "session_categories": {"BUILD": 40, "FIX": 30, "EXPLORE": 20, "MIXED": 10},
            "success_pct": 70, "median_words": 15, "deployments": 10,
            "pct_perfect": 20, "sessions_with_loops": 10, "bugs_after_high": 3,
            "niceness_score": 5.0, "please_count": 50, "pct_short": 40,
            "retried_pct": 30, "switched_pct": 15, "gave_up_pct": 10,
        }
        base.update(overrides)
        return base

    def test_returns_tuple(self):
        d = self._base_metrics()
        result = compute_archetype(d)
        assert len(result) == 5
        key, name, line, share, stats_html = result
        assert key in ("firefighter", "architect", "speedrunner", "perfectionist", "whisperer", "commander")
        assert name.startswith("THE ")
        assert len(line) > 0
        assert len(share) > 0

    def test_firefighter(self):
        d = self._base_metrics(
            session_categories={"FIX": 60, "BUILD": 20, "EXPLORE": 10, "MIXED": 10},
            success_pct=30,
        )
        key, name, *_ = compute_archetype(d)
        assert key == "firefighter"
        assert name == "THE FIREFIGHTER"

    def test_architect(self):
        d = self._base_metrics(
            session_categories={"BUILD": 70, "FIX": 10, "EXPLORE": 10, "MIXED": 10},
            median_words=35, success_pct=85,
        )
        key, name, *_ = compute_archetype(d)
        assert key == "architect"

    def test_commander(self):
        d = self._base_metrics(
            median_words=3, niceness_score=2.0, pct_short=90,
            session_categories={"BUILD": 25, "FIX": 25, "EXPLORE": 25, "MIXED": 25},
        )
        key, name, *_ = compute_archetype(d)
        assert key == "commander"

    def test_whisperer(self):
        d = self._base_metrics(
            niceness_score=9.0, success_pct=85, please_count=200,
            session_categories={"BUILD": 25, "FIX": 25, "EXPLORE": 25, "MIXED": 25},
            median_words=15,
        )
        key, name, *_ = compute_archetype(d)
        assert key == "whisperer"

    def test_perfectionist(self):
        d = self._base_metrics(
            pct_perfect=60, sessions_with_loops=40,
            session_categories={"BUILD": 25, "FIX": 25, "EXPLORE": 25, "MIXED": 25},
            niceness_score=5.0, median_words=15, success_pct=50,
        )
        key, name, *_ = compute_archetype(d)
        assert key == "perfectionist"

    def test_stats_html_not_empty(self):
        d = self._base_metrics()
        _, _, _, _, stats_html = compute_archetype(d)
        assert "div" in stats_html

    def test_empty_data(self):
        d = {"sessions": 0, "session_categories": {}, "success_pct": 0,
             "median_words": 0, "deployments": 0, "pct_perfect": 0,
             "sessions_with_loops": 0, "niceness_score": 5.0, "please_count": 0,
             "pct_short": 0, "retried_pct": 0, "switched_pct": 0,
             "gave_up_pct": 0, "bugs_after_high": 0}
        result = compute_archetype(d)
        assert len(result) == 5


class TestNewAggregations:
    """Test that new aggregation fields are computed."""

    def _make_data(self, **overrides):
        base = {
            "outcomes": [], "tones": [], "misuses": [],
            "scoring_instances": [], "error_sessions": [],
            "loop_findings": [], "project_sessions": [],
            "all_user_texts": [], "session_word_counts": {},
            "session_count": 0,
        }
        base.update(overrides)
        return base

    def test_session_categories(self):
        outcomes = [
            {"outcome": "SUCCESS", "category": "BUILD",
             "start": datetime(2026, 1, 1), "end": datetime(2026, 1, 2),
             "duration_min": 60, "loc_changed": 100, "loc_added": 80, "loc_removed": 20,
             "commits": 2, "deployments": 1, "bash_commands": 5, "files_edited": 3,
             "total_tool_uses": 10, "error_rate": 0.1},
            {"outcome": "SUCCESS", "category": "FIX",
             "start": datetime(2026, 1, 2), "end": datetime(2026, 1, 3),
             "duration_min": 30, "loc_changed": 50, "loc_added": 30, "loc_removed": 20,
             "commits": 1, "deployments": 0, "bash_commands": 3, "files_edited": 2,
             "total_tool_uses": 8, "error_rate": 0.2},
        ]
        d = compute_aggregates(self._make_data(outcomes=outcomes, session_count=2))
        assert d["session_categories"]["BUILD"] == 1
        assert d["session_categories"]["FIX"] == 1
        assert d["loc_added"] == 110
        assert d["loc_removed"] == 40
        assert d["commits_per_deploy"] == 3  # 3 commits / 1 deploy

    def test_abandoned_count(self):
        outcomes = [
            {"outcome": "SUCCESS", "category": "BUILD", "abandoned": False,
             "start": datetime(2026, 1, 1), "end": datetime(2026, 1, 2),
             "duration_min": 60, "loc_changed": 100, "loc_added": 80, "loc_removed": 20,
             "commits": 1, "deployments": 0, "bash_commands": 5, "files_edited": 3,
             "total_tool_uses": 10, "error_rate": 0.1},
            {"outcome": "FAILURE", "category": "FIX", "abandoned": True,
             "start": datetime(2026, 1, 2), "end": datetime(2026, 1, 3),
             "duration_min": 30, "loc_changed": 10, "loc_added": 5, "loc_removed": 5,
             "commits": 0, "deployments": 0, "bash_commands": 2, "files_edited": 1,
             "total_tool_uses": 5, "error_rate": 0.5},
        ]
        d = compute_aggregates(self._make_data(outcomes=outcomes, session_count=2))
        assert d["abandoned_count"] == 1

    def test_error_action_pcts(self):
        """switched_pct, retried_pct, gave_up_pct are computed from error post-actions."""
        outcomes = [
            {"outcome": "SUCCESS", "start": datetime(2026, 1, 1), "end": datetime(2026, 1, 2),
             "duration_min": 60, "loc_changed": 100, "commits": 1, "deployments": 0,
             "bash_commands": 5, "files_edited": 3, "total_tool_uses": 10, "error_rate": 0.1},
        ]
        d = compute_aggregates(self._make_data(outcomes=outcomes, session_count=1))
        # With no error_sessions, all pcts default to 0
        assert d["switched_pct"] == 0
        assert d["retried_pct"] == 0
        assert d["gave_up_pct"] == 0

    def test_prompt_success_pcts_default(self):
        """short/long_prompt_success_pct default to 0 with no data."""
        outcomes = [
            {"outcome": "SUCCESS", "start": datetime(2026, 1, 1), "end": datetime(2026, 1, 2),
             "duration_min": 60, "loc_changed": 100, "commits": 1, "deployments": 0,
             "bash_commands": 5, "files_edited": 3, "total_tool_uses": 10, "error_rate": 0.1},
        ]
        d = compute_aggregates(self._make_data(outcomes=outcomes, session_count=1))
        assert d["short_prompt_success_pct"] == 0
        assert d["long_prompt_success_pct"] == 0

    def test_wasted_cost(self):
        outcomes = [
            {"outcome": "SUCCESS", "start": datetime(2026, 1, 1), "end": datetime(2026, 1, 2),
             "duration_min": 60, "loc_changed": 100, "commits": 1, "deployments": 0,
             "bash_commands": 5, "files_edited": 3, "total_tool_uses": 10, "error_rate": 0.1},
        ]
        sessions = [
            {"input_tokens": 500000, "output_tokens": 200000, "cache_read_tokens": 100000,
             "cache_creation_tokens": 50000, "message_count": 50, "project_name": "Test",
             "model": "claude-sonnet-4-20250514"}
        ]
        loops = [{"estimated_tokens": 50000, "session_slug": "s1", "tool": "Edit", "count": 5}]
        d = compute_aggregates(self._make_data(
            outcomes=outcomes, session_count=1,
            project_sessions=sessions, loop_findings=loops
        ))
        assert d["wasted_tokens"] == 50000
        # wasted_cost depends on total_cost being > 0
        assert "wasted_cost" in d


class TestComputePercentile:
    """Test percentile interpolation from benchmark tables."""

    def test_zero_value(self):
        assert compute_percentile(0, "sessions_monthly") == 0

    def test_below_p25(self):
        # 4 sessions/month: halfway to p25 threshold of 8
        p = compute_percentile(4, "sessions_monthly")
        assert 1 <= p <= 25

    def test_exact_p25(self):
        p = compute_percentile(8, "sessions_monthly")
        assert p == 25

    def test_exact_p50(self):
        p = compute_percentile(20, "sessions_monthly")
        assert p == 50

    def test_exact_p99(self):
        p = compute_percentile(400, "sessions_monthly")
        assert p == 99

    def test_above_p99(self):
        p = compute_percentile(1000, "sessions_monthly")
        assert p == 99

    def test_interpolation_midpoint(self):
        # Midpoint between p25=8 and p50=20 -> should be ~37
        p = compute_percentile(14, "sessions_monthly")
        assert 30 <= p <= 45

    def test_high_tokens(self):
        # 10B tokens/mo: between p95=1.5B and p99=5B
        p = compute_percentile(3e9, "tokens_monthly")
        assert 95 <= p <= 99

    def test_success_pct(self):
        # 80% success: between p90=75 and p95=85
        p = compute_percentile(80, "success_pct")
        assert 90 <= p <= 95

    def test_all_benchmark_keys(self):
        """Every benchmark key works without error."""
        from generate_wrapped import BENCHMARKS
        for key in BENCHMARKS:
            p = compute_percentile(100, key)
            assert 0 <= p <= 99


class TestComputePercentiles:
    """Test the full percentiles computation from metrics dict."""

    def test_returns_all_keys(self):
        d = {
            "days": 30, "sessions": 100, "hours": 200, "loc": 10000,
            "total_tokens": 1e9, "total_cost": 500, "success_pct": 70,
            "deployments": 50,
        }
        pcts = compute_percentiles(d)
        assert "sessions" in pcts
        assert "hours" in pcts
        assert "loc" in pcts
        assert "tokens" in pcts
        assert "cost" in pcts
        assert "success" in pcts
        assert "deployments" in pcts
        assert "overall" in pcts

    def test_overall_is_weighted_average(self):
        d = {
            "days": 30, "sessions": 0, "hours": 0, "loc": 0,
            "total_tokens": 0, "total_cost": 0, "success_pct": 0,
            "deployments": 0,
        }
        pcts = compute_percentiles(d)
        assert pcts["overall"] == 0

    def test_high_usage(self):
        d = {
            "days": 30, "sessions": 300, "hours": 2000, "loc": 200000,
            "total_tokens": 10e9, "total_cost": 10000, "success_pct": 90,
            "deployments": 3000,
        }
        pcts = compute_percentiles(d)
        assert pcts["overall"] >= 90
        for key in ("sessions", "hours", "loc", "tokens"):
            assert pcts[key] >= 90

    def test_normalizes_to_monthly(self):
        # 60 days with 100 sessions = 50/month, which is p75
        d = {
            "days": 60, "sessions": 100, "hours": 0, "loc": 0,
            "total_tokens": 0, "total_cost": 0, "success_pct": 0,
            "deployments": 0,
        }
        pcts = compute_percentiles(d)
        assert pcts["sessions"] == 75

    def test_monthly_normalization_fairness(self):
        """Same usage rate over 1 month vs 3 months should give similar percentiles."""
        base = {"hours": 0, "success_pct": 70}

        d_1mo = {**base, "days": 30, "sessions": 50, "loc": 5000,
                 "total_tokens": 150e6, "total_cost": 150, "deployments": 30}
        d_3mo = {**base, "days": 90, "sessions": 150, "loc": 15000,
                 "total_tokens": 450e6, "total_cost": 450, "deployments": 90}

        pcts_1 = compute_percentiles(d_1mo)
        pcts_3 = compute_percentiles(d_3mo)

        # All time-dependent metrics should produce identical percentiles
        for key in ("sessions", "loc", "tokens", "cost", "deployments"):
            assert pcts_1[key] == pcts_3[key], f"{key}: 1mo={pcts_1[key]} vs 3mo={pcts_3[key]}"

    def test_loc_normalized_not_raw(self):
        """LOC is now monthly-normalized. 30K LOC over 3 months = 10K/mo, not 30K raw."""
        d = {
            "days": 90, "sessions": 0, "hours": 0, "loc": 30000,
            "total_tokens": 0, "total_cost": 0, "success_pct": 0,
            "deployments": 0,
        }
        pcts = compute_percentiles(d)
        # 10K/mo is between p75=5000 and p90=15000
        assert 75 <= pcts["loc"] <= 90


class TestPercentileDisclaimer:
    """Test that disclaimer text appears in generated HTML."""

    def _make_minimal_data(self):
        """Return a minimal data dict suitable for generate_html."""
        from datetime import datetime
        from collections import Counter
        return {
            "sessions": 500, "days": 90, "start_date": datetime(2025, 1, 1),
            "end_date": datetime(2025, 3, 31), "hours": 2000, "hours_days": 83,
            "longest_session_hours": 12, "longest_session_days": 0.5,
            "loc": 100000, "loc_added": 80000, "loc_removed": 20000,
            "files_edited_count": 500, "commits": 300, "deployments": 100,
            "bash_commands": 5000, "commits_per_deploy": 3, "abandoned_count": 10,
            "session_categories": Counter({"BUILD": 200, "FIX": 150, "EXPLORE": 150}),
            "top_projects": [("Project A", 200)], "median_words": 15,
            "pct_short": 30, "short_prompt_errors": 5, "long_prompt_errors": 3,
            "error_ratio": "1.7x", "error_ratio_text": "1.7x more errors when terse",
            "prompt_examples": ["fix it"], "total_tokens": 10_000_000_000,
            "tokens_display": 10.0, "tokens_suffix": "B",
            "tokens_reading_comparison": "5 years of reading",
            "total_messages": 10000, "total_cost": 5000,
            "total_errors": 500, "error_categories": Counter({"COMMAND_FAILED": 300, "EDIT_FAILED": 200}),
            "error_files_count": 100, "switch_rate": 15, "switched_pct": 15,
            "retried_pct": 60, "gave_up_pct": 25, "wasted_tokens": 50_000_000,
            "wasted_tokens_m": 50, "loop_count": 100, "sessions_with_loops": 50,
            "worst_loop": '"Edit" 8 times in a row', "wasted_cost": 100,
            "avg_score": 8.5, "median_score": 9, "pct_perfect": 40,
            "total_scores": 200, "bugs_after_high": 15,
            "success_pct": 76, "full_success_pct": 60, "partial_success_pct": 16,
            "misuse_total": 50, "misuse_top": [("grep via Bash", 30)],
            "niceness_score": 5.2, "user_nice_total": 200, "user_harsh_total": 50,
            "user_swears_total": 20, "assistant_nice_total": 1000,
            "assistant_swears_total": 5, "please_count": 100,
            "nice_to_harsh": "4.0x", "claude_nice_ratio": 5.0,
            "top_swears": [("damn", 10)], "swears_by_hour": [0]*24,
            "nice_by_hour": [0]*24, "swear_peak_hour": 22, "swear_peak_count": 5,
            "nice_peak_hour": 10, "nice_peak_count": 20,
            "swear_example_quote": "", "total_user_msgs": 5000, "swear_pct": 0.4,
            "machine_counts": Counter({"AX41": 300, "Mac": 200}),
            "short_prompt_success_pct": 70, "long_prompt_success_pct": 80,
        }

    def test_overall_disclaimer_present(self):
        d = self._make_minimal_data()
        rules = [("Rule 1", "Desc 1"), ("Rule 2", "Desc 2"), ("Rule 3", "Desc 3")]
        pcts = compute_percentiles(d)
        archetype = compute_archetype(d, pcts)
        html = generate_html(d, rules, archetype, pcts)
        assert "percentile-disclaimer" in html
        assert "Estimated from community benchmarks" in html

    def test_badge_has_asterisk_and_tooltip(self):
        d = self._make_minimal_data()
        rules = [("Rule 1", "Desc 1"), ("Rule 2", "Desc 2"), ("Rule 3", "Desc 3")]
        pcts = compute_percentiles(d)
        archetype = compute_archetype(d, pcts)
        html = generate_html(d, rules, archetype, pcts)
        # At least one badge should have asterisk and tooltip
        assert 'title="Estimated from community benchmarks"' in html
        assert "%*</span>" in html


class TestSanitizeHtmlForPublish:
    """Test HTML sanitization for public sharing."""

    def test_project_names_replaced(self):
        html = '<span class="bar-label">MySecret</span><span class="bar-label">AnotherProj</span>'
        sanitized, counts = sanitize_html_for_publish(html)
        assert "MySecret" not in sanitized
        assert "AnotherProj" not in sanitized
        assert 'Project 1' in sanitized
        assert 'Project 2' in sanitized
        assert counts["projects"] == 2

    def test_sessions_on_text_sanitized(self):
        html = '<div>42 sessions on MySecret</div>'
        sanitized, _ = sanitize_html_for_publish(html)
        assert "MySecret" not in sanitized
        assert "sessions on Project 1" in sanitized

    def test_prompt_examples_stripped(self):
        html = '''<div class="fade-up prompt-examples">
            <span class="prompt-example">&quot;fix it&quot;</span>
            <span class="prompt-example">&quot;run tests&quot;</span>
        </div>'''
        sanitized, counts = sanitize_html_for_publish(html)
        assert '<span class="prompt-example">' not in sanitized
        assert counts["prompts"] == 2

    def test_prompt_strip_robust_with_nested_content(self):
        """Prompt stripping works even if there's extra markup inside."""
        html = '<span class="prompt-example">&quot;hello world&quot;</span>'
        sanitized, counts = sanitize_html_for_publish(html)
        assert "hello world" not in sanitized
        assert counts["prompts"] == 1

    def test_swear_quote_removed(self):
        html = '<div class="fade-up label-detail">"Why the hell is this broken"</div>'
        sanitized, counts = sanitize_html_for_publish(html)
        assert "hell is this broken" not in sanitized
        assert counts["swear_quote"] == 1

    def test_swear_quote_no_match_without_quotes(self):
        html = '<div class="fade-up label-detail">Some normal text</div>'
        sanitized, counts = sanitize_html_for_publish(html)
        assert "Some normal text" in sanitized
        assert counts["swear_quote"] == 0

    def test_swear_pills_stripped(self):
        html = '''<div class="fade-up swear-pills">
            <div class="swear-pill"><span class="censored">f**k</span><span class="uncensored">fuck</span><span class="swear-count">42x</span></div>
        </div>'''
        sanitized, counts = sanitize_html_for_publish(html)
        assert "f**k" not in sanitized
        assert "fuck" not in sanitized
        assert "42x" not in sanitized
        assert '<div class="swear-pill">' not in sanitized
        assert counts["swear_words"] == 1

    def _wrap_split_container(self, inner):
        """Wrap machine spans in the split labels container div."""
        return f'<div style="display:flex; gap:2rem; margin-top:0.5rem; font-size:0.7rem; color:var(--text-muted);">{inner}</div>'

    def test_machine_names_replaced(self):
        html = self._wrap_split_container('<span>300 AX41</span><span>112 Mac</span>')
        sanitized, counts = sanitize_html_for_publish(html)
        import re
        assert re.search(r'<span>\d+ AX41</span>', sanitized) is None
        assert re.search(r'<span>\d+ Mac</span>', sanitized) is None
        assert "Machine 1" in sanitized
        assert "Machine 2" in sanitized
        assert counts["machines"] == 2

    def test_known_machine_names(self):
        for name in ["AX41", "Mac", "Linux", "Other", "Windows", "WSL", "Docker"]:
            html = self._wrap_split_container(f'<span>10 {name}</span>')
            sanitized, counts = sanitize_html_for_publish(html)
            assert f'<span>10 {name}</span>' not in sanitized
            assert counts["machines"] == 1

    def test_machine_regex_scoped_to_container(self):
        """Machine regex should NOT match spans outside the split container."""
        html = '<span>10 AX41</span>'  # No container
        sanitized, counts = sanitize_html_for_publish(html)
        assert "AX41" in sanitized  # Left untouched
        assert counts["machines"] == 0

    def test_machine_regex_skips_long_unknown_words(self):
        """Machine regex shouldn't match random long words even in container."""
        html = self._wrap_split_container('<span>10 SomethingVeryLongAndUnlikely</span>')
        sanitized, counts = sanitize_html_for_publish(html)
        # All words in the container are replaced (no length filter since scoped)
        # The regex now replaces all <span>N Word</span> inside the container
        assert counts["machines"] == 1

    def test_html_escaped_project_name_stripped(self):
        """Project names with special chars are HTML-escaped, sanitizer still strips them."""
        html = '<span class="bar-label">My&amp;Project</span>'
        sanitized, counts = sanitize_html_for_publish(html)
        assert "My&amp;Project" not in sanitized
        assert "Project 1" in sanitized
        assert counts["projects"] == 1

    def test_html_escaped_prompt_stripped(self):
        """Prompt examples with special chars are HTML-escaped, sanitizer still strips them."""
        html = '<span class="prompt-example">&quot;fix &lt;div&gt;&quot;</span>'
        sanitized, counts = sanitize_html_for_publish(html)
        assert "fix" not in sanitized
        assert counts["prompts"] == 1

    def test_empty_html(self):
        sanitized, counts = sanitize_html_for_publish("")
        assert sanitized == ""
        assert all(v == 0 for v in counts.values())

    def test_idempotent(self):
        """Running sanitization twice produces the same result."""
        html = '<span class="bar-label">Proj</span>\n'
        html += '<span class="prompt-example">&quot;hi&quot;</span>\n'
        html += self._wrap_split_container('<span>5 Mac</span>')
        first, _ = sanitize_html_for_publish(html)
        second, _ = sanitize_html_for_publish(first)
        assert first == second

    def test_full_realistic_html(self):
        """Test with HTML matching real template patterns."""
        html = '''
<span class="bar-label">OpenPaper</span>
<span class="bar-label">Rocketlist</span>
<div>42 sessions on OpenPaper</div>
<div class="fade-up prompt-examples">
  <span class="prompt-example">&quot;fix it&quot;</span>
  <span class="prompt-example">&quot;run tests&quot;</span>
</div>
<div class="fade-up label-detail">"Why the hell"</div>
<div class="fade-up swear-pills">
  <div class="swear-pill"><span class="censored">f**k</span><span class="uncensored">fuck</span><span class="swear-count">42x</span></div>
  <div class="swear-pill"><span class="censored">sh*t</span><span class="uncensored">shit</span><span class="swear-count">10x</span></div>
</div>
<div style="display:flex; gap:2rem; margin-top:0.5rem; font-size:0.7rem; color:var(--text-muted);">
  <span>300 AX41</span>
  <span>112 Mac</span>
</div>'''
        sanitized, counts = sanitize_html_for_publish(html)
        # Nothing private survives
        for private in ["OpenPaper", "Rocketlist", "fix it", "run tests",
                        "hell", "f**k", "fuck", "sh*t", "shit"]:
            assert private not in sanitized, f"'{private}' leaked!"
        assert counts["projects"] == 2
        assert counts["prompts"] == 2
        assert counts["swear_quote"] == 1
        assert counts["swear_words"] == 2
        assert counts["machines"] == 2


class TestVerifySanitization:
    """Test the defense-in-depth verification function."""

    def test_clean_html_passes(self):
        html = '<span class="bar-label">Project 1</span><span>5 Machine 1</span>'
        leaks = verify_sanitization(html, ["RealProject"], ["AX41"])
        assert leaks == []

    def test_project_name_leak_detected(self):
        html = '<span class="bar-label">RealProject</span>'
        leaks = verify_sanitization(html, ["RealProject"], [])
        assert len(leaks) == 1
        assert "RealProject" in leaks[0]

    def test_machine_name_leak_detected(self):
        html = '<span>5 AX41</span>'
        leaks = verify_sanitization(html, [], ["AX41"])
        assert len(leaks) == 1
        assert "AX41" in leaks[0]

    def test_prompt_example_leak_detected(self):
        html = '<span class="prompt-example">&quot;fix it&quot;</span>'
        leaks = verify_sanitization(html, [], [], prompt_examples=["fix it"])
        assert len(leaks) >= 1

    def test_swear_quote_leak_detected(self):
        html = '<div class="fade-up label-detail">"Why the hell is this broken"</div>'
        leaks = verify_sanitization(html, [], [], swear_quote="Why the hell is this broken")
        assert any("swear quote" in l for l in leaks)

    def test_swear_quote_clean_passes(self):
        html = '<div class="fade-up label-detail"></div>'
        leaks = verify_sanitization(html, [], [], swear_quote="Why the hell")
        assert not any("swear quote" in l for l in leaks)

    def test_project_name_context_aware(self):
        """Common words like 'art' shouldn't false-positive from 'chart'."""
        html = '<span class="bar-label">Project 1</span> chart display'
        leaks = verify_sanitization(html, ["art"], [])
        assert leaks == []

    def test_html_escaped_project_name_detected(self):
        """Project names with special chars are HTML-escaped but still caught."""
        html = '<span class="bar-label">My&amp;Project</span>'
        leaks = verify_sanitization(html, ["My&Project"], [])
        assert len(leaks) == 1

    def test_uncensored_span_detected(self):
        html = '<span class="uncensored">word</span>'
        leaks = verify_sanitization(html, [], [])
        assert any("uncensored" in l for l in leaks)

    def test_censored_span_detected(self):
        html = '<span class="censored">w**d</span>'
        leaks = verify_sanitization(html, [], [])
        assert any("censored" in l for l in leaks)

    def test_multiple_leaks_all_reported(self):
        html = '<span class="bar-label">Proj1</span><span>5 AX41</span><span class="uncensored">x</span>'
        leaks = verify_sanitization(html, ["Proj1"], ["AX41"])
        assert len(leaks) == 3  # project + machine + uncensored

    def test_empty_names_no_false_positives(self):
        html = '<span class="bar-label">Project 1</span>'
        leaks = verify_sanitization(html, [], [])
        assert leaks == []


import pytest

class TestIntegrationSanitizePipeline:
    """Integration test: full pipeline -> sanitize -> verify no leaks."""

    @pytest.fixture(autouse=True)
    def _check_session_data(self):
        """Skip if no Claude Code session data available."""
        data_dir = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
        dirs = data_dir.split(":")
        if not any(os.path.isdir(d.strip()) for d in dirs):
            pytest.skip("No Claude Code session data available")

    def test_full_pipeline_sanitization(self):
        """Run collect -> aggregate -> generate -> sanitize -> verify end-to-end."""
        data = collect_data(max_sessions=50)
        if data["session_count"] == 0:
            pytest.skip("No sessions found")

        d = compute_aggregates(data)
        rules = compute_rules(d)
        percentiles = compute_percentiles(d)
        archetype = compute_archetype(d, percentiles)
        html = generate_html(d, rules, archetype, percentiles)

        # Collect private data that should be stripped
        project_names = [name for name, _ in d.get("top_projects", [])]
        machine_names = list(d.get("machine_counts", {}).keys())
        prompt_examples = d.get("prompt_examples", [])
        swear_quote = d.get("swear_example_quote", "")

        # Sanitize
        sanitized, counts = sanitize_html_for_publish(html)

        # Verify no private data survives
        leaks = verify_sanitization(sanitized, project_names, machine_names,
                                    prompt_examples=prompt_examples, swear_quote=swear_quote)
        assert leaks == [], f"Sanitization leaks: {leaks}"

        # Verify structural assertions
        assert '<span class="prompt-example">' not in sanitized
        assert '<span class="uncensored">' not in sanitized
        assert '<span class="censored">' not in sanitized
        assert '<div class="swear-pill">' not in sanitized

        # Verify functional HTML survived
        assert 'IntersectionObserver' in sanitized
        assert 'scroll-snap' in sanitized

        # Verify project labels are generic
        if project_names:
            assert 'bar-label">Project 1' in sanitized

        # Verify machine labels are generic (if any machines exist)
        if machine_names:
            assert re.search(r'<span>\d+ Machine 1</span>', sanitized)

        # Verify idempotent
        second, _ = sanitize_html_for_publish(sanitized)
        assert sanitized == second
