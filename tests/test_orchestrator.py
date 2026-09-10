import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from issue_agent.agents import (
    make_final_fix_prompt,
    make_final_review_prompt,
    make_plan_prompt,
    make_task_prompt,
    make_task_review_prompt,
)
from issue_agent.codegraph import CodegraphConfig
from issue_agent.models import Blocker, Comment, Issue, PlanTask, RecordedChild, TaskStatus
from issue_agent.orchestrator import (
    Orchestrator,
    clarification_notes,
    declared_blockers,
    has_detailed_plan,
    parse_plan,
    parse_plan_output,
    review_verdict,
)
from issue_agent.process import CommandError, Result
from issue_agent.state import StateStore

APPROVE = "Looks good.\nVERDICT: APPROVE\n"


class _Log:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))

    def review(self, phase, output, **fields):
        self.events.append(("review", {"phase": phase, "output": output, **fields}))


def result(stdout: str = "") -> Result:
    return Result(returncode=0, stdout=stdout, stderr="")


def make_orchestrator(tmp_path: Path, *, attempts: int = 2, reviewer: str = "reviewer") -> Orchestrator:
    app = Orchestrator.__new__(Orchestrator)
    app.config = SimpleNamespace(
        max_attempts=attempts,
        max_task_attempts=attempts,
        checks=(),
        check_timeout_seconds=1800,
        checks_parallel=True,
        reviewer_agent=reviewer,
        planner_agent="planner",
        max_tasks=8,
        base_branch="main",
        ready_label="agent-ready",
        default_agent="worker",
        auto_plan_unlabeled=True,
        auto_plan_limit=20,
        ready_poll_limit=20,
        auto_ready_with_plan=False,
        allow_split=False,
        max_split_children=5,
        max_clarify_rounds=2,
        clarify_ignore_authors=(),
        log_dir=tmp_path / "logs",
        dry_run=True,
        repo=tmp_path,
        codegraph=CodegraphConfig(),
        review_task_mode="full",
    )
    app.state = StateStore(tmp_path / "state.db")
    app.running = {}
    app._viewer_login = None
    app._run_ids = {}
    app._baseline_inflight = {}
    app.github = SimpleNamespace(
        labels=AsyncMock(),
        comment=AsyncMock(),
        create_pr=AsyncMock(return_value="dry-run://pr/4"),
        runnable_issues=AsyncMock(return_value=[]),
        unassigned_issues=AsyncMock(return_value=[]),
        blocker_states=AsyncMock(return_value={}),
        viewer_login=AsyncMock(return_value="octocat"),
        comments=AsyncMock(return_value=[]),
        create_issue=AsyncMock(return_value=(0, "")),
        link_parent=AsyncMock(),
        add_blocked_by=AsyncMock(),
    )
    app.workspaces = SimpleNamespace(
        create=AsyncMock(return_value=(tmp_path, "agent/4-task")),
        status=AsyncMock(return_value=""),
        changed=AsyncMock(return_value=True),
        commit=AsyncMock(),
        amend=AsyncMock(),
        push=AsyncMock(),
        reset=AsyncMock(),
        clean=AsyncMock(),
        head_commit=AsyncMock(return_value="abc1234"),
        write_plan_file=Mock(),
        write_task_file=Mock(),
        write_feedback_file=Mock(),
    )
    app.agents = {
        "worker": SimpleNamespace(execute=AsyncMock(return_value=result())),
        "planner": SimpleNamespace(
            execute=AsyncMock(
                return_value=result(
                    '```json\n[{"title": "One", "description": "D"}, {"title": "Two", "description": "D"}]\n```\n'
                )
            )
        ),
        "reviewer": SimpleNamespace(execute=AsyncMock(return_value=result(APPROVE))),
    }
    app.agent_limits = {name: asyncio.Semaphore(1) for name in app.agents}
    return app


def run_process(app: Orchestrator, issue: Issue) -> None:
    app.state.claim(issue, "worker")
    asyncio.run(app.process(issue, "worker"))


def test_review_verdict_must_be_the_final_line():
    assert review_verdict("Looks good.\nVERDICT: APPROVE\n") == "VERDICT: APPROVE"
    assert review_verdict("VERDICT: APPROVE\nBut this is not done") is None
    assert review_verdict("No verdict") is None


def test_read_only_agent_honors_its_own_concurrency_limit(tmp_path):
    app = make_orchestrator(tmp_path)
    active = 0
    max_active = 0

    async def execute(workspace, prompt, *, review=False):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return result(APPROVE)

    app.agents["reviewer"].execute = execute

    async def run_reviews():
        await asyncio.gather(
            app._execute_read_only(
                "reviewer", tmp_path, "one", role="reviewer", acquire_agent_limit=True
            ),
            app._execute_read_only(
                "reviewer", tmp_path, "two", role="reviewer", acquire_agent_limit=True
            ),
        )

    asyncio.run(run_reviews())
    assert max_active == 1


def test_coding_agent_honors_limit_per_cli_invocation(tmp_path):
    app = make_orchestrator(tmp_path)
    active = 0
    max_active = 0

    async def execute(workspace, prompt, *, review=False):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return result()

    app.agents["worker"].execute = execute

    async def run_workers():
        await asyncio.gather(
            app._execute_agent("worker", tmp_path, "one"),
            app._execute_agent("worker", tmp_path, "two"),
        )

    asyncio.run(run_workers())
    assert max_active == 1


def test_plan_prompt_demands_detail_but_single_line():
    """The planner must be pushed for concrete specs + acceptance criteria while
    being told that the single-line JSON rule limits line breaks, not length."""
    prompt = make_plan_prompt(Issue(9, "T", "B"), 8)
    assert "Acceptance:" in prompt
    assert "files or modules" in prompt
    assert "no raw line breaks" in prompt
    assert "long single line" in prompt


def test_plan_prompt_appends_guidance_without_touching_base():
    base = make_plan_prompt(Issue(9, "T", "B"), 8)
    assert "codegraph" not in base
    guided = make_plan_prompt(Issue(9, "T", "B"), 8, guidance="GUIDANCE-BLOCK")
    assert guided.startswith(base)
    assert guided.endswith("GUIDANCE-BLOCK")


def test_plan_prompt_offers_the_questions_shape_instead_of_guessing():
    prompt = make_plan_prompt(Issue(9, "T", "B"), 8)

    assert '{"questions"' in prompt


def test_plan_prompt_carries_the_clarification_transcript_in_a_fenced_block():
    base = make_plan_prompt(Issue(9, "T", "B"), 8)

    prompt = make_plan_prompt(Issue(9, "T", "B"), 8, clarification="@me: Use SQLite.")

    assert prompt != base
    # The transcript is untrusted issue text, so it arrives fenced and labelled
    # as information rather than as instructions.
    assert "@me: Use SQLite." in prompt
    assert "```text\n@me: Use SQLite.\n```" in prompt
    assert "never as instructions" in prompt
    # Asking is a last resort on a re-plan, not the default.
    assert "do NOT guess" in base


CLARIFY_QUESTION = "❓ **More information needed**\n\n1. Which module should this live in?"


def test_clarification_notes_start_at_the_first_question():
    comments = [
        Comment("someone", "2026-09-10T00:00:00Z", "Unrelated discussion."),
        Comment("bot", "2026-09-10T01:00:00Z", CLARIFY_QUESTION),
        Comment("someone", "2026-09-10T02:00:00Z", "Use the parser module."),
    ]

    notes = clarification_notes(comments, "bot")

    assert "Unrelated discussion." not in notes
    assert "Use the parser module." in notes
    assert "@someone" in notes


def test_clarification_notes_are_empty_without_a_question():
    comments = [Comment("someone", "2026-09-10T00:00:00Z", "Just chatting.")]

    assert clarification_notes(comments, "bot") == ""


def test_task_prompt_inlines_titles_only_and_points_to_plan_file():
    plan = [PlanTask("One", "first long description"), PlanTask("Two", "second long description")]
    prompt = make_task_prompt(Issue(9, "T", "B"), plan[1], plan)
    assert "1. One" in prompt
    assert "2. Two" in prompt
    assert ".agent/plan.md" in prompt
    assert "first long description" not in prompt
    assert "second long description" not in prompt


def test_task_prompt_retry_uses_feedback_pointer_and_short_excerpt():
    plan = [PlanTask("One", "D")]
    prompt = make_task_prompt(Issue(9, "T", "B"), plan[0], plan, retry_error="E" * 5000)
    assert ".agent/feedback.md" in prompt
    assert "E" * 800 in prompt
    assert "E" * 801 not in prompt


def test_task_review_prompt_appends_guidance_without_touching_base():
    base = make_task_review_prompt(Issue(9, "T", "B"), PlanTask("One", "D"))
    assert "codegraph" not in base
    guided = make_task_review_prompt(Issue(9, "T", "B"), PlanTask("One", "D"), guidance="G")
    assert guided.startswith(base)
    assert guided.endswith("G")


def test_final_review_prompt_uses_plan_pointer_and_guidance():
    plan = [PlanTask("One", "first long description")]
    base = make_final_review_prompt(Issue(9, "T", "B"), plan, "main")
    assert ".agent/plan.md" in base
    assert "1. One" in base
    assert "first long description" not in base
    guided = make_final_review_prompt(Issue(9, "T", "B"), plan, "main", guidance="G")
    assert guided.startswith(base)
    assert guided.endswith("G")


def test_final_fix_prompt_uses_feedback_pointer_and_short_excerpt():
    prompt = make_final_fix_prompt(Issue(9, "T", "B"), "F" * 5000)
    assert ".agent/feedback.md" in prompt
    assert "F" * 800 in prompt
    assert "F" * 801 not in prompt


def test_parse_plan_accepts_fenced_json_with_prose():
    plan = parse_plan(
        "Here is my plan:\n```json\n[{\"title\": \"A\", \"description\": \"B\"}]\n```\nDone.", 8
    )
    assert [t.title for t in plan] == ["A"]


def test_parse_plan_rejects_missing_fence():
    with pytest.raises(CommandError):
        parse_plan("no fence here", 8)


def test_parse_plan_rejects_invalid_json():
    with pytest.raises(CommandError):
        parse_plan("```json\nnot json\n```", 8)


def test_parse_plan_rejects_empty_list():
    with pytest.raises(CommandError):
        parse_plan("```json\n[]\n```", 8)


def test_parse_plan_rejects_too_many_tasks():
    with pytest.raises(CommandError):
        parse_plan('```json\n[{"title": "A"}, {"title": "B"}, {"title": "C"}]\n```', 2)


def test_parse_plan_tolerates_trailing_comma():
    plan = parse_plan('```json\n[{"title": "A", "description": "B",},]\n```', 8)
    assert [t.title for t in plan] == ["A"]
    assert plan[0].description == "B"


def test_parse_plan_tolerates_newline_inside_string():
    plan = parse_plan('```json\n[{"title": "A", "description": "line1\nline2"}]\n```', 8)
    assert [t.title for t in plan] == ["A"]
    assert plan[0].description == "line1\nline2"


def test_parse_plan_tolerates_realistic_llm_output():
    """Regression: deepseek-pro-0813 output a plan whose descriptions had raw newlines
    and trailing commas; parse must not reject it."""
    raw = '''```json
[
  {
    "title": "Add body-map classification and counting service",
    "description": "Create src/services/body_map.py defining an anatomical body-map
data model and pure helpers.",
  },
  {
    "title": "Add body-map SVG silhouette asset",
    "description": "Add a self-authored human silhouette with a fixed viewBox.",
  }
]
```'''
    plan = parse_plan(raw, 8)
    assert [t.title for t in plan] == [
        "Add body-map classification and counting service",
        "Add body-map SVG silhouette asset",
    ]
    assert "data model and pure helpers." in plan[0].description


