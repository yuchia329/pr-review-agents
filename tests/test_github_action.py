"""The GitHub Action's wrapper: what one run does for a pull request, a fork, and a push, and
what it asks of GitHub.

One test runs a fork's code in a real Sandbox, and so needs a container runtime, as the tests
of the Sandbox do.
"""

import ast
import base64
import json
import re
import threading
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import (
    Collector,
    FixtureRepository,
    GathersNothing,
    UnreachableModel,
    commit,
    exported_spans,
    git,
    model_listing,
    results,
)
from hypothesis import given
from hypothesis import strategies as st

import pr_verification
from pr_verification.comment import MARKER, render_comment
from pr_verification.decision import Decider, ScriptedDecider
from pr_verification.ecosystem import Unsupported
from pr_verification.github_action import (
    CHECK_RUN_NAME,
    EXIT_DONE,
    EXIT_FAILED,
    FORK_SKIPPED,
    NO_REPORT_CHECK_RUN,
    SAME_REPOSITORY_SKIPPED,
    STATE_VARIABLE,
    CheckRun,
    Conclusion,
    GitHubAPI,
    GitHubFailed,
    gate_check_run,
    main,
)
from pr_verification.model import Model, ScriptedModel
from pr_verification.orchestration import FixedPipeline, Gathered, Orchestration
from pr_verification.repo_profile import CommandFailed, DependencyCache, DirectoryStore, RepoProfile
from pr_verification.report import (
    AcceptedReproduction,
    BudgetSpend,
    Claim,
    EvidenceSource,
    Gate,
    GateDecision,
    Report,
    Verdict,
)
from pr_verification.sandbox import (
    ContainerSandbox,
    Limits,
    Phase,
    RunResult,
    SandboxFailed,
)
from pr_verification.tracing import ENDPOINT_VARIABLE

TOKEN = "ghs_zqSecretToken"
REPOSITORY = "octo/shop"
CLAIM = "Fixes the crash on empty input"
ONE_INCONCLUSIVE = "Claims: 0 Confirmed, 0 Refuted, 1 Inconclusive."


# The check run


REPORTED_AS = {
    GateDecision.PASS: (Conclusion.SUCCESS, "Gate: pass"),
    GateDecision.BLOCK: (Conclusion.FAILURE, "Gate: block"),
}


@given(decision=st.sampled_from(GateDecision), note=st.text())
def test_check_run_reports_any_gate_by_its_decision_with_its_note_as_the_summary(
    decision: GateDecision, note: str
) -> None:
    conclusion, title = REPORTED_AS[decision]

    assert gate_check_run(Gate(decision, note)) == CheckRun(conclusion, title, note)


# GitHub's own API


class Answers:
    """A stand-in for GitHub's API that gives the prepared answers in order and records each
    request."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self) -> GitHubAPI:
        return GitHubAPI(
            REPOSITORY,
            TOKEN,
            api_url="https://github.example/api/v3",
            graphql_url="https://github.example/api/graphql",
            transport=self.transport,
        )

    def asked(self) -> list[tuple[str, str]]:
        return [(request.method, str(request.url)) for request in self.requests]


def closing(*issues: Mapping[str, object]) -> httpx.Response:
    """GitHub's answer to which issues a pull request closes."""
    references = {"closingIssuesReferences": {"nodes": list(issues)}}
    return httpx.Response(200, json={"data": {"repository": {"pullRequest": references}}})


def comments(*listed: Mapping[str, object]) -> httpx.Response:
    return httpx.Response(200, json=list(listed))


def a_comment(number: int, body: str, kind: str = "Bot") -> Mapping[str, object]:
    return {"id": number, "body": body, "user": {"login": "someone", "type": kind}}


COMMENTS_OF_7 = "https://github.example/api/v3/repos/octo/shop/issues/7/comments"
BODY = MARKER + "\n<h2>PR Verification</h2>\n"


def test_linked_issue_is_the_title_and_body_of_each_issue_the_pull_request_closes() -> None:
    github = Answers(
        closing(
            {"title": "Upload gives up", "body": "After one timeout.\nSee the log."},
            {"title": "No body here", "body": None},
            {"title": "Crash on empty input", "body": "A traceback."},
        )
    )

    linked = github.client().linked_issue(7)

    assert linked == (
        "Upload gives up\n\nAfter one timeout.\nSee the log.\n\n"
        "No body here\n\n\n\n"
        "Crash on empty input\n\nA traceback."
    )
    (request,) = github.requests
    assert (request.method, str(request.url)) == ("POST", "https://github.example/api/graphql")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "closingIssuesReferences" in request.content.decode()


