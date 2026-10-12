"""The GitHub Action's wrapper: gathers a pull request from GitHub, calls the core, posts the
comment, and reports the Gate as a check run.

The wrapper is thin, and it is all the system knows of GitHub. The core knows nothing about
GitHub: no other module of the package imports this one, reads a variable that GitHub sets, or
makes a request to GitHub.

Only code posts (ADR 0002). The comment is `render_comment`'s and the check run is
`gate_check_run`'s. No model is handed the token, and nothing a model wrote is posted but as
the report holds it.

The Gate is reported as a check run of its own, named `CHECK_RUN_NAME`, and not by the exit
code. The job that runs the wrapper ends the same way whether the Gate passes or blocks, so
nothing in the pipeline that waits on that job waits on the Gate.

What is kept between runs
    The Repo Profile and the installed dependencies are kept under the directory that
    `STATE_VARIABLE` names, outside the checkout. The Action restores that directory from
    GitHub's cache before every run, and saves it to the cache only after a run for a push
    to the target branch. A run for a pull request reads what the target branch saved. What
    it learns itself stays in its own copy of the directory and is never saved to the cache,
    so no pull request can write where a later run reads (ADR 0004). Restoring and saving are
    workflow configuration, and so is which branches' pushes start a run.

Pull requests from forks
    A pull request from a fork is verified by a run for `pull_request_target`, which GitHub
    starts from the workflow file on the default branch and gives the secrets (ADR 0005). The
    checkout such a run starts with holds nothing of the pull request. The wrapper fetches the
    pull request's commit into it and never checks it out: the commit is read through git, as
    the Sandbox reads it, and nothing of it is run but in the Sandbox, which is given no
    secret (ADR 0001). That a maintainer approves each such run before it starts is workflow
    configuration.
"""

import base64
import json
import os
import re
import secrets
import subprocess
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx

from pr_verification.budget import Budget, InvalidBudget, budget_from_environment
from pr_verification.claims import ClaimExtractionFailed
from pr_verification.comment import MARKER, render_comment
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import (
    Decider,
    InvalidDecisionConfiguration,
    configured_decider,
    decision_configuration,
)
from pr_verification.model import Model, ModelFailed
from pr_verification.openai_compatible import model_from_environment
from pr_verification.orchestration import FixedPipeline, Orchestration, fixed_pipeline
from pr_verification.report import Gate, GateDecision
from pr_verification.repository import fetch_commit, git
from pr_verification.tracing import traced

EXIT_DONE = 0
"""The run did its work, whichever way the Gate went."""

EXIT_FAILED = 2
"""The run could not do its work. The same number as the command line's `EXIT_NO_REPORT`, and
never the number a block has there."""

CHECK_RUN_NAME = "PR Verification Gate"
"""The name of the check run the Gate is reported as."""

STATE_VARIABLE = "PR_VERIFICATION_STATE_DIR"
"""The environment variable that names the directory the Repo Profile and the installed
dependencies are kept in between runs."""

REVIEW_VARIABLE = "PR_VERIFICATION_REVIEW"
"""The environment variable that says whether a pull request is also reviewed. It is when
this is set to `true`, and is not for any other value or none."""

FORK_SKIPPED = (
    "PR Verification did nothing in this run: the pull request comes from a fork, and a fork "
    "is verified by a run for `pull_request_target`."
)
"""What a run for `pull_request` says of a pull request from a fork."""

SAME_REPOSITORY_SKIPPED = (
    "PR Verification did nothing in this run: the pull request comes from the repository "
    "itself, and is verified by a run for `pull_request`."
)
"""What a run for `pull_request_target` says of a pull request that is not from a fork."""


class Conclusion(StrEnum):
    """How a check run ended, in GitHub's words."""

    SUCCESS = "success"
    """The Gate passes."""

    FAILURE = "failure"
    """The Gate blocks."""

    NEUTRAL = "neutral"
    """There is no Gate to report. GitHub does not hold a pull request back for it."""


