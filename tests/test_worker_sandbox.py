"""Where a durable worker's Sandboxes run, as `PR_VERIFICATION_SANDBOX` says."""

from pathlib import Path

import pytest

from pr_verification import kubernetes_sandbox, sandbox
from pr_verification.configuration import InvalidConfiguration
from pr_verification.durable.driver import SANDBOX_VARIABLE, worker_resources
from pr_verification.kubernetes_sandbox import KubernetesSandbox
from pr_verification.sandbox import ContainerSandbox

MODEL = {"PR_VERIFICATION_ENDPOINT": "http://127.0.0.1:1/v1"}


@pytest.mark.parametrize(
    "environment",
    [MODEL, {**MODEL, SANDBOX_VARIABLE: "container"}],
    ids=["not set", "container"],
)
def test_worker_runs_sandboxes_in_local_containers_unless_told_otherwise(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    resources = worker_resources(environment, tmp_path)

    assert resources is not None
    assert resources.open_sandbox is ContainerSandbox
    assert resources.remove_sandbox is sandbox.remove_sandbox


def test_worker_told_kubernetes_runs_sandboxes_on_the_cluster(tmp_path: Path) -> None:
    resources = worker_resources({**MODEL, SANDBOX_VARIABLE: "kubernetes"}, tmp_path)

    assert resources is not None
    assert resources.open_sandbox is KubernetesSandbox
    assert resources.remove_sandbox is kubernetes_sandbox.remove_sandbox


def test_worker_refuses_a_sandbox_it_does_not_know(tmp_path: Path) -> None:
    with pytest.raises(InvalidConfiguration, match=SANDBOX_VARIABLE):
        worker_resources({**MODEL, SANDBOX_VARIABLE: "docker"}, tmp_path)
