import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from patterns.config import CLAUDE_PROJECTS_DIR, find_sessions, output_path, OUTPUT_DIR, resolve_project_name


class TestCLAUDEProjectsDir:
    def test_claude_projects_dir_path(self):
        expected = Path.home() / ".claude" / "projects"
        assert CLAUDE_PROJECTS_DIR == expected

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "/tmp/custom")
        # Re-import to pick up env var
        import importlib
        import patterns.config as cfg
        importlib.reload(cfg)
        assert cfg.CLAUDE_PROJECTS_DIR == Path("/tmp/custom")
        # Restore
        monkeypatch.delenv("CLAUDE_PROJECTS_DIR")
        importlib.reload(cfg)


class TestOutputPath:
    def test_output_path(self):
        result = output_path("foo")
        assert result == OUTPUT_DIR / "foo.md"
        assert result.name == "foo.md"

    def test_output_path_custom_ext(self):
        result = output_path("foo", ".txt")
        assert result == OUTPUT_DIR / "foo.txt"
        assert result.name == "foo.txt"


class TestResolveProjectName:
    def test_mac_real_path(self):
        assert resolve_project_name("/Users/alice/openpaper-upstream/") == "OpenPaper"

    def test_ax41_real_path(self):
        assert resolve_project_name("/root/openchat-v4") == "OpenChat V4"

    def test_encoded_mac_dir(self):
        assert resolve_project_name("-Users-alice-openpaper-upstream") == "OpenPaper"

    def test_encoded_ax41_dir(self):
        assert resolve_project_name("-root-openchat-v4") == "OpenChat V4"

    def test_worktree_path(self):
        assert resolve_project_name("/root/openchat-v4-wt-agent-viz") == "OpenChat V4"

    def test_encoded_worktree(self):
        assert resolve_project_name("-root-openchat-v4-wt-something") == "OpenChat V4"

    def test_unknown_project(self):
        result = resolve_project_name("/root/some-unknown-project")
        assert result == "some-unknown-project"

    def test_none(self):
        assert resolve_project_name(None) == "Unknown"

    def test_empty_string(self):
        assert resolve_project_name("") == "Unknown"

    def test_rocketlist(self):
        assert resolve_project_name("-Users-alice-rocketlist-minimal") == "Rocketlist"

    def test_downloads_subdir(self):
        assert resolve_project_name("-Users-alice-Downloads-openpaper-upstream") == "OpenPaper"

    def test_tmp_subdir(self):
        assert resolve_project_name("-root-tmp-signalaudit-repo") == "SignalAudit"


class TestMultiDirectory:
    def test_colon_separated(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "/tmp/dir1:/tmp/dir2:/tmp/dir3")
        import importlib
        import patterns.config as cfg
        importlib.reload(cfg)
        assert len(cfg.CLAUDE_PROJECTS_DIRS) == 3
        assert cfg.CLAUDE_PROJECTS_DIRS[0] == Path("/tmp/dir1")
        assert cfg.CLAUDE_PROJECTS_DIRS[2] == Path("/tmp/dir3")
        # Backward compat: single var is first entry
        assert cfg.CLAUDE_PROJECTS_DIR == Path("/tmp/dir1")
        monkeypatch.delenv("CLAUDE_PROJECTS_DIR")
        importlib.reload(cfg)

    def test_single_path_backward_compat(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "/tmp/single")
        import importlib
        import patterns.config as cfg
        importlib.reload(cfg)
        assert len(cfg.CLAUDE_PROJECTS_DIRS) == 1
        assert cfg.CLAUDE_PROJECTS_DIR == Path("/tmp/single")
        monkeypatch.delenv("CLAUDE_PROJECTS_DIR")
        importlib.reload(cfg)

    def test_empty_segments_skipped(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "/tmp/a::/tmp/b: ")
        import importlib
        import patterns.config as cfg
        importlib.reload(cfg)
        assert len(cfg.CLAUDE_PROJECTS_DIRS) == 2
        monkeypatch.delenv("CLAUDE_PROJECTS_DIR")
        importlib.reload(cfg)


class TestFindSessions:
    def test_find_sessions_returns_list(self):
        result = find_sessions()
        assert isinstance(result, list)