@dataclass(frozen=True)
class CheckRun:
    """What is reported on a commit as the check run named `CHECK_RUN_NAME`."""

    conclusion: Conclusion
    """How the check run ended."""

    title: str
    """One line, shown beside the check run."""

    summary: str
    """What the check run's own page says. GitHub shows it as Markdown, so it is only ever
    text that the project wrote or code computed, and never Untrusted Content."""


NO_REPORT_CHECK_RUN = CheckRun(
    Conclusion.NEUTRAL,
    "PR Verification did not finish",
    "No report was produced, so no Claim was verified. This is a failure of the system and "
    "not a block: see the log of the job.",
)
"""The check run for a run that ended with no report. It holds nothing of what went wrong,
which may be Untrusted Content: that is in the job's log."""


API_URL = "https://api.github.com"
"""Where GitHub's REST API is, unless a run is told otherwise."""

GRAPHQL_URL = "https://api.github.com/graphql"
"""Where GitHub's GraphQL API is, unless a run is told otherwise."""

SERVER_URL = "https://github.com"
"""Where GitHub serves repositories to git, unless a run is told otherwise."""


def gate_check_run(gate: Gate) -> CheckRun:
    """The check run that reports `gate`.

    The conclusion is `SUCCESS` when the Gate passes and `FAILURE` when it blocks. The title
    is `Gate: pass` or `Gate: block`, and the summary is the Gate's note. A pure function of
    `gate`, so no model has a say in the check run.
    """
    blocks = gate.decision is GateDecision.BLOCK
    conclusion = Conclusion.FAILURE if blocks else Conclusion.SUCCESS
    return CheckRun(conclusion, f"Gate: {gate.decision.value}", gate.note)


class GitHubFailed(Exception):
    """GitHub could not be reached, or did not do what was asked. Its message is one line and
    safe to print: it never holds the token."""


class GitHub(Protocol):
    """What the wrapper asks of GitHub, about the pull requests of one repository."""

    def linked_issue(self, pull_request: int) -> str:
        """The text of the issues that the pull request numbered `pull_request` closes.

        For each issue GitHub lists as closed by the pull request, in GitHub's order: its
        title, an empty line, and its body. An empty line stands between one issue and the
        next. The empty string when it closes none. It is Untrusted Content.

        Raises `GitHubFailed`.
        """
        ...

    def post_comment(self, pull_request: int, body: str) -> None:
        """Make `body` the one comment this system has on the pull request.

        `body` starts with `MARKER` (`pr_verification.comment`). A comment this system posted
        on the pull request before is replaced by `body`. When there is none, one is added.

        Raises `GitHubFailed`.
        """
        ...

    def set_check_run(self, commit: str, check_run: CheckRun) -> None:
        """Report `check_run` on `commit` under the name `CHECK_RUN_NAME`, as one that has
        ended.

        Raises `GitHubFailed`.
        """
        ...