def test_linked_issue_is_empty_when_the_pull_request_closes_no_issue() -> None:
    assert Answers(closing()).client().linked_issue(7) == ""


def test_comment_is_added_when_the_system_has_none_on_the_pull_request() -> None:
    github = Answers(
        comments(
            a_comment(11, "Looks good to me."),
            a_comment(12, MARKER + "\nI wrote this by hand.", kind="User"),
        ),
        httpx.Response(201, json={"id": 13}),
    )

    github.client().post_comment(7, BODY)

    assert github.asked() == [("GET", COMMENTS_OF_7 + "?per_page=100"), ("POST", COMMENTS_OF_7)]
    assert json.loads(github.requests[1].content) == {"body": BODY}
    assert all(request.headers["Authorization"] == f"Bearer {TOKEN}" for request in github.requests)


def test_comment_the_system_posted_before_is_replaced_and_no_other_is_touched() -> None:
    github = Answers(
        comments(
            a_comment(11, MARKER + "\nI wrote this by hand.", kind="User"),
            a_comment(12, "Thanks!"),
            a_comment(13, MARKER + "\nAn earlier report."),
            a_comment(14, MARKER + "\nA later report."),
        ),
        httpx.Response(200, json={"id": 13}),
    )

    github.client().post_comment(7, BODY)

    assert github.asked() == [
        ("GET", COMMENTS_OF_7 + "?per_page=100"),
        ("PATCH", "https://github.example/api/v3/repos/octo/shop/issues/comments/13"),
    ]
    assert json.loads(github.requests[1].content) == {"body": BODY}


def test_check_run_is_reported_on_the_commit_as_one_that_has_ended() -> None:
    github = Answers(httpx.Response(201, json={"id": 1}))
    check_run = CheckRun(Conclusion.FAILURE, "Gate: block", "Claims: 0 Confirmed, 1 Refuted.")

    github.client().set_check_run("a" * 40, check_run)

    (request,) = github.requests
    assert (request.method, str(request.url)) == (
        "POST",
        "https://github.example/api/v3/repos/octo/shop/check-runs",
    )
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "name": CHECK_RUN_NAME,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "failure",
        "output": {"title": "Gate: block", "summary": "Claims: 0 Confirmed, 1 Refuted."},
    }


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError(f"connection refused for {TOKEN}"),
        httpx.Response(401, json={"message": f"Bad credentials: {TOKEN}"}),
        httpx.Response(403, text=f"Resource not accessible\nby integration {TOKEN}"),
        httpx.Response(500, text="<html>Unicorn</html>"),
        httpx.Response(200, text="<html>Not what was asked for</html>"),
        httpx.Response(200, json={"data": {"repository": None}}),
    ],
)
def test_request_github_does_not_do_is_a_failure_whose_one_line_never_holds_the_token(
    answer: httpx.Response | Exception,
) -> None:
    with pytest.raises(GitHubFailed) as issue_failed:
        Answers(answer).client().linked_issue(7)
    with pytest.raises(GitHubFailed) as comment_failed:
        Answers(answer).client().post_comment(7, BODY)

    for failure in (issue_failed, comment_failed):
        assert TOKEN not in str(failure.value)
        assert len(str(failure.value).splitlines()) == 1


def test_check_run_github_refuses_is_a_failure() -> None:
    github = Answers(httpx.Response(403, json={"message": "Resource not accessible"}))

    with pytest.raises(GitHubFailed):
        github.client().set_check_run("a" * 40, NO_REPORT_CHECK_RUN)


# One run of the Action


class RecordingGitHub:
    """A stand-in for GitHub that records what it is asked, and fails where it is told to."""

    def __init__(self, linked_issue: str = "", failing: Collection[str] = ()) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._linked_issue, self._failing = linked_issue, failing

    def _called(self, name: str, *given: object) -> None:
        self.calls.append((name, *given))
        if name in self._failing:
            raise GitHubFailed(f"GitHub answered {name} with HTTP 502")

    def linked_issue(self, pull_request: int) -> str:
        self._called("linked_issue", pull_request)
        return self._linked_issue

    def post_comment(self, pull_request: int, body: str) -> None:
        self._called("post_comment", pull_request, body)

    def set_check_run(self, commit: str, check_run: CheckRun) -> None:
        self._called("set_check_run", commit, check_run)

    def asked(self) -> list[object]:
        return [name for name, *_ in self.calls]


