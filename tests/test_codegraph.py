from pathlib import Path

from issue_agent.codegraph import CodegraphConfig, guidance_block, index_ready


def test_index_ready_detects_codegraph_dir(tmp_path: Path):
    assert index_ready(tmp_path) is False
    (tmp_path / ".codegraph").mkdir()
    assert index_ready(tmp_path) is True


def test_index_ready_ignores_plain_file(tmp_path: Path):
    (tmp_path / ".codegraph").write_text("not a directory")
    assert index_ready(tmp_path) is False


def test_index_ready_swallows_os_errors(monkeypatch):
    def boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "is_dir", boom)
    assert index_ready(Path("/some/repo")) is False


def test_guidance_block_names_repo_and_mcp_tools(tmp_path: Path):
    (tmp_path / ".codegraph").mkdir()
    block = guidance_block(tmp_path, CodegraphConfig())
    assert str(tmp_path) in block
    assert "codegraph_context" in block
    assert f'codegraph query "<symbol>" --path {tmp_path} --json' in block
    assert "Never run codegraph init" in block


def test_guidance_block_empty_without_index(tmp_path: Path):
    assert guidance_block(tmp_path, CodegraphConfig()) == ""


def test_guidance_block_empty_when_disabled(tmp_path: Path):
    (tmp_path / ".codegraph").mkdir()
    assert guidance_block(tmp_path, CodegraphConfig(enabled=False)) == ""