class GitHubAPI:
    """`GitHub` over GitHub's own API, for one repository.

    Every request carries the header `Authorization: Bearer <token>`. A request that cannot
    be sent, an answer with a status of 400 or more, and an answer that cannot be read as
    what was asked for each raise `GitHubFailed`, with the token removed from what GitHub
    said.
    """

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = API_URL,
        graphql_url: str = GRAPHQL_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """A client for `repository`, named as GitHub names it: `<owner>/<name>`.

        `api_url` and `graphql_url` are where GitHub's REST API and its GraphQL API are.
        `transport` is for a caller that brings its own in place of the network.
        """
        self._repository = repository
        self._token = token
        self._repository_url = f"{api_url.rstrip('/')}/repos/{repository}"
        self._graphql_url = graphql_url
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=_TIMEOUT,
            transport=transport,
        )

    def linked_issue(self, pull_request: int) -> str:
        """See `GitHub.linked_issue`.

        One POST request to `graphql_url`, asking for the `title` and `body` of the first 10
        of the pull request's `closingIssuesReferences`. They are read from the answer's
        `data.repository.pullRequest.closingIssuesReferences.nodes`. A body that is null is
        the empty string.
        """
        owner, _, name = self._repository.partition("/")
        variables = {"owner": owner, "name": name, "number": pull_request}
        answer = self._ask("POST", self._graphql_url, {"query": _CLOSING, "variables": variables})
        try:
            pull = answer.json()["data"]["repository"]["pullRequest"]
            issues = [
                (issue["title"], "" if issue["body"] is None else issue["body"])
                for issue in pull["closingIssuesReferences"]["nodes"]
            ]
        except (ValueError, LookupError, TypeError, RecursionError):
            issues = None
        if issues is None or not all(isinstance(text, str) for issue in issues for text in issue):
            raise self._failed("POST", self._graphql_url, f"gave no issues: {answer.text}")
        return "\n\n".join(f"{title}\n\n{body}" for title, body in issues)

    def post_comment(self, pull_request: int, body: str) -> None:
        """See `GitHub.post_comment`.

        Looks among the pull request's first 100 comments, oldest first, with
        `GET <api_url>/repos/<repository>/issues/<pull_request>/comments?per_page=100`. A
        comment this system posted before is the first of them whose `body` starts with
        `MARKER` and whose `user.type` is `Bot`: a comment a person wrote is never replaced,
        whatever it starts with.

        That comment is replaced with
        `PATCH <api_url>/repos/<repository>/issues/comments/<its id>`. When there is none,
        one is added with `POST <api_url>/repos/<repository>/issues/<pull_request>/comments`.
        Either request sends the JSON object `{"body": body}`.
        """
        comments = f"{self._repository_url}/issues/{pull_request}/comments"
        posted = self._posted_before(comments)
        if posted is None:
            self._ask("POST", comments, {"body": body})
        else:
            self._ask("PATCH", f"{self._repository_url}/issues/comments/{posted}", {"body": body})

    def _posted_before(self, comments: str) -> int | None:
        """The id of the comment this system posted before, among those listed at `comments`,
        or None when it posted none."""
        url = f"{comments}?per_page=100"
        answer = self._ask("GET", url)
        try:
            listed = answer.json()
        except (ValueError, RecursionError):
            listed = None
        if not isinstance(listed, list) or not all(isinstance(each, dict) for each in listed):
            raise self._failed("GET", url, f"gave no list of comments: {answer.text}")
        for comment in listed:
            body, user, number = comment.get("body"), comment.get("user"), comment.get("id")
            # Only a comment a bot wrote: a person can start one with the marker too.
            written_by_bot = isinstance(user, dict) and user.get("type") == "Bot"
            if isinstance(body, str) and body.startswith(MARKER) and written_by_bot:
                if not isinstance(number, int):
                    raise self._failed("GET", url, f"gave a comment with no id: {answer.text}")
                return number
        return None

    def set_check_run(self, commit: str, check_run: CheckRun) -> None:
        """See `GitHub.set_check_run`.

        One `POST <api_url>/repos/<repository>/check-runs`, sending a JSON object with `name`
        `CHECK_RUN_NAME`, `head_sha` `commit`, `status` `completed`, `conclusion` the value
        of the check run's `Conclusion`, and `output` an object with its `title` and
        `summary`.
        """
        self._ask(
            "POST",
            f"{self._repository_url}/check-runs",
            {
                "name": CHECK_RUN_NAME,
                "head_sha": commit,
                "status": "completed",
                "conclusion": check_run.conclusion.value,
                "output": {"title": check_run.title, "summary": check_run.summary},
            },
        )

    def _ask(self, method: str, url: str, body: object = None) -> httpx.Response:
        """Make one request, sending `body` as JSON when there is one. Raises `GitHubFailed`
        when it cannot be sent or is answered with a status of 400 or more."""
        try:
            answer = self._client.request(method, url, json=body)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            # Not chained: the cause would be printed with any traceback, unredacted.
            raise self._failed(method, url, f"could not be sent: {error}") from None
        if answer.status_code >= 400:
            raise self._failed(
                method, url, f"was answered with HTTP {answer.status_code}: {answer.text}"
            )
        return answer

    def _failed(self, method: str, url: str, what: str) -> GitHubFailed:
        """One line, without the token: GitHub, or the layer that sends, may repeat it back."""
        message = " ".join(f"GitHub's {method} {url} {what}".split())
        if self._token:
            message = message.replace(self._token, "[token removed]")
        if len(message) > _LONGEST_FAILURE:
            message = message[:_LONGEST_FAILURE] + " [cut short]"
        return GitHubFailed(message)


