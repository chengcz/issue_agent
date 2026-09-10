import os

import pytest

from issue_agent.cli import _wrap_display, format_status


def screen(text):
    """Expand the fixture's wide glyphs to two cells for independent assertions."""
    return text.translate(str.maketrans({
        "中": "CC", "文": "WW", "Ａ": "AA", "代": "DD", "理": "LL", "\u0301": "",
    }))


@pytest.mark.parametrize("title", [
    "long_english_task_" * 8,
    "中文" * 40,
    "中文EnglishＡe\u0301" * 10,
])
def test_status_wraps_without_losing_text_and_aligns_every_column(title):
    output = format_status([
        {"issue_number": 1, "status": "coding", "current_task": title, "agent": "代理",
         "updated_at": "2026-09-10T12:34:56"},
        {"issue_number": 2, "status": "done", "title": "中文", "agent": "codex",
         "updated_at": "2026-09-10T12:34:56"},
    ], terminal_width=90)
    lines = [screen(line) for line in output.splitlines()]
    header = lines[0]
    task_start = header.index("CURRENT TASK")
    agent_start = header.index("AGENT")
    task_end = agent_start - 2
    assert len(lines) > 4
    assert all(len(line) == 90 for line in lines)
    assert lines[2][agent_start:].startswith("DDLL ")
    assert lines[-1][agent_start:].startswith("codex")
    assert lines[2][header.index("UPDATED"):] == "2026-09-10 12:34:56"
    for line in lines[3:-1]:
        assert not line[:task_start].strip()
        assert not line[agent_start:].strip()
    reconstructed = "".join(line[task_start:task_end].rstrip() for line in lines[2:-1])
    assert reconstructed == screen(title)


def test_status_normalizes_embedded_whitespace_and_uses_terminal_size(monkeypatch):
    monkeypatch.setattr("issue_agent.cli.shutil.get_terminal_size", lambda **kw: os.terminal_size((90, 24)))
    row = {"issue_number": 1, "status": "coding", "title": "中文\nEnglish\ttask " * 10}
    assert format_status([row]) == format_status([row], terminal_width=90)
    assert "\t" not in format_status([row])
    assert all(len(screen(line)) <= 90 for line in format_status([row]).splitlines())


def test_status_caps_task_width_even_on_wide_terminal():
    lines = format_status([
        {"issue_number": 1, "status": "coding", "title": "x" * 121},
    ], terminal_width=300).splitlines()
    assert len(lines) == 5
    assert lines[0].index("AGENT") - lines[0].index("CURRENT TASK") == 62


def test_status_narrow_terminal_preserves_headers_and_empty_output():
    lines = format_status([
        {"issue_number": 1, "status": "coding", "title": "中文" * 20},
    ], terminal_width=20).splitlines()
    assert any(line.startswith("CURRENT TASK:") for line in lines)
    assert all(len(screen(line)) <= 20 for line in lines)
    assert "".join(line[14:] for line in lines).count("中文") == 20
    assert format_status([]) == "No matching tasks."


@pytest.mark.parametrize("width", [2, 15, 20, 65, 80, 100])
def test_status_adapts_all_columns_to_terminal(width):
    row = {
        "issue_number": 12345, "status": "human_review", "title": "中文" * 40,
        "agent": "代理" * 25, "updated_at": "2026-09-10T12:34:56",
    }
    output = format_status([row], terminal_width=width)
    assert all(len(screen(line)) <= width for line in output.splitlines())
    assert output.count("中") == 40
    assert output.count("代") == 25


def test_status_compact_layout_separates_issues():
    output = format_status([
        {"issue_number": 1, "status": "done", "title": "中文"},
        {"issue_number": 2, "status": "coding", "title": "English"},
    ], terminal_width=40)
    sections = output.split("\n\n")
    assert len(sections) == 2
    assert "#1" in sections[0]
    assert "#2" in sections[1]


def test_wrap_prefers_english_word_boundaries_without_losing_spaces():
    text = "Implement status wrapping"
    assert _wrap_display(text, 12) == ["Implement ", "status ", "wrapping"]
    assert "".join(_wrap_display(text, 12)) == text
