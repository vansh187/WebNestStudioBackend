"""Automated CodeLab problem generation: verification, safety, resilience.

The LLM and persistence are in-memory fakes; the code runner is the real one,
so these tests execute the fake "model-written" solutions for real.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import codelab_generation_service as generation
from services.codelab_generation_service import DEFAULT_STARTER, CodelabGenerationService, _pick_topic
from services.codelab_sandbox import UnsafeCodeError, check_code_is_safe, normalize_output, run_python
from services.llm_provider import GenerationFailedError

GOOD_SOLUTION = "import sys\nnums = list(map(int, sys.stdin.read().split()))\nprint(sum(nums[1:]))\n"
OTHER_GOOD_SOLUTION = "n = int(input())\nprint(sum(int(x) for x in input().split()) if n else 0)\n"
WRONG_SOLUTION = "import sys\nnums = list(map(int, sys.stdin.read().split()))\nprint(max(nums[1:]))\n"
STARTER = "import sys\ndata = sys.stdin.read().split()\n# TODO\n"
STATEMENT = (
    "A shop records the value of every sale made in a day. Given the number of sales and the value of each one, "
    "print the total value of all sales.\n\n**Input format**\n\nThe first line has n. The second line has n integers."
    "\n\n**Output format**\n\nOne integer: the total."
)


def problem_reply(
    *, title="Daily Sales Total", solution=GOOD_SOLUTION, starter=STARTER, examples=None, test_inputs=None, statement=STATEMENT
) -> str:
    data = {
        "title": title,
        "constraints": ["1 <= n <= 100"],
        "hints": ["Add them up."],
        "topics": ["lists"],
        "examples": examples
        if examples is not None
        else [
            {"input": "3\n1 2 3", "expected_output": "6", "explanation": "1 + 2 + 3 = 6."},
            {"input": "2\n10 -4", "expected_output": "6", "explanation": "10 - 4 = 6."},
        ],
        "test_inputs": test_inputs
        if test_inputs is not None
        else ["1\n5", "4\n1 1 1 1", "2\n-3 -4", "3\n100 200 300", "5\n0 0 0 0 9", "2\n7 8"],
    }
    return (
        f"<statement>\n{statement}\n</statement>\n<problem_json>\n{json.dumps(data)}\n</problem_json>\n"
        f"<reference_solution>\n```python\n{solution}```\n</reference_solution>\n<starter_code>\n{starter}</starter_code>"
    )


def solver_reply(code: str = OTHER_GOOD_SOLUTION) -> str:
    return f"<solution>\n{code}</solution>"


class FakeLLM:
    """Scripted replies: `authors` for the problem-writing call, `solvers` for
    the independent-solution call. An Exception instance is raised instead."""

    def __init__(self, authors, solvers):
        self.authors, self.solvers = list(authors), list(solvers)
        self.solver_avoided: list[str] = []

    @property
    def author_calls(self) -> int:
        return self._author_calls

    _author_calls = 0

    async def generate_text(self, system_prompt, user_message):
        self._author_calls += 1
        reply = self.authors.pop(0) if len(self.authors) > 1 else self.authors[0]
        if isinstance(reply, Exception):
            raise reply
        return reply, "gemini"

    async def generate_text_avoiding(self, system_prompt, user_message, avoid):
        self.solver_avoided.append(avoid)
        assert "print(sum" not in user_message and "expected_output" not in user_message  # no leak of the reference
        reply = self.solvers.pop(0) if len(self.solvers) > 1 else self.solvers[0]
        if isinstance(reply, Exception):
            raise reply
        return reply, "groq"


class FakeCodelab:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created: list[tuple[dict, list[dict]]] = []

    async def list_all_problems(self):
        return list(self.existing)

    async def create_problem(self, fields, test_cases):
        self.created.append((fields, test_cases))
        return SimpleNamespace(id=None, slug=fields["slug"], title=fields["title"], test_cases=test_cases)


class FakeLogs:
    def __init__(self):
        self.rows: list[dict] = []
        self.latest: dict[str, datetime] = {}

    async def create(self, **fields):
        self.rows.append(fields)

    async def latest_success_at(self, difficulty):
        return self.latest.get(difficulty)

    async def attempt_counts_by_topic(self, difficulty):
        counts: dict[str, int] = {}
        for row in self.rows:
            if row["difficulty"] == difficulty and row["topic"]:
                counts[row["topic"]] = counts.get(row["topic"], 0) + 1
        return counts


class FakeHook:
    def __init__(self):
        self.reasons: list[str] = []

    async def trigger(self, reason):
        self.reasons.append(reason)
        return True


def make_service(authors, solvers=(solver_reply(),), existing=()):
    service = CodelabGenerationService.__new__(CodelabGenerationService)
    service._codelab = FakeCodelab(existing)
    service._logs = FakeLogs()
    service._settings = SimpleNamespace(codelab_solution_timeout_seconds=5.0, codelab_generation_interval_days=1)
    service._llm = FakeLLM(authors, solvers)
    service._deploy_hook = FakeHook()
    return service


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    monkeypatch.setattr(generation, "LLM_RETRY_PAUSE_SECONDS", 0)


# ---- The happy path ----


def test_verified_problem_is_published_with_computed_outputs():
    service = make_service([problem_reply()])

    results = asyncio.run(service.generate_set("manual-admin", ["easy"]))

    assert [(r.difficulty, r.success, r.slug) for r in results] == [("easy", True, "daily-sales-total")]
    fields, tests = service._codelab.created[0]
    assert fields["status"] == "published" and fields["difficulty"] == "easy" and fields["points"] == 20
    assert fields["track"] == "python" and fields["starter_files"][0]["content"].strip() == STARTER.strip()
    assert [t["expected_output"] for t in tests] == ["6", "6", "5", "4", "-7", "600", "9", "15"]
    assert [t["is_hidden"] for t in tests] == [False, False] + [True] * 6
    assert service._llm.solver_avoided == ["gemini"]  # second opinion asked of the other provider
    assert service._logs.rows[0]["success"] is True and service._logs.rows[0]["problem_slug"] == "daily-sales-total"
    assert service._deploy_hook.reasons == ["codelab problems published: daily-sales-total"]


def test_one_run_produces_easy_medium_and_hard():
    service = make_service(
        [problem_reply(title="Daily Sales Total"), problem_reply(title="Warehouse Crate Count"), problem_reply(title="Orchard Harvest Tally")]
    )
    # Distinct statements so the three drafts are not duplicates of each other.
    service._llm.authors = [
        problem_reply(title=title, statement=f"{topic} " * 30 + STATEMENT[-120:])
        for title, topic in [("Daily Sales Total", "sales"), ("Warehouse Crate Count", "crates"), ("Orchard Harvest Tally", "apples")]
    ]

    results = asyncio.run(service.generate_set("scheduled"))

    assert [(r.difficulty, r.success) for r in results] == [("easy", True), ("medium", True), ("hard", True)]
    assert [f["points"] for f, _ in service._codelab.created] == [20, 40, 80]
    assert len(service._deploy_hook.reasons) == 1  # one rebuild for the whole set


# ---- Verification rejects bad drafts ----


def test_wrong_reference_solution_is_caught_by_the_independent_solution():
    service = make_service([problem_reply(solution=WRONG_SOLUTION)])

    results = asyncio.run(service.generate_set("scheduled", ["easy"]))

    assert results[0].success is False
    assert "disagrees with the reference solution" in results[0].detail
    assert service._codelab.created == []
    assert service._logs.rows[-1]["success"] is False
    assert service._deploy_hook.reasons == []


def test_a_bad_draft_is_retried_and_the_next_good_one_is_published():
    service = make_service([problem_reply(solution=WRONG_SOLUTION), problem_reply()])

    results = asyncio.run(service.generate_set("scheduled", ["medium"]))

    assert results[0].success is True
    assert service._llm.author_calls == 2


@pytest.mark.parametrize(
    "solution, reason",
    [
        ("import os\nprint(os.getcwd())\n", "was not run"),
        ("print(open('/etc/passwd').read())\n", "was not run"),
        ("print(1 // 0)\n", "failed on test 1"),
        ("while True:\n    pass\n", "timed out"),
        ("def broken(:\n    pass\n", "was not run"),
    ],
)
def test_unsafe_crashing_or_hanging_solutions_are_rejected_not_raised(solution, reason):
    service = make_service([problem_reply(solution=solution)])
    service._settings.codelab_solution_timeout_seconds = 1.0
    generation_attempts = generation.MAX_ATTEMPTS_PER_PROBLEM

    results = asyncio.run(service.generate_set("scheduled", ["easy"]))

    assert results[0].success is False and reason in results[0].detail
    assert service._llm.author_calls == generation_attempts
    assert service._codelab.created == []


def test_non_deterministic_solution_is_rejected():
    flaky = "import sys, itertools\nprint(id(object()) % 7)\n"
    service = make_service([problem_reply(solution=flaky)], solvers=[solver_reply(flaky)])
    results = asyncio.run(service.generate_set("scheduled", ["easy"]))
    assert results[0].success is False


def test_duplicate_of_an_existing_problem_is_rejected():
    existing = SimpleNamespace(slug="daily-sales-total", title="Daily Sales Total", statement=STATEMENT, difficulty="easy", topics=["lists"])
    service = make_service([problem_reply()], existing=[existing])
    results = asyncio.run(service.generate_set("scheduled", ["easy"]))
    assert results[0].success is False and "already exists" in results[0].detail


def test_too_few_tests_is_rejected():
    service = make_service([problem_reply(test_inputs=["1\n5", "1\n6"])])
    results = asyncio.run(service.generate_set("scheduled", ["easy"]))
    assert results[0].success is False and "hidden test inputs" in results[0].detail


# ---- Self-healing ----


def test_miscalculated_example_is_replaced_by_the_verified_output():
    examples = [
        {"input": "3\n1 2 3", "expected_output": "7", "explanation": "Wrongly worked out."},
        {"input": "2\n10 -4", "expected_output": "6", "explanation": "10 - 4 = 6."},
    ]
    service = make_service([problem_reply(examples=examples)])

    asyncio.run(service.generate_set("scheduled", ["easy"]))

    fields, _ = service._codelab.created[0]
    assert [e["expected_output"] for e in fields["examples"]] == ["6", "6"]
    assert fields["examples"][0]["explanation"] is None  # written for the wrong answer
    assert fields["examples"][1]["explanation"] == "10 - 4 = 6."


@pytest.mark.parametrize("starter", [GOOD_SOLUTION, "import os\n", "print(1 // 0)\n", ""])
def test_starter_that_solves_crashes_or_is_unsafe_is_replaced(starter):
    service = make_service([problem_reply(starter=starter)])
    asyncio.run(service.generate_set("scheduled", ["easy"]))
    fields, _ = service._codelab.created[0]
    assert fields["starter_files"][0]["content"] == DEFAULT_STARTER


def test_default_starter_runs_without_error():
    result = asyncio.run(run_python(DEFAULT_STARTER, "3\n1 2 3"))
    assert result.ok and result.stdout == ""


# ---- Nothing escapes as an exception ----


def test_llm_outage_fails_that_difficulty_without_raising():
    service = make_service([GenerationFailedError("all providers down")])
    results = asyncio.run(service.generate_set("scheduled"))
    assert [r.success for r in results] == [False, False, False]
    assert len(service._logs.rows) == 3


def test_transient_llm_failure_is_retried():
    service = make_service([GenerationFailedError("overloaded"), problem_reply()])
    results = asyncio.run(service.generate_set("scheduled", ["easy"]))
    assert results[0].success is True


@pytest.mark.parametrize("reply", ["", "not the format at all", "<problem_json>[1, 2]</problem_json>", "<problem_json>{bad json</problem_json>"])
def test_malformed_model_reply_fails_cleanly(reply):
    service = make_service([reply])
    results = asyncio.run(service.generate_set("scheduled", ["hard"]))
    assert results[0].success is False


def test_unexpected_errors_are_contained_per_difficulty():
    service = make_service([problem_reply()])

    async def broken_create(fields, test_cases):
        raise RuntimeError("database exploded")

    async def broken_log(**fields):
        raise RuntimeError("log table missing")

    service._codelab.create_problem = broken_create
    service._logs.create = broken_log

    results = asyncio.run(service.generate_set("scheduled"))

    assert [r.success for r in results] == [False, False, False]


def test_due_difficulties_respects_the_interval():
    service = make_service([problem_reply()])
    now = datetime.now(timezone.utc)
    service._logs.latest = {"easy": now - timedelta(hours=3), "medium": now - timedelta(hours=23)}
    assert asyncio.run(service.due_difficulties()) == ["medium", "hard"]
    service._settings.codelab_generation_interval_days = 2
    assert asyncio.run(service.due_difficulties()) == ["hard"]


def test_topic_rotation_picks_the_least_attempted_topic():
    first, second, third = generation.TOPICS["easy"][:3]
    assert _pick_topic("easy", {}) == first
    assert _pick_topic("easy", {first: 1}) == second
    assert _pick_topic("easy", {first: 2, second: 2}) == third


def test_rotation_advances_past_a_topic_that_keeps_failing_and_ignores_model_labels():
    # Run 1: every draft fails verification. Run 2 must move to the next topic.
    service = make_service([problem_reply(solution=WRONG_SOLUTION)])
    asyncio.run(service.generate_set("scheduled", ["easy"]))
    asyncio.run(service.generate_set("scheduled", ["easy"]))
    first, second = generation.TOPICS["easy"][:2]
    assert [row["topic"] for row in service._logs.rows] == [first, second]

    # A success is recorded under the curriculum topic even though the model
    # labelled the problem differently ("lists"), and that topic leads the list.
    service._llm.authors = [problem_reply()]
    asyncio.run(service.generate_set("scheduled", ["easy"]))
    third = generation.TOPICS["easy"][2]
    assert service._logs.rows[-1]["topic"] == third and service._logs.rows[-1]["success"] is True
    assert service._codelab.created[0][0]["topics"][0] == third


def test_missing_topic_history_does_not_stop_generation():
    service = make_service([problem_reply()])

    async def broken(difficulty):
        raise RuntimeError("log table missing")

    service._logs.attempt_counts_by_topic = broken
    results = asyncio.run(service.generate_set("scheduled", ["easy"]))
    assert results[0].success is True


def test_latex_dollars_are_unwrapped_but_real_dollar_signs_survive():
    statement = (
        "A stall sells $N$ items and item $i+1$ costs more than item $i$. One mug costs $5 and a plate costs $10. "
        "Fields are separated by '$' and end with '$'. Print the total for the given basket of items in whole dollars."
    )
    service = make_service([problem_reply(statement=statement)])
    asyncio.run(service.generate_set("scheduled", ["easy"]))
    published = service._codelab.created[0][0]["statement"]
    assert "sells N items and item i+1 costs more than item i." in published
    assert "costs $5 and a plate costs $10." in published
    assert "separated by '$' and end with '$'." in published


# ---- The runner ----


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import subprocess",
        "from sys import modules",
        "import sys\nsys.modules['os']",
        "open('x')",
        "eval('1')",
        "exec('x = 1')",
        "__import__('os')",
        "().__class__.__bases__",
        "getattr(1, 'real')",
        "import re\nre.enum.sys.modules",
        "import operator",
        "from operator import attrgetter",
        "import string\nstring.Formatter().get_field('0.real', (1,), {})",
        "from string import Formatter",
        "print('{0.__globals__}'.format(len))",
        "import functools\nfunctools.attrgetter",
        "x = 1\x00",
        "(" * 5000,
    ],
)
def test_unsafe_code_is_never_run(code):
    with pytest.raises(UnsafeCodeError):
        check_code_is_safe(code)
    with pytest.raises(UnsafeCodeError):
        asyncio.run(run_python(code, ""))


def test_runner_reports_failures_as_results():
    assert asyncio.run(run_python("print(int(input()) * 2)", "21")).stdout.strip() == "42"
    assert asyncio.run(run_python("raise ValueError('boom')", "")).ok is False
    assert asyncio.run(run_python("while True: pass", "", timeout_seconds=1)).error.startswith("timed out")
    assert asyncio.run(run_python("print('x' * 100000)", "")).error == "output is too large"
    # An endless print loop is killed as soon as it passes the cap, long
    # before the time limit, and its output is never accumulated.
    started = time.monotonic()
    flood = asyncio.run(run_python("while True:\n    print('x' * 1000)", "", timeout_seconds=30))
    assert flood.error == "output is too large" and flood.stdout == "" and time.monotonic() - started < 10
    assert asyncio.run(run_python("print(1)", "x" * 60_000)).error == "input is too large"
    assert asyncio.run(run_python("print('ignores its input')", "1 2 3\n" * 500)).ok is True
    main_guard = "def main():\n    print('ok')\n\nif __name__ == '__main__':\n    main()\n"
    assert asyncio.run(run_python(main_guard, "")).stdout.strip() == "ok"


def test_runner_does_not_expose_the_server_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    result = asyncio.run(run_python("import sys\nprint(sorted(sys.stdin.read().split()))", "b a"))
    assert result.ok and "secret-value" not in result.stdout


def test_output_normalisation():
    assert normalize_output("a  \r\nb\t\n\n\n") == "a\nb"