_TIMEOUT = httpx.Timeout(30.0)
_LONGEST_FAILURE = 500

_CLOSING = """\
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 10) {
        nodes {
          title
          body
        }
      }
    }
  }
}
"""
"""Asks GitHub's GraphQL API for the issues a pull request closes."""


def main(
    environment: Mapping[str, str] | None = None,
    *,
    github: GitHub | None = None,
    model: Model | None = None,
    decider: Decider | None = None,
    orchestration: Orchestration | None = None,
    budget: Budget | None = None,
) -> int:
    """Do the work of one run of the Action, and return its exit code.

    `environment` is the process's own when None. It gives the variables that GitHub sets,
    `GITHUB_EVENT_NAME`, `GITHUB_EVENT_PATH`, `GITHUB_WORKSPACE`, `GITHUB_REPOSITORY`, and
    `GITHUB_SHA`; `GITHUB_TOKEN`, which the workflow passes; and the system's own
    configuration. The repository is the checkout at `GITHUB_WORKSPACE`. `github`, `model`,
    `decider`, `orchestration`, and `budget` are for a caller that brings its own. Without
    them:

    - `github` is `GitHubAPI(GITHUB_REPOSITORY, GITHUB_TOKEN)`, with `GITHUB_API_URL` and
      `GITHUB_GRAPHQL_URL` as its two addresses when they are set;
    - `model`, `decider`, and `budget` are the ones the environment configures, as the
      command line finds them;
    - `orchestration` is `fixed_pipeline` on the directory `STATE_VARIABLE` names, with the
      decider as its progress guard, as the command line makes it for `--state-dir`. When
      the variable is unset or empty there is none, and nothing is kept between runs.

    A directory named by `STATE_VARIABLE` that is the checkout or lies inside it, both
    resolved, is a failure, found before anything else is done: a pull request's files are
    checked out there, so a pull request could write to it.

    A pull request: `GITHUB_EVENT_NAME` is `pull_request` or `pull_request_target`
        The event is the JSON object in the file `GITHUB_EVENT_PATH` names, and the pull
        request is its `pull_request`. Its `head.sha` is a commit's full ID, of 40 or of 64
        hexadecimal digits in lower case: an event that names the commit any other way cannot
        be read.

        A pull request comes from a fork when its `head.repo.full_name` is not its
        `base.repo.full_name`, which includes its having no `head.repo`. A run for
        `pull_request` verifies a pull request that does not come from a fork, and a run for
        `pull_request_target` verifies one that does, so each is verified once however the
        workflow starts its runs.

        The other kind is skipped: the one line `::notice::` followed by `FORK_SKIPPED`, in
        a run for `pull_request`, or by `SAME_REPOSITORY_SKIPPED`, in a run for
        `pull_request_target`, is printed on standard output, which GitHub shows on the
        run's page, and the answer is `EXIT_DONE`. Nothing else is done. No model is
        configured or asked, nothing of the pull request is read, fetched, or run, and
        `github` is not called, so the skip needs neither a model nor a token. No check run
        is set: the run that verifies the pull request sets it, and the token GitHub gives
        a run for `pull_request` from a fork could not.

        A run for `pull_request_target` starts from a checkout of the default branch, so it
        first fetches the `head.sha` from the checkout's remote `origin`, asking for it by
        its ID: the commit verified is the one the event names, whatever the fork has pushed
        since. Nothing is checked out. When `GITHUB_TOKEN` is set, the request carries it as
        the header `Authorization: basic`, as the password of the user `x-access-token`, and
        only when `origin` is under `GITHUB_SERVER_URL`, or under `SERVER_URL` when that is
        unset or empty. A commit that cannot be fetched is a failure.

        The pull request is then verified, in this order:

        1. Gathers the pull request. `after` is its `head.sha`. `before` is the merge base
           of its `base.sha` and its `head.sha` in the repository: the commit of the target
           branch that the pull request starts from. The title is its `title`, the
           description is its `body`, the empty string when that is null, and the linked
           issue is `github.linked_issue` of its `number`.
        2. Calls the core's entry point, `run`, with that pull request, the model, the
           decider, the orchestration, and the Budget, and with `review` true when
           `REVIEW_VARIABLE` says so, as the command line does for `--review`.
        3. Prints the report, as `Report.to_json` writes it.
        4. Sets the check run: `github.set_check_run(after, gate_check_run(report.gate))`.
        5. Posts the comment: `github.post_comment(number, render_comment(report))`.

        Steps 4 and 5 are each done whatever came of the other. The answer is `EXIT_DONE`
        whichever way the Gate goes.

    A push: `GITHUB_EVENT_NAME` is `push`
        `GITHUB_SHA` is the commit pushed, on the target branch. It is prepared, so that the
        Repo Profile is learned and the dependencies are installed from the target branch
        and from nothing of a pull request. The pipeline is `orchestration` when that is a
        `FixedPipeline`, and otherwise `fixed_pipeline` on the directory `STATE_VARIABLE`
        names, with no progress guard. With one,
        `pipeline.prepare_target_commit(repository, GITHUB_SHA)` is called once. With none,
        nothing is done.

        No model is configured or asked, no Budget is kept to, and `github` is not called, so
        a push needs neither a model nor a token. The answer is `EXIT_DONE` whatever the
        pipeline answers: `Unsupported` and `CommandFailed` are answers, which leave nothing
        new kept.

    Any other event
        Nothing is done, and it is a failure.

    Failures
        A failure is a run that could not do its work: an event that cannot be read, a model,
        a decider, or a Budget that cannot be had as configured, no report from `run`, a
        `GitHubFailed`, or anything at all that is raised. It is one printed line that starts
        `pr-verification: `, or the traceback of an exception that nothing here expects, and
        the answer is `EXIT_FAILED`.

        When a pull request that is not skipped ends with no report, no comment is
        posted, and `github.set_check_run(after, NO_REPORT_CHECK_RUN)` is called once the
        `head.sha` has been read, so that a failure of the system is never shown as a block
        and the check run is not left waiting. A `GitHubFailed` from that call is one more
        printed line.

    Printing
        GitHub's runner reads the log of a job and acts on commands it finds in a line. So
        whatever is printed that can hold Untrusted Content, which is the report, the line
        of a failure, and a traceback, is printed on standard output after the one line
        `::stop-commands::<token>` and before the one line `::<token>::`, where the token is
        32 hexadecimal digits chosen at random for the run. Between those two lines the
        runner acts on nothing. Those two lines are printed once each, and only by a run
        that prints something between them.

    Tracing
        All of this is done inside `traced(environment)`: with tracing configured, the
        run's trace is sent before `main` returns, and the answer is the same as with
        tracing off.
    """
    if environment is None:
        environment = os.environ
    log = _Log()
    with traced(environment):
        try:
            return _run(environment, log, github, model, decider, orchestration, budget)
        except Exception as error:
            log.failed(error)
            return EXIT_FAILED
        finally:
            log.close()