SAME_REPOSITORY: Mapping[str, object] = {"full_name": REPOSITORY}
FORK: Mapping[str, object] = {"full_name": "someone/shop"}
ABSENT = "the event's `head` has no `repo` at all"


def pull_request_event(
    repository: FixtureRepository,
    *,
    body: str | None = "Also tidies the greeting.",
    head_repo: object = SAME_REPOSITORY,
) -> dict[str, object]:
    """The event GitHub gives a run for a pull request numbered 7."""
    head = {"sha": repository.after, "repo": head_repo}
    if head_repo is ABSENT:
        del head["repo"]
    return {
        "action": "synchronize",
        "pull_request": {
            "number": 7,
            "title": "Fix the crash on empty input",
            "body": body,
            "head": head,
            "base": {"sha": repository.before, "repo": SAME_REPOSITORY},
        },
    }


def environment_for(
    tmp_path: Path,
    repository: FixtureRepository,
    event: object,
    *,
    name: str = "pull_request",
    **more: str,
) -> dict[str, str]:
    """What the Action's run finds in its environment for `event`."""
    path = tmp_path / "event.json"
    path.write_text(event if isinstance(event, str) else json.dumps(event))
    return {
        "GITHUB_EVENT_NAME": name,
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_WORKSPACE": str(repository.path),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_SHA": repository.before,
        "GITHUB_TOKEN": TOKEN,
        **more,
    }


STOP_LINE = re.compile("^::stop-commands::([0-9a-fA-F]{32})$", flags=re.MULTILINE)


def between_the_stops(out: str) -> str:
    """What a run printed between the line that stops GitHub's runner acting on commands and
    the line that ends the stop, each of which it printed once."""
    lines = out.splitlines()
    (token,) = STOP_LINE.findall(out)
    stop, end = lines.index(f"::stop-commands::{token}"), lines.index(f"::{token}::")
    assert lines.count(f"::{token}::") == 1 and stop < end
    return "\n".join(lines[stop + 1 : end])


def report_printed(printed: Mapping[str, object]) -> Report:
    """The report as printed, for a pull request whose Claims have no Evidence."""
    claims, spend = printed["claims"], printed["budget_spend"]
    assert isinstance(claims, list) and isinstance(spend, dict)
    return Report(
        before=str(printed["before"]),
        after=str(printed["after"]),
        claims=tuple(
            Claim(claim["text"], Verdict(claim["verdict"]), claim["reason"]) for claim in claims
        ),
        findings=(),
        budget_spend=BudgetSpend(spend["seconds"], spend["dollars"], spend["steps"]),
    )


def verify(
    environment: Mapping[str, str],
    github: RecordingGitHub,
    model: Model | None = None,
    orchestration: Orchestration | None = None,
) -> int:
    """One run of the Action for a pull request that makes one Claim."""
    return main(
        environment,
        github=github,
        model=model_listing(CLAIM) if model is None else model,
        decider=ScriptedDecider(["behavioural"]),
        orchestration=GathersNothing() if orchestration is None else orchestration,
    )


