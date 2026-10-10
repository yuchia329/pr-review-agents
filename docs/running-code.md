# Running code

Pull request code and Reproductions are run in one place only, the Sandbox. What is specific to a
language sits behind the Ecosystem boundary. The contracts are the docstrings in
`src/pr_verification/sandbox.py`, `ecosystem.py`, and `python_ecosystem.py`; this page says what
is needed to use them and where their limits are.

## The Sandbox

The Sandbox is a local container (ADR 0001). It needs the `docker` command and a container
runtime on the same machine that answers it. Without one, making a `ContainerSandbox` raises
`SandboxFailed`.

Each run is a new container. What it is given:

- The files of one commit, as a working copy it may write to, and a home directory. Both are kept
  in a volume that belongs to that commit, so an install phase leaves what it installed for the
  test phase, and the commit before a pull request never sees what the commit after it left.
- Nothing else from the machine that started it: no environment variable, no file, no socket of
  the container runtime.
- A user that is not root, with every capability dropped and no way to gain privileges.
- The network in the install phase, and none in the test phase.
- Limits on time, memory, and processors. A run that reaches the time limit or the memory limit
  is stopped whole and reported as such; the processor limit only slows it.

Closing the Sandbox removes the volumes. Containers are removed as each run ends.

### Known limits

**A container is a weaker boundary than a virtual machine.** The code inside shares the kernel
with the process that started it, and that process holds the model's API key. A flaw in the
kernel or the container runtime that lets code out of the container would let it reach the key.
Nothing here removes that risk. It is reduced by the Sandbox never being given a secret, by the
runner being short-lived, and by the key being narrowly scoped. The Sandbox boundary is one
method wide so that a remote virtual machine can take the container's place.

Also not limited: the disk space a run may fill, the number of processes it may start, and the
length of the output it may write. The Sandbox returns within its stated time of a limit being
reached only while the container runtime answers promptly and the output is of ordinary length.

Proxy settings in the container runtime's own configuration (`proxies` in
`~/.docker/config.json`) are passed into every container by the `docker` command. Keep
credentials out of them.

In the install phase the network is open, so code that runs while installing, such as a
`setup.py`, can send out anything it can read. That is the files of the commit and nothing else.

## The Python Ecosystem

The Python Ecosystem installs with pip and tests with pytest. It works out the commands from the
files of the commit, without running any of them.

### Layouts covered

| Layout | Recognised by |
| --- | --- |
| A packaged project, with its packages at the root or under `src/` | At the root: a `pyproject.toml` with a `[project]` or `[build-system]` table, or a `setup.py`, or a `setup.cfg` |
| A project with requirements files | A `requirements.txt` at the root and no packaging |
| Plain modules | A `.py` file anywhere and neither of the above |

A commit that is none of these is answered with `Unsupported`, and a Claim that needed code run
in it is Inconclusive.

### What is installed

- pytest, always.
- The project itself and its dependencies, for a packaged project.
- What `requirements.txt` at the root lists.
- Test dependencies declared in a usual place: an extra or a dependency group named `test`,
  `tests`, `testing`, or `dev` in `pyproject.toml`, or a root file named `requirements-dev.txt`,
  `requirements-test.txt`, `dev-requirements.txt`, or `test-requirements.txt`.

The image is `python:<version>-slim`, for the newest of Python 3.14 to 3.10 that
`requires-python` in `pyproject.toml` allows.

### Known limits

- A lock file (`uv.lock`, `poetry.lock`, `Pipfile.lock`) is not read: versions are resolved
  afresh by pip.
- Extras declared only in `setup.py` or `setup.cfg` are not installed, because finding them would
  mean running the file.
- When `requires-python` allows none of the versions as they are written, such as `3.12`, the
  newest is used and the install fails with pip's own message. A bound on the patch release
  together with an upper bound, such as `~=3.12.4`, is one way to get there.
- A `requirements.txt` that pins hashes makes the install fail, because pytest and the project
  itself are installed alongside it without one.
- Test results are read from the summary pytest prints. A failed test whose name holds ` - ` is
  named only up to there, because that is also how pytest sets off the error's message.
- The summary is printed by a process the pull request's code runs in, so that code can write
  what it likes there. Results are Untrusted Content like the rest of the output.