class _CannotRun(Exception):
    """The run cannot do its work as it was started. Its message is one line."""


_FAILURES = (
    _CannotRun,
    GitHubFailed,
    InvalidPullRequest,
    InvalidBudget,
    InvalidDecisionConfiguration,
    ModelFailed,
    ClaimExtractionFailed,
    OSError,
    UnicodeDecodeError,
)
"""What a run that could not do its work raises when nothing is at fault in the system, as
the command line has it. Anything else is a fault, and is printed as its traceback."""


class _Log:
    """Prints what may hold Untrusted Content, where GitHub's runner acts on nothing."""

    def __init__(self) -> None:
        self._token = secrets.token_hex(16)
        self._stopped = False

    def print(self, text: str) -> None:
        if not self._stopped:
            print(f"::stop-commands::{self._token}")
            self._stopped = True
        print(text, flush=True)

    def failed(self, error: Exception) -> None:
        """Print a failure: its one line, or the traceback of a fault."""
        if isinstance(error, _FAILURES):
            self.print(f"pr-verification: {' '.join(str(error).split())}")
        else:
            self.print("".join(traceback.format_exception(error)).rstrip("\n"))

    def close(self) -> None:
        """Let the runner act on commands again, when it was stopped."""
        if self._stopped:
            print(f"::{self._token}::", flush=True)