@pytest.mark.parametrize(
    ("body", "description"),
    [("Also tidies the greeting.", "Also tidies the greeting."), (None, "")],
)
def test_pull_request_is_verified_its_report_printed_its_gate_reported_and_its_comment_posted(
    body: str | None,
    description: str,
    fixture_repository: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github = RecordingGitHub(linked_issue="Upload gives up\n\nAfter one timeout.")
    model = model_listing(CLAIM)
    event = pull_request_event(fixture_repository, body=body)
    environment = environment_for(
        tmp_path, fixture_repository, event, **{STATE_VARIABLE: str(tmp_path / "state")}
    )

    code = verify(environment, github, model)

    assert code == EXIT_DONE
    printed = json.loads(between_the_stops(capsys.readouterr().out))
    assert (printed["before"], printed["after"]) == (
        fixture_repository.before,
        fixture_repository.after,
    )
    assert [(claim["text"], claim["verdict"]) for claim in printed["claims"]] == [
        (CLAIM, "inconclusive")
    ]
    assert json.loads(model.requests[0].data) == {
        "title": "Fix the crash on empty input",
        "description": description,
        "linked_issue": "Upload gives up\n\nAfter one timeout.",
    }
    assert github.calls == [
        ("linked_issue", 7),
        (
            "set_check_run",
            fixture_repository.after,
            CheckRun(Conclusion.SUCCESS, "Gate: pass", ONE_INCONCLUSIVE),
        ),
        ("post_comment", 7, render_comment(report_printed(printed))),
    ]


REGRESSION = AcceptedReproduction(
    source=EvidenceSource.RAN_CODE,
    observation="before: exit status 0; after: exit status 1",
    command=("run-reproduction",),
    before=RunResult("1 passed", exit_status=0, limit_hit=None),
    after=RunResult("1 failed", exit_status=1, limit_hit=None),
    path="reproduction.txt",
    reproduction="assert parse('') == []",
    before_results=results(passed="t"),
    after_results=results(failed="t"),
)


class GathersARegression:
    """A stand-in orchestration whose Evidence refutes every Claim."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        return tuple(Gathered((REGRESSION,)) for _ in claims)


def test_gate_that_blocks_is_a_check_run_concluded_as_failure_and_the_run_still_ends_as_done(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = RecordingGitHub()
    event = pull_request_event(fixture_repository)

    code = verify(
        environment_for(tmp_path, fixture_repository, event), github, None, GathersARegression()
    )

    assert code == EXIT_DONE
    printed = json.loads(between_the_stops(capsys.readouterr().out))
    assert printed["gate"]["decision"] == "block"
    assert github.calls[1] == (
        "set_check_run",
        fixture_repository.after,
        CheckRun(Conclusion.FAILURE, "Gate: block", printed["gate"]["note"]),
    )
    assert github.asked() == ["linked_issue", "set_check_run", "post_comment"]


def test_pull_request_is_compared_with_the_commit_of_the_target_branch_it_starts_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "moved-on"
    path.mkdir()
    git(path, "init", "--quiet")
    (path / "greeting.py").write_text("GREETING = 'hello'\n")
    started_from = commit(path, "Add greeting")
    git(path, "checkout", "--quiet", "-b", "change")
    (path / "greeting.py").write_text("GREETING = 'hello there'\n")
    head = commit(path, "Tidy greeting")
    git(path, "checkout", "--quiet", "-")
    (path / "farewell.py").write_text("FAREWELL = 'bye'\n")
    base = commit(path, "Move the target branch on")
    repository = FixtureRepository(path=path, before=base, after=head)
    github = RecordingGitHub()

    verify(environment_for(tmp_path, repository, pull_request_event(repository)), github)

    printed = json.loads(between_the_stops(capsys.readouterr().out))
    assert (printed["before"], printed["after"]) == (started_from, head)
    assert github.calls[1][:2] == ("set_check_run", head)


@pytest.mark.parametrize(
    "head_repo",
    [FORK, None, ABSENT],
    ids=["another repository", "a repository that is gone", "no repository named"],
)
def test_pull_request_from_a_fork_is_skipped_by_a_run_for_pull_request_and_nothing_else_is_done(
    head_repo: object,
    fixture_repository: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github, model = RecordingGitHub(), ScriptedModel([])
    event = pull_request_event(fixture_repository, head_repo=head_repo)
    environment = environment_for(tmp_path, fixture_repository, event)
    del environment["GITHUB_TOKEN"]

    code = main(environment, github=github, model=model)
    with_neither_a_model_nor_a_token = main(environment)

    notice = f"::notice::{FORK_SKIPPED}\n"
    assert (code, with_neither_a_model_nor_a_token) == (EXIT_DONE, EXIT_DONE)
    assert capsys.readouterr().out == notice * 2
    assert github.calls == []
    assert model.requests == []


# A pull request from a fork, in a run for `pull_request_target`


def checkout_of_the_default_branch(
    on_github: FixtureRepository, tmp_path: Path
) -> FixtureRepository:
    """The checkout a run for `pull_request_target` starts with: the default branch, with the
    repository on GitHub as its `origin`. The pull request's commit is on GitHub, under no
    branch, and is not in the checkout."""
    origin, path = on_github.path, tmp_path / "checkout"
    git(origin, "update-ref", "refs/pull/7/head", on_github.after)
    git(origin, "reset", "--quiet", "--hard", on_github.before)
    # Not a local clone, which would take every object `origin` holds, the commit among them.
    git(tmp_path, "clone", "--quiet", "--no-local", str(origin), str(path))
    assert on_github.after not in git(path, "rev-list", "--all")
    return FixtureRepository(path, on_github.before, on_github.after)


@pytest.fixture
def default_checkout(fixture_repository: FixtureRepository, tmp_path: Path) -> FixtureRepository:
    return checkout_of_the_default_branch(fixture_repository, tmp_path)


def test_fork_is_verified_in_a_run_for_pull_request_target_and_its_files_are_never_checked_out(
    default_checkout: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = RecordingGitHub()
    event = pull_request_event(default_checkout, head_repo=FORK)
    environment = environment_for(tmp_path, default_checkout, event, name="pull_request_target")

    code = verify(environment, github)

    assert code == EXIT_DONE
    printed = json.loads(between_the_stops(capsys.readouterr().out))
    assert (printed["before"], printed["after"]) == (
        default_checkout.before,
        default_checkout.after,
    )
    assert github.calls == [
        ("linked_issue", 7),
        (
            "set_check_run",
            default_checkout.after,
            CheckRun(Conclusion.SUCCESS, "Gate: pass", ONE_INCONCLUSIVE),
        ),
        ("post_comment", 7, render_comment(report_printed(printed))),
    ]
    assert git(default_checkout.path, "rev-parse", "HEAD") == default_checkout.before
    assert git(default_checkout.path, "status", "--porcelain") == ""
    assert '"hello"' in (default_checkout.path / "greeting.py").read_text()


KEY = "key-kept-outside-the-sandbox"
# Prints all that code in a pull request can find in its environment.
READS_ITS_ENVIRONMENT = """\
import os
print("NAMES", sorted(os.environ))
print("VALUES", sorted(os.environ.values()))
"""


class RunsThePullRequest:
    """A stand-in orchestration that runs the pull request's own `greeting.py` in a real
    Sandbox, and gives what it printed as the Evidence for every Claim."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        limits = Limits(seconds=60.0, memory_bytes=512 * 1024 * 1024, processors=1.0)
        with ContainerSandbox(repository, "python:3.14-slim") as sandbox:
            ran = sandbox.run(
                ("python", "greeting.py"), commit=after, phase=Phase.TEST, limits=limits
            )
        return tuple(Gathered((replace(REGRESSION, after=ran),)) for _ in claims)


def test_code_of_a_fork_is_run_where_it_finds_neither_the_key_nor_the_token(
    fixture_repository: FixtureRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (fixture_repository.path / "greeting.py").write_text(READS_ITS_ENVIRONMENT)
    hostile = commit(fixture_repository.path, "Tidy greeting")
    on_github = FixtureRepository(fixture_repository.path, fixture_repository.before, hostile)
    checkout = checkout_of_the_default_branch(on_github, tmp_path)
    event = pull_request_event(checkout, head_repo=FORK)
    secrets = {"PR_VERIFICATION_API_KEY": KEY, "GITHUB_TOKEN": TOKEN}
    for name, secret in secrets.items():
        monkeypatch.setenv(name, secret)
    environment = environment_for(tmp_path, checkout, event, name="pull_request_target", **secrets)

    code = verify(environment, RecordingGitHub(), None, RunsThePullRequest())

    assert code == EXIT_DONE
    out = capsys.readouterr().out
    (reproduction,) = json.loads(between_the_stops(out))["claims"][0]["evidence"]
    found = reproduction["after"]["output"]
    assert "NAMES" in found and "VALUES" in found, "the fork's code ran"
    assert not any(name in found for name in secrets)
    assert not any(secret in out for secret in secrets.values())
    assert "NAMES" not in (checkout.path / "greeting.py").read_text()


def test_pull_request_from_the_repository_itself_is_skipped_by_a_run_for_pull_request_target(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github, model = RecordingGitHub(), ScriptedModel([])
    event = pull_request_event(fixture_repository)
    environment = environment_for(tmp_path, fixture_repository, event, name="pull_request_target")
    del environment["GITHUB_TOKEN"]

    code = main(environment, github=github, model=model)
    with_neither_a_model_nor_a_token = main(environment)

    assert (code, with_neither_a_model_nor_a_token) == (EXIT_DONE, EXIT_DONE)
    assert capsys.readouterr().out == f"::notice::{SAME_REPOSITORY_SKIPPED}\n" * 2
    assert github.calls == []
    assert model.requests == []


def test_fork_whose_commit_cannot_be_fetched_is_a_failure_reported_as_a_neutral_check_run(
    default_checkout: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github, model = RecordingGitHub(), model_listing(CLAIM)
    event = pull_request_event(default_checkout, head_repo=FORK)
    environment = environment_for(tmp_path, default_checkout, event, name="pull_request_target")
    git(default_checkout.path, "remote", "set-url", "origin", str(tmp_path / "gone"))

    code = verify(environment, github, model)

    assert code == EXIT_FAILED
    assert github.calls == [("set_check_run", default_checkout.after, NO_REPORT_CHECK_RUN)]
    assert model.requests == []
    printed = between_the_stops(capsys.readouterr().out)
    assert printed.startswith("pr-verification: ")
    assert len(printed.splitlines()) == 1


@dataclass
class GitHost:
    """A stand-in for a server of git over HTTP that listens on this machine and hands nothing
    over."""

    url: str
    authorizations: list[str | None] = field(default_factory=list)
    """The `Authorization` header of each request it was sent, in order."""


@pytest.fixture
def git_host() -> Iterator[GitHost]:
    authorizations: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            authorizations.append(self.headers.get("Authorization"))
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield GitHost(f"http://127.0.0.1:{server.server_port}", authorizations)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("origin_is_github", [True, False])
def test_fork_commit_is_asked_for_with_the_token_and_only_of_githubs_own_server(
    origin_is_github: bool,
    default_checkout: FixtureRepository,
    git_host: GitHost,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = pull_request_event(default_checkout, head_repo=FORK)
    environment = environment_for(tmp_path, default_checkout, event, name="pull_request_target")
    if origin_is_github:
        environment["GITHUB_SERVER_URL"] = git_host.url
    git(default_checkout.path, "remote", "set-url", "origin", f"{git_host.url}/{REPOSITORY}")

    code = verify(environment, RecordingGitHub())

    assert code == EXIT_FAILED, "the stand-in hands nothing over"
    assert git_host.authorizations
    for authorization in git_host.authorizations:
        if origin_is_github:
            assert authorization is not None
            scheme, _, credentials = authorization.partition(" ")
            assert scheme.lower() == "basic"
            assert base64.b64decode(credentials).decode() == f"x-access-token:{TOKEN}"
        else:
            assert authorization is None
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert not any(sent.split()[-1] in out for sent in git_host.authorizations if sent)


@pytest.mark.parametrize(
    ("name", "head_repo", "named"),
    [
        ("pull_request_target", FORK, "refs/heads/main:refs/heads/moved"),
        ("pull_request_target", FORK, "--upload-pack=false"),
        ("pull_request", SAME_REPOSITORY, "HEAD"),
    ],
)
def test_event_that_names_its_commit_by_anything_but_a_full_id_cannot_be_read(
    name: str,
    head_repo: object,
    named: str,
    default_checkout: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github, model = RecordingGitHub(), model_listing(CLAIM)
    event = pull_request_event(
        FixtureRepository(default_checkout.path, default_checkout.before, named),
        head_repo=head_repo,
    )
    refs = git(default_checkout.path, "show-ref")

    code = verify(environment_for(tmp_path, default_checkout, event, name=name), github, model)

    assert code == EXIT_FAILED
    assert github.calls == []
    assert model.requests == []
    assert git(default_checkout.path, "show-ref") == refs
    printed = between_the_stops(capsys.readouterr().out)
    assert printed.startswith("pr-verification: ")
    assert len(printed.splitlines()) == 1


@pytest.mark.parametrize("failing", ["set_check_run", "post_comment"])
def test_check_run_and_comment_are_each_attempted_whatever_came_of_the_other(
    failing: str,
    fixture_repository: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github = RecordingGitHub(failing={failing})
    event = pull_request_event(fixture_repository)

    code = verify(environment_for(tmp_path, fixture_repository, event), github)

    assert code == EXIT_FAILED
    assert github.asked() == ["linked_issue", "set_check_run", "post_comment"]
    lines = between_the_stops(capsys.readouterr().out).splitlines()
    assert lines[-1].startswith("pr-verification: ")
    assert json.loads("\n".join(lines[:-1]))["gate"]["decision"] == "pass"


class Fault(Exception):
    """An error that nothing expects: a fault in the system."""


class Faulty:
    """A stand-in orchestration with a fault in it."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        raise Fault("the orchestration has a fault")


def neutral_check_runs(github: RecordingGitHub) -> list[tuple[object, ...]]:
    return [call for call in github.calls if call[0] == "set_check_run"]


@pytest.mark.parametrize(
    ("failing", "model", "decider", "configured"),
    [
        pytest.param((), UnreachableModel(), ScriptedDecider([]), {}, id="the model fails"),
        pytest.param(
            ("linked_issue",), ScriptedModel([]), ScriptedDecider([]), {}, id="GitHub fails"
        ),
        pytest.param((), None, ScriptedDecider([]), {}, id="no model is configured"),
        pytest.param(
            (),
            ScriptedModel([]),
            None,
            {"PR_VERIFICATION_DECISION": "gpt"},
            id="the decision layer cannot be had as configured",
        ),
    ],
)
def test_run_with_no_report_posts_no_comment_reports_a_neutral_check_run_and_prints_one_line(
    failing: Collection[str],
    model: Model | None,
    decider: Decider | None,
    configured: Mapping[str, str],
    fixture_repository: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github = RecordingGitHub(failing=failing)
    event = pull_request_event(fixture_repository)
    environment = environment_for(tmp_path, fixture_repository, event, **configured)

    code = main(
        environment, github=github, model=model, decider=decider, orchestration=GathersNothing()
    )

    assert code == EXIT_FAILED
    assert "post_comment" not in github.asked()
    assert neutral_check_runs(github) == [
        ("set_check_run", fixture_repository.after, NO_REPORT_CHECK_RUN)
    ]
    printed = between_the_stops(capsys.readouterr().out)
    assert printed.startswith("pr-verification: ")
    assert len(printed.splitlines()) == 1


def test_fault_in_the_system_is_printed_as_its_traceback_and_reported_as_a_neutral_check_run(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = RecordingGitHub()
    event = pull_request_event(fixture_repository)

    code = verify(environment_for(tmp_path, fixture_repository, event), github, None, Faulty())

    assert code == EXIT_FAILED
    assert "post_comment" not in github.asked()
    assert neutral_check_runs(github) == [
        ("set_check_run", fixture_repository.after, NO_REPORT_CHECK_RUN)
    ]
    printed = between_the_stops(capsys.readouterr().out)
    assert "Traceback" in printed and "the orchestration has a fault" in printed


def test_neutral_check_run_that_github_refuses_is_one_more_printed_line(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = RecordingGitHub(failing={"set_check_run"})
    event = pull_request_event(fixture_repository)

    code = verify(environment_for(tmp_path, fixture_repository, event), github, UnreachableModel())

    assert code == EXIT_FAILED
    assert github.asked() == ["linked_issue", "set_check_run"]
    lines = between_the_stops(capsys.readouterr().out).splitlines()
    assert len(lines) == 2
    assert all(line.startswith("pr-verification: ") for line in lines)


@pytest.mark.parametrize("event", [None, "this is not JSON", "[]", "{}"])
def test_event_that_cannot_be_read_is_a_failure_of_one_line_and_nothing_is_asked_of_anyone(
    event: str | None,
    fixture_repository: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github, model = RecordingGitHub(), model_listing(CLAIM)
    environment = environment_for(tmp_path, fixture_repository, event or "")
    if event is None:
        Path(environment["GITHUB_EVENT_PATH"]).unlink()

    code = verify(environment, github, model)

    assert code == EXIT_FAILED
    assert github.calls == []
    assert model.requests == []
    printed = between_the_stops(capsys.readouterr().out)
    assert printed.startswith("pr-verification: ")
    assert len(printed.splitlines()) == 1


def test_event_that_is_neither_a_pull_request_nor_a_push_is_a_failure_and_nothing_is_done(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    github, model = RecordingGitHub(), model_listing(CLAIM)
    event = pull_request_event(fixture_repository)
    environment = environment_for(tmp_path, fixture_repository, event, name="issue_comment")

    code = verify(environment, github, model)

    assert code == EXIT_FAILED
    assert github.calls == []
    assert model.requests == []


@pytest.mark.parametrize("inside", [".", "kept", "docs/../kept/state"])
def test_state_kept_where_a_pull_request_could_write_is_refused_before_anything_is_done(
    inside: str, fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    github, model = RecordingGitHub(), model_listing(CLAIM)
    (fixture_repository.path / "docs").mkdir()
    environment = environment_for(
        tmp_path,
        fixture_repository,
        pull_request_event(fixture_repository),
        **{STATE_VARIABLE: str(fixture_repository.path / inside)},
    )

    code = verify(environment, github, model)

    assert code == EXIT_FAILED
    assert github.calls == []
    assert model.requests == []


def test_untrusted_content_is_printed_only_where_the_runner_acts_on_nothing(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = environment_for(
        tmp_path, fixture_repository, pull_request_event(fixture_repository)
    )
    command = "::error::The Gate blocks"

    verify(environment, RecordingGitHub(), model_listing(command))
    first = capsys.readouterr().out
    verify(environment, RecordingGitHub(), model_listing(command))
    second = capsys.readouterr().out

    assert first.count(command) == between_the_stops(first).count(command) > 0
    assert STOP_LINE.findall(first) != STOP_LINE.findall(second), "each run chooses its token"


# A push to the target branch


@dataclass(frozen=True)
class Prepares(FixedPipeline):
    """A fixed pipeline that records each commit it is asked to prepare, and prepares none."""

    answer: RepoProfile | Unsupported | CommandFailed | Exception = Unsupported("Not found.")
    asked: list[tuple[Path, str]] = field(default_factory=list)

    def prepare_target_commit(
        self, repository: Path, target: str
    ) -> RepoProfile | Unsupported | CommandFailed:
        self.asked.append((repository.resolve(), target))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def prepares(
    tmp_path: Path, answer: RepoProfile | Unsupported | CommandFailed | Exception
) -> Prepares:
    return Prepares(
        DirectoryStore(tmp_path / "profiles"), DependencyCache(tmp_path / "homes"), answer=answer
    )


@pytest.mark.parametrize(
    "answer",
    [
        RepoProfile("python:3.12", (("pip", "install", "."),), ("pytest",), "pytest", (), (), ""),
        Unsupported("Nothing here says how the code is tested."),
        CommandFailed(("pip", "install", "."), RunResult("no network", 1, None)),
    ],
)
def test_push_prepares_the_commit_pushed_and_asks_nothing_of_a_model_or_of_github(
    answer: RepoProfile | Unsupported | CommandFailed,
    fixture_repository: FixtureRepository,
    tmp_path: Path,
) -> None:
    github = RecordingGitHub()
    pipeline = prepares(tmp_path, answer)
    environment = environment_for(tmp_path, fixture_repository, {"ref": "main"}, name="push")
    environment["GITHUB_SHA"] = fixture_repository.after
    del environment["GITHUB_TOKEN"]

    code = main(environment, github=github, orchestration=pipeline)

    assert code == EXIT_DONE
    assert pipeline.asked == [(fixture_repository.path.resolve(), fixture_repository.after)]
    assert github.calls == []


def test_push_with_nowhere_to_keep_anything_does_nothing_and_ends_as_done(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    github = RecordingGitHub()
    environment = environment_for(tmp_path, fixture_repository, {"ref": "main"}, name="push")

    assert main(environment, github=github) == EXIT_DONE
    assert main(environment, github=github, orchestration=GathersNothing()) == EXIT_DONE
    assert github.calls == []


def test_push_whose_pipeline_raises_is_a_failure(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    github, pipeline = RecordingGitHub(), prepares(tmp_path, SandboxFailed("no container runtime"))
    environment = environment_for(tmp_path, fixture_repository, {"ref": "main"}, name="push")

    assert main(environment, github=github, orchestration=pipeline) == EXIT_FAILED
    assert len(pipeline.asked) == 1
    assert github.calls == []


def test_push_prepares_nothing_when_the_state_is_kept_where_a_pull_request_could_write(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    pipeline = prepares(tmp_path, Unsupported("Nothing here says how the code is tested."))
    environment = environment_for(
        tmp_path,
        fixture_repository,
        {"ref": "main"},
        name="push",
        **{STATE_VARIABLE: str(fixture_repository.path / "kept")},
    )

    assert main(environment, github=RecordingGitHub(), orchestration=pipeline) == EXIT_FAILED
    assert pipeline.asked == []


# Tracing


def test_run_of_the_action_sends_its_one_trace_before_it_returns_and_answers_as_it_would(
    fixture_repository: FixtureRepository, tmp_path: Path, collector: Collector
) -> None:
    environment = environment_for(
        tmp_path,
        fixture_repository,
        pull_request_event(fixture_repository),
        **{ENDPOINT_VARIABLE: collector.endpoint},
    )

    code = verify(environment, RecordingGitHub())

    assert code == EXIT_DONE
    assert len(collector.received) == 1
    ((path, sent),) = collector.received
    assert path == "/v1/traces"
    assert [span["name"] for span in exported_spans(sent)].count("pr-verification") == 1
    assert TOKEN not in json.dumps(sent)


# The core knows nothing about GitHub


def test_no_other_module_imports_the_wrapper_or_names_a_variable_that_github_sets() -> None:
    package = Path(pr_verification.__file__).parent
    delivery = {"github_action.py", "comment.py"}

    for module in sorted(package.glob("*.py")):
        nodes = list(ast.walk(ast.parse(module.read_text())))
        imported = {
            name
            for node in nodes
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in [getattr(node, "module", None), *(alias.name for alias in node.names)]
        }
        texts = {
            node.value
            for node in nodes
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if module.name != "github_action.py":
            assert not imported & {"pr_verification.github_action", "github_action"}, module.name
            assert not any(re.fullmatch("GITHUB_[A-Z_]+", text) for text in texts), module.name
        if module.name not in delivery:
            assert not imported & {"pr_verification.comment", "comment"}, module.name
