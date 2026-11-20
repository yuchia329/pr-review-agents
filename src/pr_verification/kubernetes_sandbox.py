"""The Sandbox as one Kubernetes Job per run, on the tainted sandbox node (execution plan B2).

It keeps the promises of `Sandbox` and `SandboxWithHomes`. The phase is a label, `install` or
`test`, and the network policies in `infra/` are what make the install phase able to reach the
network and the test phase able to reach nothing. A volume is a claim on the `sandbox` storage
class, which is the data disk of the sandbox node. Filling a volume and carrying a home
directory are done by a short-lived helper pod that runs the archive commands, not pull request
code. The archive is streamed through `kubectl exec` and is never unpacked on this machine.

`name` and `step` mean what they mean for `ContainerSandbox`. The Job of a step is named from
the two, and one left by an earlier attempt is deleted before the step is tried again.

The namespace, storage class, and kubectl context come from the environment when not passed:
`PR_VERIFICATION_SANDBOX_NAMESPACE` (`sandbox`), `PR_VERIFICATION_SANDBOX_STORAGE_CLASS`
(`sandbox`), and `PR_VERIFICATION_KUBE_CONTEXT` (the current context).
"""

import hashlib
import json
import math
import os
import secrets
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from pr_verification.repository import resolve_commit
from pr_verification.sandbox import (
    _HOME,
    _NAME,
    _RESTORE_HOME,
    _SAVE_HOME,
    _USER_ID,
    _VOLUME,
    _WORKING_COPY,
    Command,
    LimitHit,
    Limits,
    Phase,
    RunResult,
    SandboxFailed,
    _archive_commit,
)

_LABEL = "pr-verification.sandbox"
_PULL_FAILED = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}
_NODE_LABEL = "pr-verification/node"
_SANDBOX_NODE = "sandbox"
_COULD_NOT_START = {
    "executable file not found": 127,
    "no such file or directory": 127,
    "permission denied": 126,
}
"""Words of a `StartError` and the exit status each stands for. A container that never started
reports a start time of 1970, so it is told apart by its reason, not by its times."""
_START_WITHIN_SECONDS = 600.0
"""How long the Sandbox waits for a pod to start, image pull included, before the run itself
has failed."""