def _run(
    environment: Mapping[str, str],
    log: _Log,
    github: GitHub | None,
    model: Model | None,
    decider: Decider | None,
    orchestration: Orchestration | None,
    budget: Budget | None,
) -> int:
    """One run of the Action, as `main` describes it. Raises for a failure that leaves nothing
    more to do."""
    repository = Path(_variable(environment, "GITHUB_WORKSPACE"))
    state = _state_directory(environment, repository)
    event = _variable(environment, "GITHUB_EVENT_NAME")
    if event in ("pull_request", "pull_request_target"):
        return _verify(
            environment,
            log,
            repository,
            state,
            github,
            model,
            decider,
            orchestration,
            budget,
            verifies_forks=event == "pull_request_target",
        )
    if event != "push":
        raise _CannotRun(f"the event {event!r} is neither a pull request nor a push")
    pipeline = orchestration if isinstance(orchestration, FixedPipeline) else None
    if pipeline is None and state is not None:
        pipeline = fixed_pipeline(state)
    if pipeline is not None:
        pipeline.prepare_target_commit(repository, _variable(environment, "GITHUB_SHA"))
    return EXIT_DONE


def _verify(
    environment: Mapping[str, str],
    log: _Log,
    repository: Path,
    state: Path | None,
    github: GitHub | None,
    model: Model | None,
    decider: Decider | None,
    orchestration: Orchestration | None,
    budget: Budget | None,
    *,
    verifies_forks: bool,
) -> int:
    """A run for a pull request, as `main` describes it. `state` is the directory that what is
    kept between runs is kept in, if there is one, and `verifies_forks` is whether the run is
    one for `pull_request_target`, which verifies pull requests from forks and no others."""
    pull_request = _member(_event(environment), dict, "pull_request")
    after = _member(pull_request, str, "head", "sha")
    if not _COMMIT_ID.fullmatch(after):
        raise _CannotRun("the event's pull_request.head.sha is not a commit's full ID")
    if _from_fork(pull_request) != verifies_forks:
        skipped = SAME_REPOSITORY_SKIPPED if verifies_forks else FORK_SKIPPED
        print(f"::notice::{skipped}", flush=True)
        return EXIT_DONE
    if github is None:
        github = _github(environment)
    try:
        if verifies_forks:
            _fetch(environment, repository, after)
        if model is None:
            model = model_from_environment(environment)
        if model is None:
            raise _CannotRun(
                "no model is configured, so nothing can be verified: set "
                "PR_VERIFICATION_API_KEY, or PR_VERIFICATION_ENDPOINT for a model that needs "
                "no key"
            )
        if decider is None:
            decider = configured_decider(decision_configuration(environment), model)
        if budget is None:
            budget = budget_from_environment(environment)
        if orchestration is None and state is not None:
            orchestration = fixed_pipeline(state, progress_guard=decider)
        number = _member(pull_request, int, "number")
        report = run(
            _gathered(pull_request, repository, after, github.linked_issue(number)),
            model,
            decider,
            orchestration=orchestration,
            budget=budget,
            review=environment.get(REVIEW_VARIABLE) == "true",
        )
    except Exception as error:
        log.failed(error)
        # A failure of the system is not shown as a block, and the check run is not left waiting.
        _delivered(log, lambda: github.set_check_run(after, NO_REPORT_CHECK_RUN))
        return EXIT_FAILED
    log.print(report.to_json())
    delivered = [
        _delivered(log, lambda: github.set_check_run(after, gate_check_run(report.gate))),
        _delivered(log, lambda: github.post_comment(number, render_comment(report))),
    ]
    return EXIT_DONE if all(delivered) else EXIT_FAILED


def _delivered(log: _Log, deliver: Callable[[], None]) -> bool:
    """Whether `deliver` did its work. A failure is printed and not raised, so that what is
    delivered next is delivered whatever came of this."""
    try:
        deliver()
    except Exception as error:
        log.failed(error)
        return False
    return True