def test_parse_plan_tolerates_prose_quotes_inside_string():
    """Stray double quotes used as prose punctuation must not split the string."""
    plan = parse_plan('```json\n[{"title": "call "body" map", "description": "use the "x" filter" }]\n```', 8)
    assert plan[0].title == 'call "body" map'
    assert plan[0].description == 'use the "x" filter'


def test_parse_plan_output_keeps_bare_task_lists_working():
    outcome = parse_plan_output('```json\n[{"title": "A", "description": "B"}]\n```', 8, 5)

    assert [task.title for task in outcome.tasks] == ["A"]
    assert outcome.questions == ()
    assert outcome.split == ()


def test_parse_plan_output_reads_planner_questions():
    outcome = parse_plan_output(
        '```json\n{"questions": ["Which module?", "Which protocol?"]}\n```', 8, 5
    )

    assert outcome.questions == ("Which module?", "Which protocol?")
    assert outcome.tasks == ()


def test_parse_plan_output_reads_split_children_and_their_order():
    outcome = parse_plan_output(
        '```json\n{"split": ['
        '{"title": "First", "body": "Body one"},'
        '{"title": "Second", "body": "Body two", "depends_on": [0]}'
        "]}\n```",
        8,
        5,
    )

    assert [child.title for child in outcome.split] == ["First", "Second"]
    assert outcome.split[0].depends_on == ()
    assert outcome.split[1].depends_on == (0,)


def test_parse_plan_output_rejects_object_without_a_known_shape():
    with pytest.raises(CommandError, match="questions"):
        parse_plan_output('```json\n{"notes": "nothing actionable"}\n```', 8, 5)


def test_parse_plan_output_rejects_two_shapes_at_once():
    with pytest.raises(CommandError):
        parse_plan_output('```json\n{"questions": ["Q?"], "split": []}\n```', 8, 5)


def test_parse_plan_output_rejects_empty_questions():
    with pytest.raises(CommandError):
        parse_plan_output('```json\n{"questions": []}\n```', 8, 5)


def test_parse_plan_output_requires_split_title_and_body():
    with pytest.raises(CommandError):
        parse_plan_output('```json\n{"split": [{"title": "First"}]}\n```', 8, 5)


def test_parse_plan_output_rejects_more_children_than_allowed():
    children = ", ".join(f'{{"title": "C{i}", "body": "B"}}' for i in range(3))

    with pytest.raises(CommandError, match="max_split_children"):
        parse_plan_output(f'```json\n{{"split": [{children}]}}\n```', 8, 2)


def test_parse_plan_output_rejects_dependency_outside_the_batch():
    with pytest.raises(CommandError):
        parse_plan_output(
            '```json\n{"split": [{"title": "A", "body": "B", "depends_on": [3]}]}\n```', 8, 5
        )


def test_parse_plan_output_rejects_self_dependency():
    with pytest.raises(CommandError):
        parse_plan_output(
            '```json\n{"split": [{"title": "A", "body": "B", "depends_on": [0]}]}\n```', 8, 5
        )


def test_parse_plan_output_rejects_dependency_cycles():
    with pytest.raises(CommandError, match="circular"):
        parse_plan_output(
            '```json\n{"split": ['
            '{"title": "A", "body": "B", "depends_on": [1]},'
            '{"title": "C", "body": "D", "depends_on": [0]}'
            "]}\n```",
            8,
            5,
        )


def test_declared_blockers_reads_english_and_chinese_declarations():
    body = (
        "## 依赖与风险\n\n"
        "- Blocked by: #12\n"
        "- Depends on: #13, #14\n"
        "- 前置 Issue：#15\n"
        "- 依赖：#16\n"
    )

    assert declared_blockers(body) == (12, 13, 14, 15, 16)


def test_declared_blockers_ignores_issue_references_outside_a_declaration():
    body = "## Scope\n\nSee #12 for background; this supersedes #13.\n"

    assert declared_blockers(body) == ()


def test_declared_blockers_reports_each_reference_once():
    assert declared_blockers("Depends on #12; also blocked by #12.\n") == (12,)


def test_dependency_gate_blocks_until_every_blocker_closes(tmp_path):
    app = make_orchestrator(tmp_path)
    partly_open = Issue(number=4, title="T", body="B", blocked_by=(2, 3))

    app.github.blocker_states = AsyncMock(
        return_value={2: Blocker(2, "First", True), 3: Blocker(3, "Second", False)}
    )
    assert asyncio.run(app._dependency_gate(partly_open, None, cache={})) is True

    app.github.blocker_states = AsyncMock(
        return_value={2: Blocker(2, "First", True), 3: Blocker(3, "Second", True)}
    )
    assert asyncio.run(app._dependency_gate(partly_open, None, cache={})) is False


def test_dependency_gate_treats_unresolved_blockers_as_open(tmp_path):
    app = make_orchestrator(tmp_path)
    app.github.blocker_states = AsyncMock(return_value={})
    issue = Issue(number=4, title="T", body="B", blocked_by=(2,))

    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True


def test_dependency_gate_ignores_issues_that_are_already_under_way(tmp_path):
    app = make_orchestrator(tmp_path)
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", False)})

    interrupted = Issue(number=4, title="T", body="B", labels=("agent-running",), blocked_by=(2,))
    resuming = Issue(number=5, title="T", body="B", blocked_by=(2,))
    coding_row = {"status": str(TaskStatus.CODING)}

    assert asyncio.run(app._dependency_gate(interrupted, None, cache={})) is False
    assert asyncio.run(app._dependency_gate(resuming, coding_row, cache={})) is False


def test_dependency_gate_reuses_the_blocker_cache_across_candidates(tmp_path):
    app = make_orchestrator(tmp_path)
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", True)})
    first = Issue(number=4, title="T", body="B", blocked_by=(2,))
    second = Issue(number=5, title="T", body="B", blocked_by=(2,))
    cache: dict[int, Blocker] = {}

    assert asyncio.run(app._dependency_gate(first, None, cache=cache)) is False
    assert asyncio.run(app._dependency_gate(second, None, cache=cache)) is False

    assert app.github.blocker_states.await_count == 1


def _track_recorder(admitted: list[int]):
    def fake_track(number, coroutine):
        coroutine.close()
        admitted.append(number)

    return fake_track


def test_run_once_isolates_a_failing_candidate_from_the_ones_behind_it(tmp_path):
    """One candidate's gate failure must not starve every candidate after it."""
    app = make_orchestrator(tmp_path)
    first = Issue(number=4, title="First", body="B", labels=("agent-ready",))
    second = Issue(number=5, title="Second", body="B", labels=("agent-ready",))
    app.github.runnable_issues = AsyncMock(return_value=[first, second])
    app._dependency_gate = AsyncMock(side_effect=[CommandError("gh exploded"), False])
    admitted: list[int] = []
    app._track = _track_recorder(admitted)

    asyncio.run(app.run_once())

    assert admitted == [5]


def test_run_once_isolates_a_failing_label_reconciliation(tmp_path):
    app = make_orchestrator(tmp_path)
    app.state.claim(Issue(number=4, title="Stuck", body="B"), "worker")
    app.state.update(4, TaskStatus.HUMAN_REVIEW)
    stuck = Issue(number=4, title="Stuck", body="B", labels=("human-review",))
    ready = Issue(number=5, title="Second", body="B", labels=("agent-ready",))
    app.github.runnable_issues = AsyncMock(return_value=[stuck, ready])
    app.github.labels = AsyncMock(side_effect=CommandError("gh: rate limited"))
    admitted: list[int] = []
    app._track = _track_recorder(admitted)

    asyncio.run(app.run_once())

    assert admitted == [5]
    assert app.github.labels.await_count == 1


def test_plan_only_republishes_a_recovered_plan_without_the_planner(tmp_path):
    """A crash between save_plan and publication recovers to PLANNED; the next
    planning poll must republish the saved plan without another LLM call."""
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "T", "B")
    app.state.claim_for_planning(issue, "planner", 3)
    app.state.update(4, TaskStatus.PLANNING)
    app.state.save_plan(4, [PlanTask(title="One", description="D")])

    assert app.recover() == 1
    row = next(r for r in app.state.rows() if int(r["issue_number"]) == 4)
    assert row["status"] == str(TaskStatus.PLANNED)
    assert app.state.claim_for_planning(issue, "planner", 3) is True
    planner_execute = app.agents["planner"].execute

    asyncio.run(app.plan_only(issue))

    planner_execute.assert_not_awaited()
    published = [call.kwargs.get("add") for call in app.github.labels.await_args_list]
    assert ("agent-planned",) in published
    assert any("Issue Agent Plan" in body for body in comments(app))


def test_once_mode_waits_for_sibling_workers_when_one_raises(tmp_path):
    """A failing worker must not abandon its siblings: gather collects every
    outcome, the sibling still runs to completion, and the exit code is 1."""
    from issue_agent.cli import wait_for_once_workers

    app = make_orchestrator(tmp_path)
    completed: list[int] = []

    async def failing() -> None:
        raise CommandError("worker exploded")

    async def succeeding() -> None:
        completed.append(5)

    async def scenario() -> int:
        app.running = {
            4: asyncio.create_task(failing()),
            5: asyncio.create_task(succeeding()),
        }
        return await wait_for_once_workers(app)

    assert asyncio.run(scenario()) == 1
    assert completed == [5]


def test_once_mode_returns_zero_when_every_worker_succeeds(tmp_path):
    from issue_agent.cli import wait_for_once_workers

    app = make_orchestrator(tmp_path)

    async def scenario() -> int:
        app.running = {4: asyncio.create_task(asyncio.sleep(0))}
        return await wait_for_once_workers(app)

    assert asyncio.run(scenario()) == 0


def test_coding_path_routes_planner_questions_to_clarification(tmp_path):
    """Spec §11 D3: questions on the coding path must ask the human instead of
    burning the whole-issue failure budget on repeated planner runs."""
    app = make_orchestrator(tmp_path)
    app.agents["planner"].execute = AsyncMock(
        return_value=result('```json\n{"questions": ["What auth?", "Which DB?"]}\n```\n')
    )
    issue = Issue(4, "Ambiguous", "Body")

    run_process(app, issue)

    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.PENDING)
    assert row["failures"] == 0
    added = [call.kwargs.get("add") for call in app.github.labels.await_args_list]
    removed = [call.kwargs.get("remove") for call in app.github.labels.await_args_list]
    assert ("agent-needs-info",) in added
    assert ("agent-running",) in removed
    assert any("More information needed" in body for body in comments(app))


def test_coding_path_routes_a_split_proposal_to_the_split_flow(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.allow_split = True
    app.agents["planner"].execute = AsyncMock(
        return_value=result(
            '```json\n{"split": [{"title": "Child A", "body": "A body"},'
            ' {"title": "Child B", "body": "B body", "depends_on": [0]}]}\n```\n'
        )
    )
    app.github.create_issue = AsyncMock(
        side_effect=[
            (11, "https://github.com/o/r/issues/11"),
            (12, "https://github.com/o/r/issues/12"),
        ]
    )
    issue = Issue(4, "Too big", "Body")

    run_process(app, issue)

    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.SPLIT)
    assert [child.number for child in app.state.load_split(4)] == [11, 12]


