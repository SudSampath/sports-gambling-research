from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pytest_bdd import given, scenarios, then

scenarios("../features/quality_workflow.feature")

WORKFLOW_PATH = Path(__file__).parent.parent.parent / ".github" / "workflows" / "quality.yml"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture
def workflow():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    # YAML parses the bare `on:` key as the boolean True in PyYAML's default
    # (YAML 1.1) resolver; load with a loader that keeps it as the string
    # "on" so trigger assertions can key off workflow["on"] directly.
    return yaml.safe_load(raw), raw


@given("the quality workflow file")
def the_workflow_file(workflow):
    return workflow


def _triggers(workflow):
    parsed, _ = workflow
    # PyYAML's default resolver treats the unquoted `on:` key as boolean True.
    return parsed.get("on", parsed.get(True))


def _jobs(workflow):
    parsed, _ = workflow
    return parsed["jobs"]


@then("it triggers on the pull_request event")
def triggers_on_pull_request(workflow):
    assert "pull_request" in _triggers(workflow)


@then("it triggers on pushes to the main branch")
def triggers_on_push_to_main(workflow):
    push = _triggers(workflow)["push"]
    assert push["branches"] == ["main"]


@then("it never triggers on pull_request_target")
def never_triggers_on_pull_request_target(workflow):
    assert "pull_request_target" not in _triggers(workflow)
    _, raw = workflow
    assert "pull_request_target" not in raw


@then("the top-level permissions are exactly contents: read")
def permissions_are_read_only(workflow):
    parsed, _ = workflow
    assert parsed["permissions"] == {"contents": "read"}


@then("every step's action reference is pinned to a 40-character commit SHA")
def actions_are_sha_pinned(workflow):
    checked_any = False
    for job in _jobs(workflow).values():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses is None:
                continue
            checked_any = True
            action, _, ref = uses.partition("@")
            assert ref, f"{action} is not pinned to anything"
            assert SHA_PATTERN.match(ref), f"{action}@{ref} is not a full 40-character commit SHA"
    assert checked_any, "no 'uses' steps were found to check"


@then("the checkout step sets persist-credentials to false")
def checkout_does_not_persist_credentials(workflow):
    for job in _jobs(workflow).values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert step.get("with", {}).get("persist-credentials") is False
                return
    pytest.fail("no actions/checkout step found")


def _run_commands(workflow) -> list[str]:
    commands = []
    for job in _jobs(workflow).values():
        for step in job.get("steps", []):
            if "run" in step:
                commands.append(step["run"].strip())
    return commands


@then("the BDD scenarios command runs before the full suite command")
def bdd_command_runs_before_full_suite(workflow):
    commands = _run_commands(workflow)
    bdd_index = next(i for i, c in enumerate(commands) if "pytest -m bdd" in c)
    full_index = next(i for i, c in enumerate(commands) if c == "python -m pytest -q")
    assert bdd_index < full_index


@then("the full suite command runs before the CLI startup command")
def full_suite_runs_before_cli(workflow):
    commands = _run_commands(workflow)
    full_index = next(i for i, c in enumerate(commands) if c == "python -m pytest -q")
    cli_index = next(i for i, c in enumerate(commands) if "sgr.cli --help" in c)
    assert full_index < cli_index


@then("the quality job has a timeout of at most 10 minutes")
def quality_job_has_bounded_timeout(workflow):
    for job in _jobs(workflow).values():
        assert 0 < job["timeout-minutes"] <= 10


@then("the workflow cancels in-progress runs for the same ref")
def cancel_in_progress_runs(workflow):
    parsed, _ = workflow
    assert parsed["concurrency"]["cancel-in-progress"] is True
    assert "${{ github.ref }}" in parsed["concurrency"]["group"]


@then("the quality job runs only on a standard GitHub-hosted runner")
def standard_runner_only(workflow):
    for job in _jobs(workflow).values():
        assert job["runs-on"] == "ubuntu-latest"


@then('the quality job is named "BDD and full suite"')
def quality_job_named(workflow):
    names = [job.get("name") for job in _jobs(workflow).values()]
    assert "BDD and full suite" in names


@then("PYTHONPATH is set to src for the quality job")
def pythonpath_set_for_cli_step(workflow):
    # The package is never pip-installed (only its dependencies are);
    # pytest's own pythonpath ini option covers the two pytest steps, but
    # "python -m sgr.cli" run directly needs this set explicitly, at the
    # job level or on its own step -- caught by an actual failed GitHub
    # Actions run before this assertion was added.
    for job in _jobs(workflow).values():
        if job.get("env", {}).get("PYTHONPATH") == "src":
            return
        for step in job.get("steps", []):
            if "sgr.cli" in step.get("run", "") and step.get("env", {}).get("PYTHONPATH") == "src":
                return
    pytest.fail("no job- or step-level PYTHONPATH=src found for the CLI step")