class KubernetesSandbox:
    """`SandboxWithHomes` on the cluster the plan's Terraform builds."""

    def __init__(
        self,
        repository: Path,
        image: str,
        *,
        name: str | None = None,
        step: str | None = None,
        namespace: str | None = None,
        storage_class: str | None = None,
        context: str | None = None,
    ) -> None:
        """A Sandbox for the commits of `repository`, running commands in `image`.

        Raises `ValueError` when `name` or `step` holds anything but lowercase letters,
        digits, and `-`. Raises `SandboxFailed` when kubectl cannot reach the cluster, or the
        sandbox node is not there.
        """
        self._repository = repository
        self._image = image
        for given in (name, step):
            if given is not None and not _NAME.fullmatch(given):
                raise ValueError(f"{given!r} holds other than lowercase letters, digits, and -")
        self._name = name
        self._step = step if name is not None else None
        self._namespace = namespace or os.environ.get(
            "PR_VERIFICATION_SANDBOX_NAMESPACE", "sandbox"
        )
        self._storage_class = storage_class or os.environ.get(
            "PR_VERIFICATION_SANDBOX_STORAGE_CLASS", "sandbox"
        )
        self._context = context or os.environ.get("PR_VERIFICATION_KUBE_CONTEXT")
        self._instance = name if name is not None else f"sandbox-{secrets.token_hex(4)}"
        self._runs = 0
        self._processors = self._allocatable_processors()

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        """See `Sandbox.run`."""
        try:
            pvc = self._volume_for(commit)
            job = self._job_name()
            if self._step is not None:
                self._delete("job", job)
            self._apply(self._job(job, command, pvc, phase, limits))
            try:
                return self._wait(job, limits.seconds)
            finally:
                if self._step is None:
                    self._delete("job", job)
        except (OSError, subprocess.CalledProcessError) as error:
            raise SandboxFailed(f"the Sandbox could not do the run: {error}") from error

    def save_home(self, commit: str, archive: Path) -> None:
        """See `SandboxWithHomes.save_home`."""
        try:
            pvc = self._volume_for(commit)
            descriptor, partial = tempfile.mkstemp(dir=archive.parent, prefix=f"{archive.name}.")
            try:
                with open(descriptor, "wb") as written:
                    self._with_helper(pvc, lambda pod: self._exec(pod, _SAVE_HOME, stdout=written))
                os.replace(partial, archive)
            except BaseException:
                os.unlink(partial)
                raise
        except (OSError, subprocess.CalledProcessError) as error:
            raise SandboxFailed(f"the Sandbox could not save a home directory: {error}") from error

    def restore_home(self, commit: str, archive: Path) -> None:
        """See `SandboxWithHomes.restore_home`."""
        try:
            pvc = self._volume_for(commit)
            with open(archive, "rb") as saved:
                self._with_helper(pvc, lambda pod: self._exec(pod, _RESTORE_HOME, stdin=saved))
        except (OSError, subprocess.CalledProcessError) as error:
            raise SandboxFailed(
                f"the Sandbox could not restore a home directory: {error}"
            ) from error

    def close(self) -> None:
        """Remove everything this Sandbox made, except the volumes of a named Sandbox.

        Calling it again does nothing.
        """
        if self._name is None:
            self._delete_labelled("job", "pod", "pvc")
        else:
            self._delete_labelled("job", "pod")

    def __enter__(self) -> "KubernetesSandbox":
        """Returns this Sandbox."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Calls `close`, whether or not the block raised."""
        self.close()

    def _volume_for(self, commit: str) -> str:
        try:
            commit = resolve_commit(self._repository, commit)
        except subprocess.CalledProcessError as error:
            raise SandboxFailed(f"commit {commit!r} is not in {self._repository}") from error
        pvc = _object_name(f"{self._instance}-{commit}")
        found = self._get("pvc", pvc)
        if found is not None and _annotation(found, "pr-verification/filled") == "true":
            return pvc
        if found is not None:
            self._delete("pvc", pvc)
        self._apply(self._claim(pvc, commit))
        try:
            self._fill(pvc, commit)
        except BaseException:
            self._delete("pvc", pvc)
            raise
        self._kubectl("annotate", "pvc", pvc, "pr-verification/filled=true", "--overwrite")
        return pvc

    def _fill(self, pvc: str, commit: str) -> None:
        def unpack(pod: str) -> None:
            with tempfile.TemporaryFile() as archive:
                with tarfile.open(fileobj=archive, mode="w") as tar:
                    _archive_commit(self._repository, commit, tar)
                archive.seek(0)
                self._exec(pod, ("tar", "-x", "-f", "-", "-C", _VOLUME), stdin=archive)

        self._with_helper(pvc, unpack)

    def _with_helper(self, pvc: str, use: Callable[[str], None]) -> None:
        pod = _object_name(f"helper-{pvc}-{secrets.token_hex(3)}")
        self._apply(self._helper(pod, pvc))
        try:
            self._wait_until_started(pod, deadline=time.monotonic() + _START_WITHIN_SECONDS)
            use(pod)
        finally:
            self._delete("pod", pod)

    def _wait(self, job: str, seconds: float) -> RunResult:
        """Until the command ends or reaches its time limit, counted from when it starts."""
        give_up = time.monotonic() + _START_WITHIN_SECONDS
        started: float | None = None
        while True:
            pod = self._pod_for_job(job)
            if pod is None:
                if time.monotonic() > give_up:
                    raise SandboxFailed("the Sandbox could not do the run: the Job made no pod")
                time.sleep(0.2)
                continue
            name = str(pod["metadata"]["name"])
            container = _container_state(pod)
            if container is None:
                if time.monotonic() > give_up:
                    raise SandboxFailed("the Sandbox could not do the run: the pod did not start")
                time.sleep(0.2)
                continue
            waiting = container.get("waiting")
            if waiting is not None:
                if waiting.get("reason") in _PULL_FAILED:
                    message = waiting.get("message", waiting["reason"])
                    raise SandboxFailed(f"the Sandbox could not do the run: {message}")
                if time.monotonic() > give_up:
                    raise SandboxFailed("the Sandbox could not do the run: the image did not start")
                time.sleep(0.2)
                continue
            running = container.get("running")
            if running is not None:
                if started is None:
                    started = _timestamp(str(running["startedAt"]))
                    give_up = started + seconds + 30
                if time.time() >= started + seconds:
                    output = self._logs(name)
                    self._delete("job", job)
                    return RunResult(output, exit_status=None, limit_hit=LimitHit.TIME)
                time.sleep(0.2)
                continue
            terminated = container.get("terminated") or {}
            output = self._logs(name)
            reason = str(terminated.get("reason") or "")
            if reason == "OOMKilled":
                return RunResult(output, exit_status=None, limit_hit=LimitHit.MEMORY)
            if reason == "StartError":
                return _could_not_start(str(terminated.get("message") or reason))
            began = terminated.get("startedAt")
            ended = terminated.get("finishedAt")
            if isinstance(began, str) and isinstance(ended, str):
                if reason == "DeadlineExceeded" or _timestamp(ended) >= _timestamp(began) + seconds:
                    return RunResult(output, exit_status=None, limit_hit=LimitHit.TIME)
            elif started is None:
                raise SandboxFailed(
                    f"the Sandbox could not do the run: {reason or 'the command did not start'}"
                )
            code = terminated.get("exitCode")
            return RunResult(output, int(code) if code is not None else None, limit_hit=None)

    def _wait_until_started(self, pod: str, *, deadline: float) -> None:
        while time.monotonic() < deadline:
            found = self._get("pod", pod)
            if found is None:
                time.sleep(0.2)
                continue
            container = _container_state(found)
            waiting = (container or {}).get("waiting")
            if waiting and waiting.get("reason") in _PULL_FAILED:
                message = waiting.get("message", waiting["reason"])
                raise SandboxFailed(f"the Sandbox could not do the run: {message}")
            if container is not None and "running" in container:
                return
            phase = str(found.get("status", {}).get("phase"))
            if phase in {"Failed", "Succeeded"}:
                raise SandboxFailed(f"the Sandbox could not do the run: the helper {phase}")
            time.sleep(0.2)
        raise SandboxFailed("the Sandbox could not do the run: the helper did not start")

    def _job(
        self, name: str, command: Command, pvc: str, phase: Phase, limits: Limits
    ) -> dict[str, Any]:
        processors = min(limits.processors, self._processors)
        cpu = _cpu(processors)
        memory = str(limits.memory_bytes)
        labels = {**self._labels(), "phase": phase.value}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": self._namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 600,
                "activeDeadlineSeconds": math.ceil(limits.seconds) + int(_START_WITHIN_SECONDS),
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        **_held_on_the_sandbox_node(),
                        "containers": [
                            {
                                "name": "run",
                                "image": self._image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": list(command),
                                "workingDir": f"{_VOLUME}/{_WORKING_COPY}",
                                "env": [{"name": "HOME", "value": f"{_VOLUME}/{_HOME}"}],
                                "securityContext": _not_root(),
                                "resources": {
                                    "limits": {"memory": memory, "cpu": cpu},
                                    "requests": {"memory": memory, "cpu": cpu},
                                },
                                "volumeMounts": [{"name": "commit", "mountPath": _VOLUME}],
                            }
                        ],
                        "volumes": [
                            {"name": "commit", "persistentVolumeClaim": {"claimName": pvc}}
                        ],
                    },
                },
            },
        }

    def _helper(self, name: str, pvc: str) -> dict[str, Any]:
        labels = {**self._labels(), "phase": Phase.TEST.value}
        spec = _held_on_the_sandbox_node()
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": self._namespace, "labels": labels},
            "spec": {
                **spec,
                "containers": [
                    {
                        "name": "helper",
                        "image": self._image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sleep", "600"],
                        # As the runs' own user: with every capability dropped, root could
                        # not give the unpacked files to that user.
                        "securityContext": _not_root(),
                        "volumeMounts": [{"name": "commit", "mountPath": _VOLUME}],
                    }
                ],
                "volumes": [{"name": "commit", "persistentVolumeClaim": {"claimName": pvc}}],
            },
        }

    def _claim(self, name: str, commit: str) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": self._labels(),
                "annotations": {
                    "pr-verification/commit": commit,
                    "pr-verification/filled": "false",
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": self._storage_class,
                "resources": {"requests": {"storage": "10Gi"}},
            },
        }

    def _labels(self) -> dict[str, str]:
        return {_LABEL: hashlib.sha256(self._instance.encode()).hexdigest()[:16]}

    def _job_name(self) -> str:
        if self._step is not None:
            return _object_name(f"{self._instance}-{self._step}")
        self._runs += 1
        return _object_name(f"{self._instance}-{self._runs}-{secrets.token_hex(2)}")

    def _allocatable_processors(self) -> float:
        listed = json.loads(
            self._kubectl("get", "nodes", "-l", f"{_NODE_LABEL}={_SANDBOX_NODE}", "-o", "json")
        )
        items = listed.get("items") or []
        if not items:
            raise SandboxFailed("the Sandbox has no node to run on")
        cpu = str(items[0]["status"]["allocatable"]["cpu"])
        if cpu.endswith("m"):
            return int(cpu[:-1]) / 1000
        return float(cpu)

    def _pod_for_job(self, job: str) -> dict[str, Any] | None:
        listed = json.loads(self._kubectl("get", "pods", "-l", f"job-name={job}", "-o", "json"))
        items = listed.get("items") or []
        return items[0] if items else None

    def _logs(self, pod: str) -> str:
        done = self._completed("logs", pod)
        if done.returncode != 0:
            return ""
        return done.stdout.decode("utf-8", "replace")

    def _exec(
        self,
        pod: str,
        command: Command,
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> None:
        self._run(
            ["exec", "-i", pod, "--", *command],
            stdin=stdin,
            stdout=stdout,
        )

    def _apply(self, manifest: dict[str, Any]) -> None:
        self._run(["apply", "-f", "-"], stdin=_json_bytes(manifest))

    def _get(self, kind: str, name: str) -> dict[str, Any] | None:
        done = self._completed("get", kind, name, "-o", "json")
        if done.returncode != 0:
            if b"NotFound" in done.stderr or b"not found" in done.stderr:
                return None
            detail = done.stderr.decode("utf-8", "replace").strip()
            raise SandboxFailed(f"kubectl failed to get: {detail}")
        loaded = json.loads(done.stdout)
        if not isinstance(loaded, dict):
            raise SandboxFailed("kubectl returned something other than an object")
        return loaded

    def _delete(self, kind: str, name: str) -> None:
        done = self._completed(
            "delete", kind, name, "--ignore-not-found", "--wait=false", "--grace-period=1"
        )
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()
            raise SandboxFailed(f"kubectl failed to delete: {detail}")

    def _delete_labelled(self, *kinds: str) -> None:
        selector = f"{_LABEL}={self._labels()[_LABEL]}"
        done = self._completed(
            "delete",
            ",".join(kinds),
            "-l",
            selector,
            "--ignore-not-found",
            "--wait=false",
            "--grace-period=1",
        )
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()
            raise SandboxFailed(f"kubectl failed to delete: {detail}")

    def _kubectl(self, *arguments: str) -> str:
        done = self._run(list(arguments))
        return done.stdout.decode("utf-8", "replace")

    def _run(
        self,
        arguments: list[str],
        *,
        stdin: IO[bytes] | bytes | None = None,
        stdout: IO[bytes] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            done = subprocess.run(
                self._argv(arguments),
                input=stdin if isinstance(stdin, bytes) else None,
                stdin=None if isinstance(stdin, bytes) or stdin is None else stdin,
                stdout=stdout or subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxFailed(f"cannot run kubectl: {error}") from error
        if done.returncode != 0:
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()
            raise SandboxFailed(f"kubectl failed to {arguments[0]}: {detail}")
        return done

    def _completed(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(self._argv(list(arguments)), capture_output=True)
        except OSError as error:
            raise SandboxFailed(f"cannot run kubectl: {error}") from error

    def _argv(self, arguments: list[str]) -> list[str]:
        command = ["kubectl"]
        if self._context:
            command.extend(["--context", self._context])
        cluster_scoped = arguments[:2] == ["get", "nodes"]
        if not cluster_scoped and "-n" not in arguments and "--namespace" not in arguments:
            command.extend(["-n", self._namespace])
        return command + arguments


def remove_sandbox(name: str, *, namespace: str | None = None, context: str | None = None) -> None:
    """Remove every Job, pod, and claim that a `KubernetesSandbox` made with `name`.

    Calling it again, or for a name nothing was made with, does nothing. Raises `SandboxFailed`
    when kubectl cannot do it.
    """
    where = namespace or os.environ.get("PR_VERIFICATION_SANDBOX_NAMESPACE", "sandbox")
    selected = context or os.environ.get("PR_VERIFICATION_KUBE_CONTEXT")
    label = f"{_LABEL}={hashlib.sha256(name.encode()).hexdigest()[:16]}"
    command = ["kubectl"]
    if selected:
        command.extend(["--context", selected])
    command.extend(
        [
            "-n",
            where,
            "delete",
            "job,pod,pvc",
            "-l",
            label,
            "--ignore-not-found",
            "--wait=false",
            "--grace-period=1",
        ]
    )
    try:
        done = subprocess.run(command, capture_output=True)
    except OSError as error:
        raise SandboxFailed(f"cannot run kubectl: {error}") from error
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise SandboxFailed(f"kubectl failed to delete: {detail}")


def _held_on_the_sandbox_node() -> dict[str, Any]:
    return {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 1,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "nodeSelector": {_NODE_LABEL: _SANDBOX_NODE},
        "tolerations": [
            {
                "key": _SANDBOX_NODE,
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ],
    }


def _could_not_start(message: str) -> RunResult:
    """The result of a command the runtime could not start, as `ContainerSandbox` gives it: the
    runtime's own words, with 127 for a program that does not exist and 126 for one that cannot
    be run. Any other failure to start is the Sandbox's own."""
    for words, status in _COULD_NOT_START.items():
        if words in message:
            return RunResult(message, status, limit_hit=None)
    raise SandboxFailed(f"the Sandbox could not do the run: {message}")


def _not_root() -> dict[str, Any]:
    return {
        "runAsUser": _USER_ID,
        "runAsGroup": _USER_ID,
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def _object_name(text: str) -> str:
    """A DNS label of at most 63 characters, stable for the same text."""
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    trimmed = text.strip("-")[:50].strip("-")
    made = f"{trimmed}-{digest}" if trimmed else digest
    return made[:63].strip("-")


def _cpu(processors: float) -> str:
    if processors >= 1:
        return f"{processors:g}"
    return f"{max(1, int(round(processors * 1000)))}m"


def _annotation(manifest: dict[str, Any], key: str) -> str:
    annotations = manifest.get("metadata", {}).get("annotations") or {}
    return str(annotations.get(key, ""))


def _container_state(pod: dict[str, Any]) -> dict[str, Any] | None:
    statuses = pod.get("status", {}).get("containerStatuses") or []
    if not statuses:
        return None
    state = statuses[0].get("state")
    return state if isinstance(state, dict) else None


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _json_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest).encode()
