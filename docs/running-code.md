# Running code

Pull request code and Reproductions are run in one place only, the Sandbox. What is specific to a
language sits behind the Ecosystem boundary. What is kept between pull requests is the Repo
Profile and the installed dependencies. The contracts are the docstrings in
`src/pr_verification/sandbox.py`, `ecosystem.py`, `python_ecosystem.py`, and `repo_profile.py`;
this page says what is needed to use them and where their limits are.

## The Sandbox

The Sandbox is a local container (ADR 0001). It needs the `docker` command and a container
runtime on the same machine that answers it. Without one, making a `ContainerSandbox` raises
`SandboxFailed`.

Each run is a new container. What it is given:

- The files of one commit, as a working copy it may write to, and a home directory. Both are kept
  in a volume that belongs to that commit, so an install phase leaves what it installed for the
  test phase, and the commit before a pull request never sees what the commit after it left.
- Nothing else from the machine that started it: no environment variable, no socket of the
  container runtime, and no file but a home directory put back from an earlier run (see "What
  is kept between runs").
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

## What is kept between runs

Two things, each in a directory outside the repository that the caller names (ADR 0004). Both
refuse a directory that is the repository or lies inside it.

- **The Repo Profile**, in a `DirectoryStore`: the image, the install commands, the test command,
  the test framework, the test layout, and the lock files with a digest of their contents. It is
  one JSON file for each repository.
- **Installed dependencies**, in a `DependencyCache`: the home directory the install commands
  left, as one archive for each repository, image, and digest of the lock file.

`prepare_target` fills both, from the target commit only. A profile is saved once its install
commands and its test command have been run against that commit. It is used again while the
lock files hold the same contents, and learned again when they change or an install command
stops working. `prepare_pull_request` reads both and writes neither.

An archive is a tar file of the home directory. It is written and unpacked by `tar` running in
the Sandbox's own image, as the same user and under the same isolation as any command, with no
network. On the machine that started the Sandbox it is only ever a file that is passed along.

### Time saved on a repeat run

Measured on Flask at commit `d086db8`, on macOS with Docker Desktop 28.4, with the image already
fetched. Each run is `prepare_target` in a new Sandbox. Seconds, the median of three rounds,
with the lowest and highest in brackets.

| Run | Whole | Putting back | Installing | Saving | Proving the test command |
| --- | ---: | ---: | ---: | ---: | ---: |
| First run, nothing kept | 15.8 (13.4-17.5) | | 12.4 (9.2-12.4) | 1.3 (1.1-2.5) | 2.0 (1.9-2.0) |
| Repeat run | 6.2 (5.7-7.2) | 2.0 (1.8-2.3) | 4.1 (3.8-4.2) | | |
| Repeat run, cache emptied | 11.7 (10.4-14.4) | | 10.5 (9.0-13.1) | 1.0 (1.0-1.2) | |

- A repeat run takes 9.6 seconds less than a first run, which is 61% of it.
- The dependency cache saves 5.5 seconds: the repeat run against the one with the cache
  emptied. Putting the 131 MB archive back costs 2.0 seconds and takes 6.4 off installing.
- The Repo Profile saves the 2.0 seconds of the run that proves the test command, and
  discovery, which took under 0.05 seconds. The rest of the 4.1 seconds between the first run
  and the one with the cache emptied is installs from the network taking different times.

What the profile saves grows with the repository's test suite, and Flask's is short. One
repository on one machine is a small sample: the spread between rounds is as wide as some of
the differences.

### Known limits

- The image has to hold `sh`, `tar`, `chmod`, and `find`. The `python:<version>-slim` images
  do. With an image that does not, saving and putting back raise `SandboxFailed`.
- Saving and putting back have no time limit, and the size of an archive is not limited.
- Nothing is ever removed from the cache. Each change of the lock file leaves the archive for
  the old one behind, and a repository that is gone leaves its profile and its archives.
- A repository is told from another by the resolved path of its directory. One that is checked
  out somewhere else has no profile and no archive there, and learns again.
- An image is told from another by its name. An archive saved in `python:3.14-slim` is put back
  after that name has moved on to a newer build. The install commands are still run afterwards.
- The lock file is taken to be the files the Ecosystem names. A file that one of them includes
  is not followed, so a change there alone leaves the profile and the archive in use.
- A home directory holding something its own user may not read cannot be saved.
- When an archive cannot be put back, the home directory is left empty or part filled. From
  `prepare_target` the error goes on and the archive is deleted, so the next run starts clean.
- A profile is not written in one step. Two runs saving at once can leave a file that reads as
  no profile, and the next run learns it again. An archive does take its place in one step.

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

### What the Repo Profile keeps

- The test framework is `pytest`.
- The test layout is each directory that holds a file named `test_*.py` or `*_test.py`.
- The lock files are the root files that decide the image and the install commands, whether or
  not the commit holds them: `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, and
  the four requirements files for test dependencies named above.

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