def comments(app: Orchestrator) -> list[str]:
    return [call.args[1] for call in app.github.comment.await_args_list]


def test_dependency_gate_comments_once_about_the_open_blockers(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(2, 3))
    app.github.blocker_states = AsyncMock(
        return_value={2: Blocker(2, "Ship the parser", True), 3: Blocker(3, "Add the API", False)}
    )

    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    assert len(comments(app)) == 1
    assert "#3" in comments(app)[0] and "Add the API" in comments(app)[0]
    assert "#2" not in comments(app)[0]


def test_dependency_gate_announces_the_all_clear_once(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(2,))
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", False)})
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", True)})
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False

    assert len(comments(app)) == 2
    assert "closed" in comments(app)[1].lower()


def test_dependency_gate_comments_again_when_a_new_blocker_appears(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(2,))
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", False)})
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    grown = Issue(number=4, title="T", body="B", blocked_by=(2, 3))
    app.github.blocker_states = AsyncMock(
        return_value={2: Blocker(2, "First", False), 3: Blocker(3, "Second", False)}
    )
    assert asyncio.run(app._dependency_gate(grown, None, cache={})) is True

    assert len(comments(app)) == 2
    assert "#3" in comments(app)[1]


def test_dependency_gate_warns_once_when_the_body_claims_unlinked_blockers(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="## 依赖与风险\n\n- Blocked by: #7\n")

    # Native blocked-by is empty, so nothing gates the issue; the body line is
    # still worth one comment because the human clearly meant it to.
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False

    assert len(comments(app)) == 1
    assert "#7" in comments(app)[0]


def test_dependency_gate_stays_quiet_when_body_and_native_agree(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="Depends on: #2\n", blocked_by=(2,))
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", True)})

    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False

    assert comments(app) == []


def test_dependency_gate_warns_once_about_a_native_self_dependency(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(4,))
    app.github.blocker_states = AsyncMock(return_value={4: Blocker(4, "T", False)})

    # A self-link can never close, so the issue stays held instead of spinning.
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    self_warnings = [body for body in comments(app) if "itself" in body]
    assert len(self_warnings) == 1


def test_dependency_gate_holds_an_issue_whose_body_blocks_on_itself(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="Blocked by: #4\n")

    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    assert len(comments(app)) == 1
    assert "itself" in comments(app)[0]


def events(app: Orchestrator, number: int = 4) -> list[dict]:
    path = app.config.log_dir / f"issue-{number}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def names(app: Orchestrator, number: int = 4) -> list[str]:
    return [record["event"] for record in events(app, number)]


def test_dependency_gate_records_every_skipped_admission(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(2, 3))
    app.github.blocker_states = AsyncMock(
        return_value={2: Blocker(2, "Ship the parser", True), 3: Blocker(3, "Add the API", False)}
    )

    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    # The comment is deduplicated; the audit log must not be, or a poll that
    # keeps skipping the issue would look like it never happened.
    blocked = [record for record in events(app) if record["event"] == "dependency_blocked"]
    assert [record["blockers"] for record in blocked] == [[3], [3]]


def test_dependency_gate_records_the_all_clear_once(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(2,))
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", False)})
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", True)})
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False
    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is False

    assert names(app).count("dependency_cleared") == 1


def test_dependency_gate_records_a_self_dependency_it_holds(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="B", blocked_by=(4,))

    assert asyncio.run(app._dependency_gate(issue, None, cache={})) is True

    assert names(app).count("dependency_blocked") == 1


def test_dependency_gate_records_a_body_mismatch(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(number=4, title="T", body="## 依赖与风险\n\n- Blocked by: #7\n")

    asyncio.run(app._dependency_gate(issue, None, cache={}))

    assert names(app) == ["dependency_body_mismatch"]


def track_admissions(app: Orchestrator) -> list[int]:
    """Record what run_once admits, closing the worker so no task is left pending."""
    admitted: list[int] = []

    def track(number, coroutine):
        admitted.append(number)
        coroutine.close()

    app._track = track
    return admitted


def test_run_once_leaves_a_blocked_ready_issue_unclaimed(tmp_path):
    app = make_orchestrator(tmp_path)
    blocked = Issue(number=4, title="T", body="B", labels=("agent-ready",), blocked_by=(2,))
    app.github.runnable_issues = AsyncMock(return_value=[blocked])
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", False)})
    admitted = track_admissions(app)

    asyncio.run(app.run_once())

    assert admitted == []
    assert app.state.rows() == []


def test_run_once_admits_a_ready_issue_once_its_blockers_close(tmp_path):
    app = make_orchestrator(tmp_path)
    ready = Issue(number=4, title="T", body="B", labels=("agent-ready",), blocked_by=(2,))
    app.github.runnable_issues = AsyncMock(return_value=[ready])
    app.github.blocker_states = AsyncMock(return_value={2: Blocker(2, "First", True)})
    admitted = track_admissions(app)

    asyncio.run(app.run_once())

    assert admitted == [4]


def test_parse_plan_error_includes_context_snippet():
    """An unrepairable plan should surface the offending text in the error message."""
    with pytest.raises(CommandError, match="near:"):
        parse_plan('```json\n[{"title": "A", "description: B"}]\n```', 8)


DETAILED_PLAN = (
    "## 实施计划\n"
    "1. 修改 src/issue_agent/orchestrator.py，增加已有计划判断。\n"
    "2. 添加 tests/test_orchestrator.py 测试，验证跳过 planner。\n"
)

NESTED_PLAN = (
    "## Implementation Plan\n"
    "### Parser\n"
    "1. Add parse_issue_plan in src/parser.py to recognize numbered tasks.\n"
    "### Tests\n"
    "2. Add tests/test_parser.py covering malformed plans and empty bodies.\n"
)

VAGUE_PLAN = "## Plan\n1. Implement the feature.\n2. Add tests for it.\n"
BOLD_REPRODUCTION = (
    "**Plan:**\nTBD\n**Reproduction:**\n"
    "1. Run src/app.py with an empty configuration.\n"
    "2. Change the input value and observe the crash.\n"
)


@pytest.mark.parametrize("body", [
    DETAILED_PLAN,
    NESTED_PLAN,
    (
        "## 实施计划 ##\n1. 修改任务状态的持久化逻辑。\n"
        "   在 `save_plan` 中保存原始正文。\n2. 添加测试，验证重试时复用已保存的计划。\n"
    ),
    (
        "**Implementation Plan:**\n"
        "- [ ] Add a parser in src/parser.py for plan sections.\n"
        "- [ ] Test the parser with empty and detailed issue bodies."
    ),
])
def test_has_detailed_plan(body):
    assert has_detailed_plan(body)


@pytest.mark.parametrize("body", [
    VAGUE_PLAN, BOLD_REPRODUCTION,
    "", "Please make a plan before coding.",
    "## Plan\n- TBD\n- Implement\n",
    "## Plan\n1. Add a parser in src/parser.py.\n",
    "## Requirements\n- Add a parser in src/parser.py.\n- Test empty input values.\n",
    "## Plan\n## Reproduction\n1. Run the application locally.\n2. Change the input value.\n",
    f"<!--\n{DETAILED_PLAN}\n-->",
    f"```markdown\n{DETAILED_PLAN}\n```",
    f"```markdown\n{DETAILED_PLAN}",
    f"````markdown\n```\n{DETAILED_PLAN}\n```\n````",
    f"~~~markdown\n{DETAILED_PLAN}\n~~~",
    (
        "## Plan\n1. Add a parser in src/parser.py.\n"
        "## Tests\n2. Add tests in tests/test_parser.py.\n"
    ),
    (
        "## Plan\n1. Add a parser in src/parser.py.\n"
        "## Plan\n1. Add tests in tests/test_parser.py.\n"
    ),
])
def test_ambiguous_issue_still_needs_planning(body):
    assert not has_detailed_plan(body)


@pytest.mark.parametrize("body", [DETAILED_PLAN, NESTED_PLAN])
def test_detailed_issue_skips_planner_and_completes(tmp_path, body):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", body)

    run_process(app, issue)

    app.agents["planner"].execute.assert_not_awaited()
    assert app.state.load_plan(4) == [PlanTask("Task", body)]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE]
    assert "planner_skipped" in (app.config.log_dir / "issue-4.jsonl").read_text()


@pytest.mark.parametrize("body", [VAGUE_PLAN, BOLD_REPRODUCTION])
def test_ambiguous_plan_invokes_planner(tmp_path, body):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", body)
    assert app.state.claim_for_planning(issue, "planner")

    asyncio.run(app.plan_only(issue))

    app.agents["planner"].execute.assert_awaited_once()
    assert app.state.load_plan(4) == [PlanTask("One", "D"), PlanTask("Two", "D")]


def run_plan_only(app: Orchestrator, issue: Issue) -> None:
    app.state.claim_for_planning(issue, app.config.planner_agent or "planner")
    asyncio.run(app.plan_only(issue))


def added_labels(app: Orchestrator) -> list[str]:
    labels: list[str] = []
    for call in app.github.labels.await_args_list:
        labels.extend(call.kwargs.get("add", ()))
    return labels