def _variable(environment: Mapping[str, str], name: str) -> str:
    if not environment.get(name):
        raise _CannotRun(f"{name} is not set")
    return environment[name]


def _state_directory(environment: Mapping[str, str], repository: Path) -> Path | None:
    """The directory `STATE_VARIABLE` names, or None when it names none. Raises `_CannotRun`
    when a pull request could write to it."""
    named = environment.get(STATE_VARIABLE, "")
    if not named:
        return None
    if Path(named).resolve().is_relative_to(repository.resolve()):
        raise _CannotRun(
            f"{STATE_VARIABLE} names a directory inside the checkout, where a pull request "
            "could write to it"
        )
    return Path(named)


def _event(environment: Mapping[str, str]) -> object:
    """The event of the run. Raises `_CannotRun` when it cannot be read."""
    path = _variable(environment, "GITHUB_EVENT_PATH")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise _CannotRun(f"the event at {path} cannot be read: {error}") from error


def _member[Kind](event: object, kind: type[Kind], *path: str) -> Kind:
    """The member of `event` at `path`, which is a `kind`. Raises `_CannotRun` when the event
    has no such member."""
    for name in path:
        if not isinstance(event, dict) or name not in event:
            raise _CannotRun(f"the event has no {'.'.join(path)}")
        event = event[name]
    if not isinstance(event, kind):
        raise _CannotRun(f"the event's {'.'.join(path)} is not what GitHub gives")
    return event


def _from_fork(pull_request: Mapping[str, object]) -> bool:
    """Whether the pull request comes from a fork, as `main` says one does."""
    base = _member(pull_request, str, "base", "repo", "full_name")
    repository = _member(pull_request, dict, "head").get("repo")
    return not isinstance(repository, dict) or repository.get("full_name") != base


def _github(environment: Mapping[str, str]) -> GitHubAPI:
    """GitHub's own API, as the environment configures it."""
    return GitHubAPI(
        _variable(environment, "GITHUB_REPOSITORY"),
        _variable(environment, "GITHUB_TOKEN"),
        api_url=environment.get("GITHUB_API_URL") or API_URL,
        graphql_url=environment.get("GITHUB_GRAPHQL_URL") or GRAPHQL_URL,
    )


_COMMIT_ID = re.compile("[0-9a-f]{40}|[0-9a-f]{64}")
"""A commit's full ID, by either of the two ways git has of hashing."""


def _fetch(environment: Mapping[str, str], repository: Path, commit: str) -> None:
    """Fetch `commit` into `repository` from its `origin`, as `main` says a fork's is."""
    authorization = {}
    if environment.get("GITHUB_TOKEN"):
        server = (environment.get("GITHUB_SERVER_URL") or SERVER_URL).rstrip("/")
        credentials = f"x-access-token:{environment['GITHUB_TOKEN']}".encode()
        # In git's environment and not among its arguments, which a refusal would print.
        authorization = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{server}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {base64.b64encode(credentials).decode()}",
        }
    try:
        fetch_commit(repository, "origin", commit, environment=authorization)
    except subprocess.CalledProcessError as error:
        refusal = error.stderr.decode(errors="replace")
        raise InvalidPullRequest(
            f"cannot fetch the pull request's commit {commit} from origin: {refusal}"
        ) from error


def _gathered(
    pull_request: Mapping[str, object], repository: Path, after: str, linked_issue: str
) -> PullRequest:
    """The pull request as the core takes one, from the event's `pull_request`."""
    base = _member(pull_request, str, "base", "sha")
    description = pull_request.get("body")
    if description is not None and not isinstance(description, str):
        raise _CannotRun("the event's pull_request.body is not what GitHub gives")
    try:
        before = git(repository, "merge-base", "--end-of-options", base, after).decode().strip()
    except subprocess.CalledProcessError as error:
        refusal = error.stderr.decode(errors="replace")
        raise InvalidPullRequest(
            f"cannot find where the pull request starts from in {repository}: {refusal}"
        ) from error
    return PullRequest(
        repository=repository,
        before=before,
        after=after,
        title=_member(pull_request, str, "title"),
        description=description or "",
        linked_issue=linked_issue,
    )