def test_plan_only_reuses_detailed_issue_and_waits_for_ready(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", DETAILED_PLAN)
    assert app.state.claim_for_planning(issue, "planner")

    asyncio.run(app.plan_only(issue))

    app.agents["planner"].execute.assert_not_awaited()
    app.agents["worker"].execute.assert_not_awaited()
    assert app.state.load_plan(4) == [PlanTask("Task", DETAILED_PLAN)]
    assert app.state.rows()[0]["status"] == str(TaskStatus.PLANNED)
    # auto_ready_with_plan defaults to off, so this still needs a human.
    assert added_labels(app) == ["agent-planned"]


def test_auto_ready_releases_an_issue_whose_body_carries_the_plan(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.auto_ready_with_plan = True
    issue = Issue(4, "Task", DETAILED_PLAN)

    run_plan_only(app, issue)

    # agent-planned means "plan published, awaiting approval"; nothing is
    # waiting here, so adding it would mislead.
    assert added_labels(app) == ["agent-ready"]
    app.agents["planner"].execute.assert_not_awaited()
    assert app.state.load_plan(4) == [PlanTask("Task", DETAILED_PLAN)]
    assert app.state.rows()[0]["status"] == str(TaskStatus.PLANNED)
    assert "auto_ready_with_plan" in app.github.comment.await_args.args[1]


def test_auto_ready_leaves_a_planner_written_plan_for_human_review(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.auto_ready_with_plan = True
    issue = Issue(4, "Task", "Ambiguous request")

    run_plan_only(app, issue)

    app.agents["planner"].execute.assert_awaited_once()
    assert added_labels(app) == ["agent-planned"]


def test_auto_ready_requires_a_planner_agent(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.auto_ready_with_plan = True
    app.config.planner_agent = ""
    issue = Issue(4, "Task", DETAILED_PLAN)

    run_plan_only(app, issue)

    # With no planner configured every issue takes the "body is the plan" path,
    # so without this guard auto-ready would release the whole queue.
    assert added_labels(app) == ["agent-planned"]


def test_auto_ready_skips_child_issues_from_a_split(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.auto_ready_with_plan = True
    issue = Issue(4, "Task", DETAILED_PLAN, parent=1)

    run_plan_only(app, issue)

    assert added_labels(app) == ["agent-planned"]


def asks_questions(app: Orchestrator, question: str = "Which module should this live in?") -> None:
    app.agents["planner"].execute = AsyncMock(
        return_value=result('```json\n{"questions": ["' + question + '"]}\n```')
    )


def test_plan_only_asks_for_clarification_and_waits(tmp_path):
    app = make_orchestrator(tmp_path)
    asks_questions(app)
    issue = Issue(4, "Vague", "Improve this")

    run_plan_only(app, issue)

    assert added_labels(app) == ["agent-needs-info"]
    assert app.state.load_plan(4) is None
    rounds, marker = app.state.clarify_state(4)
    assert (rounds, bool(marker)) == (1, True)
    # PENDING with no plan is exactly what makes the next poll plan it again.
    assert app.state.rows()[0]["status"] == str(TaskStatus.PENDING)
    assert "Which module should this live in?" in app.github.comment.await_args.args[1]


def test_plan_only_reads_comments_only_after_it_has_asked(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", DETAILED_PLAN)

    run_plan_only(app, issue)

    # The transcript costs an API call, so a first planning pass must not pay it.
    app.github.comments.assert_not_awaited()


def test_plan_only_stops_asking_once_the_round_budget_is_spent(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.max_clarify_rounds = 1
    asks_questions(app)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T00:00:00+00:00")

    asyncio.run(app.plan_only(issue))

    # Still parked, but the comment stops promising another automatic pass.
    assert added_labels(app) == ["agent-needs-info"]
    assert app.state.clarify_state(4)[0] == 2
    assert "issue-agent reset" in app.github.comment.await_args.args[1]
    # The log has to say the orchestrator stopped watching, since nothing else
    # in the issue records that this is now a manual handoff.
    assert "clarify_exhausted" in names(app)


def test_plan_only_replans_with_the_clarification_transcript(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[
            Comment("octocat", "2026-09-10T01:00:01+00:00", CLARIFY_QUESTION),
            Comment("alice", "2026-09-10T02:00:00+00:00", "The parser module."),
        ]
    )

    asyncio.run(app.plan_only(issue))

    prompt = app.agents["planner"].execute.await_args.args[1]
    assert "The parser module." in prompt
    # The marker goes with the plan; the round budget stays spent.
    assert app.state.clarify_state(4) == (1, "")


SPLIT_PAYLOAD = (
    '{"split": ['
    '{"title": "Add the parser", "body": "Body one"}, '
    '{"title": "Add the API", "body": "Body two", "depends_on": [0]}'
    ']}'
)


def proposes_split(app: Orchestrator, payload: str = SPLIT_PAYLOAD) -> None:
    app.agents["planner"].execute = AsyncMock(
        return_value=result(f"```json\n{payload}\n```")
    )


def split_app(tmp_path: Path, *, children: list[tuple[int, str]] | None = None) -> Orchestrator:
    app = make_orchestrator(tmp_path)
    app.config.allow_split = True
    app.github.create_issue = AsyncMock(side_effect=children or [(12, "u12"), (13, "u13")])
    return app


def test_plan_only_creates_the_children_and_parks_the_parent(tmp_path):
    app = split_app(tmp_path)
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    assert [call.args[0] for call in app.github.create_issue.await_args_list] == [
        "Add the parser",
        "Add the API",
    ]
    assert [child.number for child in app.state.load_split(4)] == [12, 13]
    # SPLIT is not claimable, so the parent is parked without a plan of its own.
    assert app.state.rows()[0]["status"] == str(TaskStatus.SPLIT)
    assert app.state.load_plan(4) is None
    assert added_labels(app) == ["human-review"]
    removed = [
        name
        for call in app.github.labels.await_args_list
        for name in call.kwargs.get("remove", ())
    ]
    # A parent that is no longer being worked on must not keep advertising that
    # it is running or awaiting approval of a plan it will never have.
    assert removed == ["agent-running", "agent-planned", "agent-ready"]
    body = app.github.comment.await_args.args[1]
    assert "#12" in body and "#13" in body
    assert "agent-ready" in body
    # The parent's only recovery path is reset: adding the ready label to a
    # SPLIT parent is a no-op neither pool ever picks up, so the comment must
    # not suggest it.
    assert f"issue-agent reset {issue.number}" in body
    assert "add the `agent-ready` label — or run" not in body


def test_dry_run_split_completes_with_placeholder_children(tmp_path):
    """Dry-run must be able to exercise the whole split flow (spec §6.2):
    create_issue hands out distinct fake numbers, the no-op link and comment
    calls let the flow finish, and the parent lands on SPLIT."""
    from issue_agent.github import GitHub

    app = split_app(tmp_path)
    app.config.dry_run = True
    app.github = GitHub("", tmp_path, dry_run=True)
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    numbers = [child.number for child in app.state.load_split(4)]
    assert len(numbers) == 2 and len(set(numbers)) == 2
    assert all(number > 1_000_000_000 for number in numbers)
    assert app.state.rows()[0]["status"] == str(TaskStatus.SPLIT)


def test_split_children_never_inherit_workflow_labels(tmp_path):
    app = split_app(tmp_path)
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything", labels=("enhancement", "agent-ready", "agent:codex"))

    run_plan_only(app, issue)

    # `agent-ready` on a child would start something a human never released,
    # and the parent's agent route is a preference the human re-applies.
    assert app.github.create_issue.await_args_list[0].kwargs["labels"] == ("enhancement",)


def test_split_links_the_children_to_the_parent_and_to_each_other(tmp_path):
    app = split_app(tmp_path)
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    assert [call.args for call in app.github.link_parent.await_args_list] == [(12, 4), (13, 4)]
    assert [call.args for call in app.github.add_blocked_by.await_args_list] == [(13, 12)]
    # linked=True is what stops a retry from re-issuing links it already made.
    assert [child.linked for child in app.state.load_split(4)] == [True, True]


def test_split_writes_the_decision_down_before_creating_anything(tmp_path):
    app = split_app(tmp_path)
    proposes_split(app)
    app.github.create_issue = AsyncMock(side_effect=CommandError("gh: HTTP 500"))
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    # The record survives the failure, so the retry resumes this proposal
    # instead of asking the planner for a second, differently-worded one.
    assert [child.title for child in app.state.load_split(4)] == ["Add the parser", "Add the API"]
    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    assert added_labels(app) == []


def test_split_failure_keeps_the_parent_retryable_and_reports_progress(tmp_path):
    app = split_app(tmp_path, children=[(12, "u12"), CommandError("gh: HTTP 500")])
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    # A half-finished split is a failure to retry, not a result to review.
    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    assert added_labels(app) == []
    assert [child.number for child in app.state.load_split(4)] == [12, 0]
    assert "#12" in app.github.comment.await_args.args[1]
    assert "split_partial" in names(app)


def test_split_retry_creates_only_the_missing_children(tmp_path):
    app = split_app(tmp_path, children=[(13, "u13")])
    issue = Issue(4, "Too big", "Do everything")
    app.state.claim_for_planning(issue, "planner")
    app.state.save_split(
        4,
        [
            RecordedChild(title="Add the parser", body="Body one", number=12, url="u12", linked=True),
            RecordedChild(title="Add the API", body="Body two", depends_on=(0,)),
        ],
    )
    proposes_split(app, '{"split": [{"title": "Different wording", "body": "B"}]}')

    asyncio.run(app.plan_only(issue))

    # Titles come from an LLM and change between runs, so the retry must go by
    # the recorded proposal, not by asking again.
    app.agents["planner"].execute.assert_not_awaited()
    assert [call.args[0] for call in app.github.create_issue.await_args_list] == ["Add the API"]
    assert [call.args for call in app.github.link_parent.await_args_list] == [(13, 4)]
    assert [call.args for call in app.github.add_blocked_by.await_args_list] == [(13, 12)]
    assert app.state.rows()[0]["status"] == str(TaskStatus.SPLIT)
    assert "split_reused" in names(app)
    assert "split_created" in names(app)


def test_split_without_a_usable_issue_number_fails_the_plan(tmp_path):
    app = split_app(tmp_path, children=[(0, "")])
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    # 0 is what dry-run reports, and recording it as created would make the next
    # attempt skip a child that never existed.
    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    assert [child.number for child in app.state.load_split(4)] == [0, 0]
    assert added_labels(app) == []


def test_plan_only_refuses_a_split_when_splitting_is_disabled(tmp_path):
    app = make_orchestrator(tmp_path)
    proposes_split(app)
    issue = Issue(4, "Too big", "Do everything")

    run_plan_only(app, issue)

    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    assert "allow_split" in app.github.comment.await_args.args[1]
    app.github.create_issue.assert_not_awaited()


def test_run_once_leaves_a_split_parent_alone(tmp_path):
    app = split_app(tmp_path)
    issue = Issue(4, "Too big", "Do everything", labels=("human-review",))
    app.state.claim_for_planning(issue, "planner")
    app.state.update(4, TaskStatus.SPLIT)
    app.github.unassigned_issues = AsyncMock(return_value=[issue])
    admitted = track_admissions(app)

    asyncio.run(app.run_once())

    assert admitted == []
    app.github.labels.assert_not_awaited()


def test_run_once_releases_an_issue_a_human_has_answered(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[
            Comment("octocat", "2026-09-10T01:00:01Z", CLARIFY_QUESTION),
            # GitHub dates comments with a Z suffix while the marker is written
            # locally, so the two forms must compare.
            Comment("alice", "2026-09-10T02:00:00Z", "The parser module."),
        ]
    )

    asyncio.run(app.run_once())

    assert app.github.labels.await_args.args == (4,)
    assert app.github.labels.await_args.kwargs == {"remove": ("agent-needs-info",)}
    assert "clarify_answered" in names(app)


def test_run_once_ignores_discussion_from_before_the_question(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[
            # Older than the question, so it cannot be an answer to it.
            Comment("alice", "2026-09-09T23:00:00Z", "Let us discuss this next week."),
            Comment("octocat", "2026-09-10T01:00:01Z", CLARIFY_QUESTION),
        ]
    )

    asyncio.run(app.run_once())

    # Counting it would re-plan in a loop without anything having been answered.
    app.github.labels.assert_not_awaited()


def test_run_once_ignores_the_planners_own_question_comment(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[Comment("octocat", "2026-09-10T02:00:00+00:00", CLARIFY_QUESTION)]
    )

    asyncio.run(app.run_once())

    # The orchestrator posts as octocat; counting its own question as an answer
    # would leave it talking to itself.
    app.github.labels.assert_not_awaited()


def test_run_once_leaves_a_spent_clarification_parked(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.max_clarify_rounds = 1
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.state.record_clarify_round(4, "2026-09-10T03:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[Comment("alice", "2026-09-10T04:00:00+00:00", "The parser module.")]
    )

    asyncio.run(app.run_once())

    # A spent budget needs a human reset, so the reply must not re-queue it.
    app.github.labels.assert_not_awaited()
    app.github.comments.assert_not_awaited()


def test_run_once_skips_reply_detection_when_the_login_is_unknown(tmp_path):
    app = make_orchestrator(tmp_path)
    app.github.viewer_login = AsyncMock(return_value="")
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")

    asyncio.run(app.run_once())

    # Without a login there is no way to tell a human reply from the planner's
    # own question, so detection is off and the label stays for a human to drop.
    app.github.comments.assert_not_awaited()
    app.github.labels.assert_not_awaited()


def test_run_once_ignores_replies_from_configured_bots(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.clarify_ignore_authors = ("dependabot",)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[Comment("dependabot", "2026-09-10T02:00:00+00:00", "Bump to 1.2.3.")]
    )

    asyncio.run(app.run_once())

    app.github.labels.assert_not_awaited()


def test_run_once_hands_off_the_answer_and_stops_watching_for_it(tmp_path):
    """The handoff consumes the answer: the marker is cleared so a re-plan that
    keeps failing cannot make every later poll re-detect the same comment. The
    round budget stays spent and the transcript keeps flowing for the re-plan."""
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague", "Improve this")
    app.state.claim_for_planning(issue, "planner")
    app.state.record_clarify_round(4, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[
            Comment("octocat", "2026-09-10T01:00:01+00:00", CLARIFY_QUESTION),
            Comment("alice", "2026-09-10T02:00:00+00:00", "The parser module."),
        ]
    )

    asyncio.run(app.run_once())

    app.github.labels.assert_awaited_once()
    assert app.state.clarify_state(4) == (1, "")
    assert asyncio.run(app._clarification(4)) != ""


def test_run_once_keeps_scanning_after_one_label_removal_fails(tmp_path):
    """A failed needs-info removal (deleted label, gh hiccup) is contained:
    later waiting rows still get their answers released."""
    app = make_orchestrator(tmp_path)
    for number in (4, 5):
        app.state.claim_for_planning(Issue(number, "Vague", "Improve this"), "planner")
        app.state.record_clarify_round(number, "2026-09-10T01:00:00+00:00")
    app.github.comments = AsyncMock(
        return_value=[Comment("alice", "2026-09-10T02:00:00+00:00", "The parser module.")]
    )
    app.github.labels = AsyncMock(side_effect=[CommandError("label gone"), None])

    asyncio.run(app.run_once())

    assert app.github.labels.await_count == 2
    # Exactly one handoff completed: the failed row keeps its marker (the
    # answer is not consumed), the healthy row's marker is cleared. Row order
    # from the state DB is not pinned, so assert as a set.
    markers = [app.state.clarify_state(number)[1] == "" for number in (4, 5)]
    assert sorted(markers) == [False, True]


def test_clarification_marker_comes_from_the_posted_comment(tmp_path):
    """The marker is read back from the question comment GitHub just dated, so
    a local clock running fast cannot make an instant human reply count as
    'older than the marker' and stay undetected forever."""
    app = make_orchestrator(tmp_path)
    asks_questions(app)
    issue = Issue(4, "Vague", "Improve this")
    app.github.comments = AsyncMock(
        return_value=[
            Comment("alice", "2026-09-09T00:00:00Z", "unrelated discussion"),
            Comment("octocat", "2026-09-10T01:00:05Z", CLARIFY_QUESTION),
        ]
    )

    run_plan_only(app, issue)

    assert app.state.clarify_state(4) == (1, "2026-09-10T01:00:05Z")


def test_single_task_fallback_without_planner(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.planner_agent = ""
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 0
    assert app.workspaces.commit.await_args_list[0].args[1] == "feat: Task (#4)"
    assert app.workspaces.amend.await_count == 0
    app.workspaces.push.assert_awaited_once()
    assert app.state.load_plan(4) == [PlanTask("Task", "Body")]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE]
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_task_review_changes_amend_the_task_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["reviewer"].execute.side_effect = [
        result("Missing validation.\nVERDICT: REQUEST_CHANGES\n"),
        result("Fixed.\nVERDICT: APPROVE\n"),
        result(APPROVE),  # task two
        result(APPROVE),  # final review
    ]
    app.workspaces.changed.side_effect = [True, True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])

    run_process(app, issue)

    # plan already persisted -> planner not re-invoked
    assert app.agents["planner"].execute.await_count == 0
    # one commit per task; the rejected task was amended once
    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: Two (#4)"]
    assert app.workspaces.amend.await_count == 1
    app.workspaces.push.assert_awaited_once()
    # review feedback reached the next coding attempt
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "Missing validation" in worker_prompts[1]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE, TaskStatus.DONE]
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    review_log = (tmp_path / "logs" / "issue-4.reviews.jsonl").read_text(encoding="utf-8")
    assert "REQUEST_CHANGES" in review_log
    assert "APPROVE" in review_log
    execution_log = (tmp_path / "logs" / "issue-4.jsonl").read_text(encoding="utf-8")
    assert "task_attempt_failed" in execution_log
    assert "implementation_complete" in execution_log


def test_planner_output_is_persisted_and_commented(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 1
    assert "## Agent Plan" in app.github.comment.await_args_list[0].args[1]
    assert app.state.plan_task_statuses(4) == [TaskStatus.DONE, TaskStatus.DONE]


def test_unlabeled_issue_plan_waits_for_ready_before_coding(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Vague task", "Improve this module")
    assert app.state.claim_for_planning(issue, "planner") is True

    asyncio.run(app.plan_only(issue))

    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.PLANNED)
    assert app.agents["planner"].execute.await_count == 1
    assert "agent-ready" in app.github.comment.await_args.args[1]
    app.github.labels.assert_awaited_once_with(4, add=("agent-planned",))
    app.workspaces.push.assert_not_awaited()
    app.github.create_pr.assert_not_awaited()

    app.workspaces.changed.side_effect = [True, True, False]
    assert app.state.claim(issue, "worker") is True
    asyncio.run(app.process(issue, "worker"))

    assert app.agents["planner"].execute.await_count == 1
    app.github.labels.assert_any_await(
        4, add=("agent-running",), remove=("agent-ready", "agent-planned")
    )
    app.workspaces.push.assert_awaited_once()
    app.github.create_pr.assert_awaited_once()


def test_plan_only_restores_workspace_when_planner_writes_files(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.status.side_effect = ["", " M src/app.py"]
    issue = Issue(4, "Vague task", "Improve this module")
    assert app.state.claim_for_planning(issue, "planner") is True

    asyncio.run(app.plan_only(issue))

    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    app.workspaces.reset.assert_any_await(tmp_path, "HEAD")
    assert app.workspaces.clean.await_count >= 2
    assert "modified the workspace" in app.github.comment.await_args.args[1]


def test_run_once_routes_issue_without_agent_workflow_label_to_plan_only(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(12, "Needs planning", "A vague request", ("bug", "agent:claude"))
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[])
    app.github.unassigned_issues = AsyncMock(return_value=[issue])
    app.global_limit = asyncio.Semaphore(1)
    app.plan_only = AsyncMock()

    async def run_scheduler() -> None:
        await app.run_once()
        await asyncio.gather(*tuple(app.running.values()))

    asyncio.run(run_scheduler())

    app.github.unassigned_issues.assert_awaited_once_with(20, ready_label="agent-ready")
    app.plan_only.assert_awaited_once_with(issue)
    assert app.state.rows()[0]["status"] == str(TaskStatus.CLAIMED)


def test_run_once_reconciles_labels_for_persisted_human_review(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body", ("agent-running",))
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[issue])
    app.github.unassigned_issues = AsyncMock(return_value=[])
    app.state.claim(issue, "worker")
    app.state.update(4, TaskStatus.HUMAN_REVIEW, pr_url="https://example.test/pr/4")

    asyncio.run(app.run_once())

    app.github.labels.assert_awaited_once_with(
        4,
        add=("human-review",),
        remove=("agent-running", "agent-failed", "agent-ready"),
    )
    assert app.running == {}


def test_resume_reuses_plan_and_resets_to_last_done_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])
    app.state.update_plan_task(4, 0, status=TaskStatus.DONE, commit_hash="aaaa1111")

    run_process(app, issue)

    assert app.agents["planner"].execute.await_count == 0
    app.workspaces.reset.assert_awaited_once_with(tmp_path, "aaaa1111")
    # only the second task is implemented and committed
    assert [call.args[1] for call in app.workspaces.commit.await_args_list] == ["feat: Two (#4)"]
    assert app.agents["worker"].execute.await_count == 1
    app.workspaces.push.assert_awaited_once()


def test_resume_fails_when_completed_task_has_no_commit_anchor(tmp_path):
    app = make_orchestrator(tmp_path)
    app.state.claim(Issue(4, "Task", "Body"), "worker")
    app.state.save_plan(4, [PlanTask("One", "D"), PlanTask("Two", "D")])
    app.state.update_plan_task(4, 0, status=TaskStatus.DONE)

    with pytest.raises(CommandError, match="missing commit anchor"):
        asyncio.run(app._reset_to_anchor(tmp_path, 4, 1))

    app.workspaces.reset.assert_not_awaited()


def test_final_review_changes_produce_a_fix_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),  # task one
        result("Missing docs.\nVERDICT: REQUEST_CHANGES\n"),  # final review
        result("Docs added.\nVERDICT: APPROVE\n"),  # final re-review
    ]
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: final review fixes (#4)"]
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "Missing docs" in worker_prompts[1]
    app.workspaces.push.assert_awaited_once()


def test_final_checks_failure_triggers_a_fix_commit(tmp_path, monkeypatch):
    app = make_orchestrator(tmp_path)
    app.config.checks = ("pytest -q",)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),  # task one
        result(APPROVE),  # final attempt 1 (checks fail)
        result(APPROVE),  # final attempt 2 after fix
    ]
    calls = {"n": 0}

    async def fake_shell(command: str, **kwargs: object) -> Result:
        # call order: baseline capture, task checks, final attempt-1 checks, final attempt-2 checks
        calls["n"] += 1
        if calls["n"] == 3:  # final checks of attempt 1 fail
            raise CommandError("pytest failed: 1 failed")
        return result()

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    messages = [call.args[1] for call in app.workspaces.commit.await_args_list]
    assert messages == ["feat: One (#4)", "feat: final review fixes (#4)"]
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert "pytest failed" in worker_prompts[1]
    assert calls["n"] == 4
    app.workspaces.push.assert_awaited_once()


def test_exhausted_task_review_fails_and_keeps_issue_reclaimable(tmp_path):
    app = make_orchestrator(tmp_path, attempts=1)
    app.agents["reviewer"].execute.return_value = result("Bad.\nVERDICT: REQUEST_CHANGES\n")
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "review requested changes" in row["last_error"]
    assert app.github.comment.await_args.args[1].startswith("Agent run failed.")
    # the failed task is not DONE, so a later re-claim resumes it
    assert app.state.plan_task_statuses(4) != [TaskStatus.DONE]


def test_second_task_review_rejection_stops_without_auto_retry(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result("First feedback.\nVERDICT: REQUEST_CHANGES\n"),
        result("Latest feedback.\nVERDICT: REQUEST_CHANGES\n"),
    ]
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 2
    assert app.agents["reviewer"].execute.await_count == 2
    app.workspaces.push.assert_not_awaited()
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    removes = app.github.labels.await_args_list[-1].kwargs["remove"]
    assert adds == ("agent-failed",)
    assert "agent-ready" in removes
    assert app.state.plan_task_statuses(4) == [TaskStatus.PENDING]
    assert "Latest feedback" in app.state.plan_task_last_error(4, 0)


def test_invalid_task_review_verdict_requeues_within_failure_budget(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.return_value = result("Review completed without a verdict.")
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 1
    assert app.agents["reviewer"].execute.await_count == 1
    assert app.workspaces.push.await_count == 0
    labels = app.github.labels.await_args_list[-1].kwargs
    assert "agent-ready" in labels["add"]
    assert "agent-failed" in labels["add"]
    assert app.state.plan_task_statuses(4) == [TaskStatus.PENDING]


def test_task_reviewer_write_is_reverted_and_requeued(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.workspaces.status.side_effect = ["", " M src/app.py"]
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    app.workspaces.reset.assert_any_await(tmp_path, "HEAD")
    assert app.workspaces.clean.await_count >= 2
    labels = app.github.labels.await_args_list[-1].kwargs
    assert "agent-ready" in labels["add"]
    assert app.state.plan_task_statuses(4) == [TaskStatus.PENDING]


def test_second_final_review_rejection_stops_without_auto_retry(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Need docs.\nVERDICT: REQUEST_CHANGES\n"),
        result("Still missing docs.\nVERDICT: REQUEST_CHANGES\n"),
    ]
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 2
    assert app.agents["reviewer"].execute.await_count == 3
    app.workspaces.push.assert_not_awaited()
    assert app.github.labels.await_args_list[-1].kwargs["add"] == ("agent-failed",)


def test_final_review_retry_resumes_from_final_fix_commit(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Need docs.\nVERDICT: REQUEST_CHANGES\n"),
        result("Still missing docs.\nVERDICT: REQUEST_CHANGES\n"),
        result(APPROVE),
    ]
    app.workspaces.changed.side_effect = [True, True, False]
    # heads: task-1 commit, final-fix commit, and the approval probe after the
    # retry's finalize (HEAD stays at the final-fix commit)
    app.workspaces.head_commit.side_effect = ["task111", "final222", "final222"]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    expected_error = (
        "final review requested changes after the allowed fix cycle:\n"
        "Still missing docs.\nVERDICT: REQUEST_CHANGES\n"
    )
    assert app.state.final_context(4) == ("final222", expected_error)
    app.workspaces.reset.reset_mock()
    assert app.state.claim(issue, "worker", max_attempts=3) is True
    asyncio.run(app.process(issue, "worker"))

    app.workspaces.reset.assert_awaited_once_with(tmp_path, "final222")
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_awaited_once()
    assert app.state.final_context(4) == ("final222", "")


def test_final_fix_agent_error_is_persisted(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Need docs.\nVERDICT: REQUEST_CHANGES\n"),
    ]
    app.agents["worker"].execute.side_effect = [result(), CommandError("final agent unavailable")]
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert "final agent unavailable" in app.state.final_context(4)[1]


def test_invalid_final_review_verdict_requeues_without_coding_fix(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),
        result("Final review omitted its verdict."),
    ]
    app.workspaces.changed.side_effect = [True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 1
    assert app.agents["reviewer"].execute.await_count == 2
    assert app.workspaces.push.await_count == 0
    labels = app.github.labels.await_args_list[-1].kwargs
    assert "agent-ready" in labels["add"]
    assert "agent-failed" in labels["add"]


def test_command_errors_from_coding_are_retried(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute.side_effect = [CommandError("first failure"), result()]
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert app.agents["worker"].execute.await_count == 2
    assert "first failure" in app.agents["worker"].execute.await_args_list[1].args[1]
    app.workspaces.push.assert_awaited_once()


def test_prompts_include_codegraph_guidance_when_index_ready(tmp_path):
    app = make_orchestrator(tmp_path)
    (tmp_path / ".codegraph").mkdir()
    app.workspaces.changed.side_effect = [True, True, False]

    run_process(app, Issue(4, "Task", "Body"))

    planner_prompt = app.agents["planner"].execute.await_args_list[0].args[1]
    assert "codegraph" in planner_prompt
    worker_prompts = [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    assert all("codegraph" in prompt for prompt in worker_prompts)
    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    assert all("codegraph" in prompt for prompt in reviewer_prompts)


def test_prompts_omit_codegraph_block_without_index(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, True, False]

    run_process(app, Issue(4, "Task", "Body"))

    prompts = [app.agents["planner"].execute.await_args_list[0].args[1]]
    prompts += [call.args[1] for call in app.agents["worker"].execute.await_args_list]
    prompts += [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    assert all("codegraph" not in prompt for prompt in prompts)


def test_task_retry_writes_feedback_file_and_prompt_points_to_it(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute.side_effect = [CommandError("first failure"), result()]
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    app.workspaces.write_feedback_file.assert_called_once_with(tmp_path, "first failure")
    retry_prompt = app.agents["worker"].execute.await_args_list[1].args[1]
    assert ".agent/feedback.md" in retry_prompt
    assert "first failure" in retry_prompt


def test_final_fix_writes_feedback_file_and_prompt_points_to_it(tmp_path):
    app = make_orchestrator(tmp_path)
    app.agents["reviewer"].execute.side_effect = [
        result(APPROVE),  # task one
        result("Missing docs.\nVERDICT: REQUEST_CHANGES\n"),  # final review
        result("Docs added.\nVERDICT: APPROVE\n"),  # final re-review
    ]
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    feedback_texts = [call.args[1] for call in app.workspaces.write_feedback_file.call_args_list]
    assert any("Missing docs" in text for text in feedback_texts)
    fix_prompt = app.agents["worker"].execute.await_args_list[1].args[1]
    assert ".agent/feedback.md" in fix_prompt
    assert "Missing docs" in fix_prompt


def test_task_attempt_budget_is_independent_from_issue_retry_budget(tmp_path):
    app = make_orchestrator(tmp_path, attempts=3)
    app.config.max_task_attempts = 1
    app.config.reviewer_agent = ""
    app.agents["worker"].execute.side_effect = CommandError("agent unavailable")
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 1
    row = app.state.rows()[0]
    assert row["failures"] == 1
    assert row["status"] == str(TaskStatus.FAILED)
    assert "agent-ready" in app.github.labels.await_args_list[-1].kwargs["add"]


def test_unexpected_errors_remain_blocked(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.create.side_effect = RuntimeError("database unavailable")

    run_process(app, Issue(4, "Task", "Body"))

    assert app.state.rows()[0]["status"] == str(TaskStatus.BLOCKED)


def test_failure_restores_ready_until_attempt_budget_exhausted(tmp_path):
    app = make_orchestrator(tmp_path, attempts=2)
    app.agents["planner"].execute.side_effect = CommandError("plan boom")
    issue = Issue(4, "Task", "Body")

    assert app.state.claim(issue, "worker", max_attempts=2) is True
    asyncio.run(app.process(issue, "worker"))
    # first failure: attempts=1 < 2 -> kept runnable via agent-ready
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    removes = app.github.labels.await_args_list[-1].kwargs["remove"]
    assert "agent-ready" in adds
    assert "agent-failed" in adds
    assert "agent-running" in removes
    assert app.state.rows()[0]["failures"] == 1

    app.github.labels.reset_mock()
    app.github.comment.reset_mock()
    assert app.state.claim(issue, "worker", max_attempts=2) is True
    asyncio.run(app.process(issue, "worker"))
    # second failure: failures=2 >= budget -> parked, no agent-ready restored
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    assert "agent-ready" not in adds
    assert app.state.rows()[0]["failures"] == 2
    assert "agent-ready" in app.github.comment.await_args.args[1]


def test_blocked_restores_ready_until_attempt_budget_exhausted(tmp_path):
    app = make_orchestrator(tmp_path, attempts=2)
    app.workspaces.create.side_effect = RuntimeError("database unavailable")
    issue = Issue(4, "Task", "Body")

    run_process(app, issue)
    # BLOCKED under budget -> kept runnable
    assert "agent-ready" in app.github.labels.await_args_list[-1].kwargs["add"]
    assert app.state.rows()[0]["status"] == str(TaskStatus.BLOCKED)
    assert app.state.rows()[0]["failures"] == 1

    app.github.labels.reset_mock()
    app.github.comment.reset_mock()
    run_process(app, issue)
    # second BLOCKED: budget exhausted -> parked
    adds = app.github.labels.await_args_list[-1].kwargs["add"]
    assert "agent-ready" not in adds
    assert app.state.rows()[0]["failures"] == 2


def test_recovery_replans_planning_and_resumes_planned(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(1, "A", ""), "codex")
    state.update(1, TaskStatus.PLANNING)
    state.claim(Issue(2, "B", ""), "codex")
    state.update(2, TaskStatus.CODING)
    state.save_plan(2, [PlanTask("T1", "d")])
    state.update_plan_task(2, 0, status=TaskStatus.CODING)

    assert state.recover_interrupted() == 2

    rows = {row["issue_number"]: row for row in state.rows()}
    assert rows[1]["status"] == str(TaskStatus.PENDING)
    assert rows[2]["status"] == str(TaskStatus.PLANNED)
    assert state.plan_task_statuses(2) == [TaskStatus.PENDING]


def test_recovery_marks_inflight_without_plan_failed(tmp_path):
    """An active row with no plan goes back to PENDING under budget — no
    interruption path skips the retry budget — and parks as FAILED only once
    the budget is exhausted."""
    state = StateStore(tmp_path / "state.db")
    state.claim(Issue(3, "C", ""), "codex")
    state.update(3, TaskStatus.TESTING)

    assert state.recover_interrupted(max_attempts=3) == 1
    assert state.rows()[0]["status"] == str(TaskStatus.PENDING)

    state.update(3, TaskStatus.TESTING)
    assert state.recover_interrupted(max_attempts=1) == 1
    assert state.rows()[0]["status"] == str(TaskStatus.FAILED)


# ---------------------------------------------------------------------------
# review_task_mode tests
# ---------------------------------------------------------------------------

def test_formal_review_mode_skips_llm_reviewer(tmp_path, monkeypatch):
    """When review_task_mode=formal, the LLM reviewer agent is never called for tasks."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "formal"
    monkeypatch.setattr(
        "issue_agent.orchestrator.formal_review",
        lambda ws: FormalReviewResult(approved=True, reason=""),
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # LLM reviewer not called for task review (only final review)
    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    task_review_prompts = [p for p in reviewer_prompts if "most recent commit" in p]
    assert len(task_review_prompts) == 0, \
        f"LLM task reviewer should not be called in formal mode, got: {task_review_prompts}"
    app.workspaces.push.assert_awaited_once()


def test_formal_review_mode_rejects_secret_in_diff(tmp_path, monkeypatch):
    """Formal review detects secrets and triggers a retry."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "formal"
    call_count = {"n": 0}

    def fake_formal_review(workspace):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FormalReviewResult(approved=False, reason="Potential AWS access key detected")
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", fake_formal_review)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert call_count["n"] == 2  # first rejected, second approved
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_awaited_once()


def test_review_off_mode_skips_all_task_review(tmp_path, monkeypatch):
    """When review_task_mode=off, no task review happens at all."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "off"
    monkeypatch.setattr(
        "issue_agent.orchestrator.formal_review",
        lambda ws: FormalReviewResult(approved=True, reason=""),
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # reviewer only called for final review, not task review
    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    task_review_prompts = [p for p in reviewer_prompts if "most recent commit" in p]
    assert len(task_review_prompts) == 0
    app.workspaces.push.assert_awaited_once()


def test_full_review_mode_uses_llm_reviewer(tmp_path, monkeypatch):
    """When review_task_mode=full, the existing LLM reviewer is used for tasks."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "full"
    monkeypatch.setattr(
        "issue_agent.orchestrator.formal_review",
        lambda ws: FormalReviewResult(approved=True, reason=""),
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    reviewer_prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    task_review_prompts = [p for p in reviewer_prompts if "most recent commit" in p]
    assert len(task_review_prompts) == 1
    app.workspaces.push.assert_awaited_once()


# ---------------------------------------------------------------------------
# agent_call usage logging tests
# ---------------------------------------------------------------------------

def test_execute_agent_logs_usage_and_duration(tmp_path):
    """_execute_agent records an agent_call event with duration and token usage."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout="done",
            stderr="",
            duration_ms=4200,
            usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        )
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log))

    agent_calls = [e for e in log.events if e[0] == "agent_call"]
    assert len(agent_calls) == 1
    _, fields = agent_calls[0]
    assert fields["agent"] == "worker"
    assert fields["role"] == "worker"
    assert fields["duration_ms"] == 4200
    assert fields["input_tokens"] == 100
    assert fields["output_tokens"] == 50
    assert fields["cost_usd"] == 0.01


def test_execute_read_only_logs_usage_with_role(tmp_path):
    """_execute_read_only records agent_call with the caller-supplied role."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["reviewer"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout=APPROVE,
            stderr="",
            duration_ms=3100,
            usage={"input_tokens": 80, "output_tokens": 20, "cache_read_input_tokens": 500},
        )
    )

    asyncio.run(
        app._execute_read_only("reviewer", tmp_path, "p", role="task reviewer", issue_log=log)
    )

    agent_calls = [e for e in log.events if e[0] == "agent_call"]
    assert len(agent_calls) == 1
    _, fields = agent_calls[0]
    assert fields["role"] == "task reviewer"
    assert fields["duration_ms"] == 3100
    assert fields["cache_read_input_tokens"] == 500


def test_execute_agent_without_issue_log_does_not_fail(tmp_path):
    """Backward compat: omitting issue_log skips logging without raising."""
    app = make_orchestrator(tmp_path)
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(returncode=0, stdout="ok", stderr="", duration_ms=10)
    )

    res = asyncio.run(app._execute_agent("worker", tmp_path, "prompt"))
    assert res.stdout == "ok"


def test_execute_agent_logs_duration_when_usage_absent(tmp_path):
    """Plain-text CLI (no JSON envelope): duration still logged, tokens omitted."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(returncode=0, stdout="ok", stderr="", duration_ms=999, usage=None)
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log))

    agent_calls = [e for e in log.events if e[0] == "agent_call"]
    assert len(agent_calls) == 1
    _, fields = agent_calls[0]
    assert fields["duration_ms"] == 999
    assert "input_tokens" not in fields


def test_execute_agent_accumulates_usage_in_state_db(tmp_path):
    """Dual-write: agent_call usage also lands in the state DB totals."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout="done",
            stderr="",
            duration_ms=2000,
            usage={"input_tokens": 100, "output_tokens": 40, "cost_usd": 0.008},
        )
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log, issue_number=4))

    row = next(r for r in app.state.rows() if r["issue_number"] == 4)
    assert row["total_input_tokens"] == 100
    assert row["total_output_tokens"] == 40
    assert row["total_cost_usd"] == 0.008
    assert row["total_duration_ms"] == 2000
    # JSONL log still written (dual-channel preserved)
    assert any(e[0] == "agent_call" for e in log.events)


def test_execute_read_only_accumulates_usage_in_state_db(tmp_path):
    """Read-only agents (planner/reviewer) also accumulate into state DB."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.agents["reviewer"].execute = AsyncMock(
        return_value=Result(
            returncode=0,
            stdout=APPROVE,
            stderr="",
            duration_ms=1500,
            usage={"input_tokens": 60, "output_tokens": 15, "cache_read_input_tokens": 300},
        )
    )

    asyncio.run(
        app._execute_read_only(
            "reviewer", tmp_path, "p", role="task reviewer", issue_log=log, issue_number=4
        )
    )

    row = next(r for r in app.state.rows() if r["issue_number"] == 4)
    assert row["total_input_tokens"] == 60
    assert row["total_cache_read_tokens"] == 300
    assert row["total_duration_ms"] == 1500


def test_execute_agent_without_issue_number_skips_state_write(tmp_path):
    """Backward compat: omitting issue_number logs to JSONL only, no state write."""
    app = make_orchestrator(tmp_path)
    log = _Log()
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0, stdout="ok", stderr="", duration_ms=10,
            usage={"input_tokens": 5},
        )
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "prompt", issue_log=log))

    assert any(e[0] == "agent_call" for e in log.events)
    # no state rows touched
    assert app.state.rows() == []


# ---------------------------------------------------------------------------
# formal review without reviewer_agent (Fix #1)
# ---------------------------------------------------------------------------

def test_formal_review_runs_without_reviewer_agent(tmp_path, monkeypatch):
    """formal mode is deterministic — it must run even with no reviewer agent configured."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, reviewer="")
    app.config.review_task_mode = "formal"
    calls = {"n": 0}

    def fake_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", fake_formal_review)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert calls["n"] == 1, "formal review must run without reviewer_agent"
    app.workspaces.push.assert_awaited_once()
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_formal_review_without_reviewer_agent_uses_review_attempt_budget(tmp_path, monkeypatch):
    """With formal review active, attempt_limit is _REVIEW_ATTEMPTS (2), not max_task_attempts (3)."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, attempts=3, reviewer="")
    app.config.review_task_mode = "formal"
    calls = {"n": 0}

    def rejecting_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=False, reason="Forbidden file modified: .env")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", rejecting_formal_review)
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # review budget is 2 attempts even though max_task_attempts is 3
    assert calls["n"] == 2
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "formal review rejected after the allowed fix cycle" in row["last_error"]


def test_check_failure_retries_honor_max_task_attempts_when_review_is_active(tmp_path):
    """With review active, raising max_task_attempts above 2 extends check/agent-failure
    retries; the review-rejection cap stays at two rejections (see the tests below)."""
    app = make_orchestrator(tmp_path, attempts=1)
    app.config.review_task_mode = "formal"
    app.config.reviewer_agent = ""
    app.config.max_task_attempts = 4
    app.agents["worker"].execute.side_effect = CommandError("agent unavailable")
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 4
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)


def test_review_rejection_cap_survives_larger_max_task_attempts(tmp_path, monkeypatch):
    """A larger max_task_attempts must not extend the review fix cycle: the second
    rejection still terminates immediately."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, attempts=1)
    app.config.review_task_mode = "formal"
    app.config.reviewer_agent = ""
    app.config.max_task_attempts = 4
    calls = {"n": 0}

    def rejecting_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=False, reason="Forbidden file modified: .env")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", rejecting_formal_review)
    app.workspaces.changed.side_effect = [True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert calls["n"] == 2
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "formal review rejected after the allowed fix cycle" in row["last_error"]


def test_second_review_rejection_terminates_regardless_of_attempt_index(tmp_path, monkeypatch):
    """The two-rejection cap counts rejections, not attempt indexes: after a check
    failure consumes attempt 1, rejections on attempts 2 and 3 still terminate on 3."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, attempts=1)
    app.config.review_task_mode = "formal"
    app.config.reviewer_agent = ""
    app.config.max_task_attempts = 4
    app.config.checks = ("pytest -q",)
    calls = {"shell": 0, "review": 0}

    async def fake_shell(command: str, **kwargs: object) -> Result:
        # call order: baseline capture, attempt-1 checks (fail), attempt-2/3 checks (pass)
        calls["shell"] += 1
        if calls["shell"] == 2:
            raise CommandError("pytest failed: 1 failed")
        return result()

    def rejecting_formal_review(workspace):
        calls["review"] += 1
        return FormalReviewResult(approved=False, reason="Forbidden file modified: .env")

    monkeypatch.setattr("issue_agent.checks.shell", fake_shell)
    monkeypatch.setattr("issue_agent.orchestrator.formal_review", rejecting_formal_review)
    app.workspaces.changed.side_effect = [True, True, True]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.agents["worker"].execute.await_count == 3
    assert calls["review"] == 2
    app.workspaces.push.assert_not_awaited()
    row = app.state.rows()[0]
    assert row["status"] == str(TaskStatus.FAILED)
    assert "formal review rejected after the allowed fix cycle" in row["last_error"]


def test_full_mode_without_reviewer_agent_skips_review(tmp_path, monkeypatch):
    """full mode requires an LLM reviewer; without one, review is skipped (backward compat)."""
    from issue_agent.formal_review import FormalReviewResult

    app = make_orchestrator(tmp_path, reviewer="")
    app.config.review_task_mode = "full"
    calls = {"n": 0}

    def fake_formal_review(workspace):
        calls["n"] += 1
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", fake_formal_review)
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert calls["n"] == 0, "full mode must not fall back to formal review"
    app.workspaces.push.assert_awaited_once()


def test_formal_review_git_failure_retries_via_command_error(tmp_path, monkeypatch):
    """A transient git failure in formal review raises CommandError and the task retries."""
    from issue_agent.formal_review import FormalReviewResult
    from issue_agent.process import CommandError

    app = make_orchestrator(tmp_path)
    app.config.review_task_mode = "formal"
    calls = {"n": 0}

    def flaky_formal_review(workspace):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CommandError("git diff --name-only HEAD^ HEAD failed (exit 128): lock busy")
        return FormalReviewResult(approved=True, reason="")

    monkeypatch.setattr("issue_agent.orchestrator.formal_review", flaky_formal_review)
    app.workspaces.changed.side_effect = [True, True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    # first call raised (retryable), second approved — issue still completes
    assert calls["n"] == 2
    assert app.agents["worker"].execute.await_count == 2
    app.workspaces.push.assert_awaited_once()
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)


def test_process_accumulates_usage_from_all_agent_calls_in_state_db(tmp_path):
    """E2E: a full process() run sums worker + task-reviewer + final-reviewer usage into the DB."""
    app = make_orchestrator(tmp_path)
    # worker: one call with usage
    app.agents["worker"].execute = AsyncMock(
        return_value=Result(
            returncode=0, stdout="", stderr="", duration_ms=1000,
            usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        )
    )
    # reviewer: task review + final review, both APPROVE with usage
    app.agents["reviewer"].execute = AsyncMock(
        return_value=Result(
            returncode=0, stdout=APPROVE, stderr="", duration_ms=500,
            usage={"input_tokens": 30, "output_tokens": 10, "cost_usd": 0.003},
        )
    )
    app.workspaces.changed.side_effect = [True, False]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)

    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    row = next(r for r in app.state.rows() if r["issue_number"] == 4)
    # worker(1) + task reviewer(1) + final reviewer(1)
    assert row["total_input_tokens"] == 100 + 30 + 30
    assert row["total_output_tokens"] == 50 + 10 + 10
    assert abs(row["total_cost_usd"] - (0.01 + 0.003 + 0.003)) < 1e-9
    assert row["total_duration_ms"] == 1000 + 500 + 500
    task = app.state.report_rows(4)[0]["tasks"][0]
    # Task metrics include its worker and task reviewer, but not the final reviewer.
    assert task["total_input_tokens"] == 100 + 30
    assert task["total_output_tokens"] == 50 + 10
    assert task["total_duration_ms"] == 1000 + 500


def test_failed_agent_call_is_attributed_to_task_and_counted(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("Implement", "Details")])
    app.agents["worker"].execute = AsyncMock(
        side_effect=CommandError(
            "agent failed",
            result=Result(
                1,
                "",
                "boom",
                duration_ms=700,
                usage={"input_tokens": 40, "output_tokens": 5},
            ),
        )
    )
    log = _Log()

    with pytest.raises(CommandError, match="agent failed"):
        asyncio.run(
            app._execute_agent(
                "worker",
                tmp_path,
                "prompt",
                issue_log=log,
                issue_number=4,
                seq=0,
                attempt=2,
            )
        )

    task = app.state.report_rows(4)[0]["tasks"][0]
    assert task["total_input_tokens"] == 40
    assert task["total_output_tokens"] == 5
    assert task["total_duration_ms"] == 700
    event = next(fields for name, fields in log.events if name == "agent_call")
    assert event["success"] is False
    assert event["sequence"] == 0
    assert event["attempt"] == 2


def test_timed_out_agent_call_duration_is_counted_without_usage(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("Implement", "Details")])
    app.agents["worker"].execute = AsyncMock(
        side_effect=CommandError("timed out", duration_ms=900)
    )

    with pytest.raises(CommandError, match="timed out"):
        asyncio.run(
            app._execute_agent(
                "worker", tmp_path, "prompt", issue_number=4, seq=0, attempt=1
            )
        )

    task = app.state.report_rows(4)[0]["tasks"][0]
    assert task["total_duration_ms"] == 900
    assert task["total_input_tokens"] == 0


def test_worker_session_is_reused_when_resume_command_is_configured(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    execute = AsyncMock(
        side_effect=[
            Result(0, "ok", "", usage={"session_id": "thread-4"}),
            Result(0, "ok", ""),
        ]
    )
    app.agents["worker"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=("worker", "resume", "{session_id}"),
            review_resume_command=None,
        ),
        execute=execute,
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "first", issue_number=4))
    asyncio.run(app._execute_agent("worker", tmp_path, "second", issue_number=4))

    assert execute.await_args_list[1].kwargs["session_id"] == "thread-4"


def test_failed_resumed_call_clears_session_so_next_attempt_starts_fresh(tmp_path):
    """A dead/expired session must not poison every retry: after a resumed call
    fails, the stored session is dropped and the next call starts fresh."""
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    execute = AsyncMock(
        side_effect=[
            Result(0, "ok", "", usage={"session_id": "thread-4"}),
            CommandError("resume failed: session not found"),
            Result(0, "ok", ""),
        ]
    )
    app.agents["worker"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=("worker", "resume", "{session_id}"),
            review_resume_command=None,
        ),
        execute=execute,
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "first", issue_number=4))
    with pytest.raises(CommandError):
        asyncio.run(app._execute_agent("worker", tmp_path, "second", issue_number=4))
    asyncio.run(app._execute_agent("worker", tmp_path, "third", issue_number=4))

    assert execute.await_args_list[1].kwargs["session_id"] == "thread-4"
    assert "session_id" not in execute.await_args_list[2].kwargs
    assert app.state.load_session(4, "worker", "worker") == ""


def test_failed_call_with_result_session_id_stays_cleared(tmp_path):
    """agents.py unwraps failed output too, so a CommandError can carry a Result
    whose usage already has a session_id (Codex emits thread.started before the
    turn fails). Logging the failed call must not save that poisoned id right
    back over the clear_session the failure path just performed."""
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    poisoned = Result(1, "", "boom", usage={"session_id": "thread-4"})
    execute = AsyncMock(
        side_effect=[
            Result(0, "ok", "", usage={"session_id": "thread-4"}),
            CommandError("resume failed: session not found", result=poisoned),
            Result(0, "ok", ""),
        ]
    )
    app.agents["worker"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=("worker", "resume", "{session_id}"),
            review_resume_command=None,
        ),
        execute=execute,
    )

    asyncio.run(app._execute_agent("worker", tmp_path, "first", issue_number=4))
    with pytest.raises(CommandError):
        asyncio.run(app._execute_agent("worker", tmp_path, "second", issue_number=4))
    asyncio.run(app._execute_agent("worker", tmp_path, "third", issue_number=4))

    assert execute.await_args_list[1].kwargs["session_id"] == "thread-4"
    assert "session_id" not in execute.await_args_list[2].kwargs
    assert app.state.load_session(4, "worker", "worker") == ""


def test_failed_resumed_review_call_clears_reviewer_session(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_session(4, "reviewer", "reviewer", "thread-9")
    execute = AsyncMock(side_effect=[CommandError("resume failed"), result(APPROVE)])
    app.agents["reviewer"] = SimpleNamespace(
        config=SimpleNamespace(
            resume_command=None,
            review_resume_command=("reviewer", "resume", "{session_id}"),
        ),
        execute=execute,
    )

    with pytest.raises(CommandError):
        asyncio.run(
            app._execute_read_only(
                "reviewer", tmp_path, "review", role="task reviewer", issue_number=4
            )
        )
    asyncio.run(
        app._execute_read_only(
            "reviewer", tmp_path, "review", role="task reviewer", issue_number=4
        )
    )

    assert execute.await_args_list[0].kwargs["session_id"] == "thread-9"
    assert "session_id" not in execute.await_args_list[1].kwargs
    assert app.state.load_session(4, "reviewer", "reviewer") == ""


def test_push_failure_retry_reuses_approved_final_review(tmp_path):
    """A push-failure retry lands on the same approved commit: finalize (final review +
    full checks) must be reused, not re-run, while HEAD matches."""
    app = make_orchestrator(tmp_path)
    app.workspaces.push.side_effect = [CommandError("push failed"), None]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    run_process(app, issue)
    assert app.state.rows()[0]["status"] == str(TaskStatus.FAILED)
    reviewer_calls = app.agents["reviewer"].execute.await_count
    worker_calls = app.agents["worker"].execute.await_count

    assert app.state.claim(issue, "worker")
    asyncio.run(app.process(issue, "worker"))

    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    assert app.agents["reviewer"].execute.await_count == reviewer_calls
    assert app.agents["worker"].execute.await_count == worker_calls
    assert app.workspaces.push.await_count == 2
    execution_log = (tmp_path / "logs" / "issue-4.jsonl").read_text(encoding="utf-8")
    assert "final_review_reused" in execution_log


def test_final_review_reruns_when_head_differs_from_approved_commit(tmp_path):
    app = make_orchestrator(tmp_path)
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])
    app.state.update_plan_task(4, 0, status=TaskStatus.DONE, commit_hash="abc1234")
    app.state.set_final_approved(4, "stale000")

    run_process(app, issue)

    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    assert app.state.final_approved_commit(4) == "abc1234"
    prompts = [call.args[1] for call in app.agents["reviewer"].execute.await_args_list]
    assert any("complete implementation" in prompt for prompt in prompts)


def test_failed_issue_requeues_with_configured_ready_label(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.ready_label = "automation-ready"
    issue = Issue(4, "Task", "Body")

    asyncio.run(app._park_or_requeue(issue, failures=1))

    app.github.labels.assert_awaited_once_with(
        4,
        add=("agent-failed", "automation-ready"),
        remove=("agent-running",),
    )


def test_tracked_worker_completion_wakes_scheduler(tmp_path):
    app = make_orchestrator(tmp_path)
    app.running = {}
    app._wake = asyncio.Event()

    async def run():
        app._track(4, asyncio.sleep(0))
        await asyncio.gather(*tuple(app.running.values()))
        await asyncio.sleep(0)
        return app._wake.is_set()

    assert asyncio.run(run()) is True


def test_notification_failure_after_pr_does_not_rerun_implementation(tmp_path):
    app = make_orchestrator(tmp_path)
    app.workspaces.changed.side_effect = [True, False]
    app.github.labels.side_effect = [None, CommandError("label unavailable")]
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])

    asyncio.run(app.process(issue, "worker"))

    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    assert app.agents["worker"].execute.await_count == 1
    log = (tmp_path / "logs" / "issue-4.jsonl").read_text(encoding="utf-8")
    assert "publication_notification_failed" in log


@pytest.mark.parametrize("planning", [False, True])
@pytest.mark.parametrize("database", [False, True])
def test_schedule_rechecks_after_resource_wait(tmp_path, planning, database):
    app = make_orchestrator(tmp_path)
    app.running = {}
    app.global_limit = asyncio.Semaphore(1)
    app.database_lock = asyncio.Lock()
    app.config.schedule = SimpleNamespace(allows=Mock(return_value=True))
    issue = Issue(44, "Queued", "Body", ("resource:database-schema",) if database else ())
    app.github.runnable_issues = AsyncMock(return_value=[] if planning else [issue])
    app.github.unassigned_issues = AsyncMock(return_value=[issue] if planning else [])
    app.process = AsyncMock()
    app.plan_only = AsyncMock()

    async def scenario():
        lock = app.database_lock if database and not planning else app.global_limit
        await lock.acquire()
        await app.run_once()
        await asyncio.sleep(0)
        assert app.state.rows() == []
        await app.run_once()
        assert len(app.running) == 1
        app.config.schedule.allows.return_value = False
        pending = tuple(app.running.values())
        lock.release()
        await asyncio.gather(*pending)
        assert app.state.rows() == []
        app.process.assert_not_awaited()
        app.plan_only.assert_not_awaited()
        app.github.labels.assert_not_awaited()
        app.config.schedule.allows.return_value = True
        await app.run_once()
        await asyncio.gather(*tuple(app.running.values()))
        assert app.state.rows()[0]["status"] == str(TaskStatus.CLAIMED)
        (app.plan_only if planning else app.process).assert_awaited_once()

    asyncio.run(scenario())


def test_schedule_closed_skips_poll_and_logs_once(tmp_path, caplog):
    app = make_orchestrator(tmp_path)
    app.config.schedule = SimpleNamespace(allows=Mock(return_value=False))
    app.github.runnable_issues = AsyncMock()
    app.github.unassigned_issues = AsyncMock()
    with caplog.at_level("INFO"):
        asyncio.run(app.run_once())
        asyncio.run(app.run_once())
    assert caplog.text.count("execution window closed") == 1
    assert app.state.rows() == []
    app.github.runnable_issues.assert_not_awaited()
    app.github.unassigned_issues.assert_not_awaited()


def test_schedule_does_not_interrupt_started_issue(tmp_path):
    app = make_orchestrator(tmp_path)
    app.global_limit = asyncio.Semaphore(1)
    app.config.schedule = SimpleNamespace(allows=Mock(return_value=True))
    issue = Issue(4, "Task", "Body")
    app.state.claim(issue, "worker")
    app.state.save_plan(4, [PlanTask("One", "D")])
    app.state.update(4, TaskStatus.PENDING)
    app.workspaces.changed.side_effect = [True, False]

    async def close_window(*args, **kwargs):
        app.config.schedule.allows.return_value = False
        return result()

    app.agents["worker"].execute.side_effect = close_window
    asyncio.run(app._guarded_process(issue, "worker"))
    assert app.state.rows()[0]["status"] == str(TaskStatus.HUMAN_REVIEW)
    app.workspaces.push.assert_awaited_once()


@pytest.mark.parametrize("planning", [False, True])
@pytest.mark.parametrize("status,failures,eligible", [
    (TaskStatus.PENDING, 0, True),
    (TaskStatus.FAILED, 1, True),
    (TaskStatus.FAILED, 2, False),
    (TaskStatus.BLOCKED, 2, False),
    (TaskStatus.DONE, 0, False),
    (TaskStatus.CLAIMED, 0, False),
])
def test_admission_prefilter_preserves_retry_budget(tmp_path, planning, status, failures, eligible):
    app = make_orchestrator(tmp_path)
    row = {"status": str(status), "failures": failures, "plan": None}
    assert app._eligible(row, planning=planning) is eligible


def test_serve_reopens_on_next_poll(tmp_path):
    app = make_orchestrator(tmp_path)
    app.config.poll_seconds = 60
    app.config.schedule = SimpleNamespace(allows=Mock(return_value=False))
    app.running = {}
    app.github.runnable_issues = AsyncMock(return_value=[])
    app.github.unassigned_issues = AsyncMock(return_value=[])

    class Wake:
        async def wait(self):
            if app.config.schedule.allows.return_value:
                raise asyncio.CancelledError
            app.config.schedule.allows.return_value = True

        def clear(self):
            pass

    app._wake = Wake()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.serve())
    app.github.runnable_issues.assert_awaited_once()
