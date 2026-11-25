# Native Team

Native Team lets people who work in several native coding-agent conversations share a task, ask another session to review it, and see who has acknowledged the result. The local MCP server keeps team state in SQLite. The browser workbench shows the same record across projects.

## Run it locally

Use Python 3.12 or newer. From the repository root, create a virtual environment.

```sh
python -m venv .venv
```

On Linux or macOS, activate it with `source .venv/bin/activate`. In Windows PowerShell, use `.\.venv\Scripts\Activate.ps1`. You can also run the executables directly from the environment's `bin` or `Scripts` directory.

On an Intel Mac, the pinned `cryptography` dependency needs Xcode command-line tools, a recent Rust compiler and OpenSSL. With Homebrew, install `openssl@3` and `rust` and set `export OPENSSL_DIR="$(brew --prefix openssl@3)"` before installing. Intel compatibility is experimental. Upstream ended support for this architecture in version 49. [Upstream changelog](https://cryptography.io/en/50.0.2/changelog/#v49-0-0) · [Build instructions](https://cryptography.io/en/50.0.2/installation/#building-cryptography-on-macos)

```sh
python -m pip install -c requirements.lock .
native-team migrate
native-team teams
native-team ui
```

On a new installation the team directory is empty. The last command opens the read-only workbench on the loopback address printed in the terminal. To open the page yourself, use `native-team ui --no-open`. Closing its terminal stops the server. The ledger remains on disk.

## Connect conversations

Configure each participating client's native MCP settings to launch the installed `native-team` executable over stdio. Use the full executable path if the client starts outside the activated environment. Give each client its matching arguments.

| Client | Server arguments |
| --- | --- |
| Codex | `mcp --client codex` |
| Antigravity CLI (agy) | `mcp --client agy` |
| Claude Code | `mcp --client claude` |

In each chosen conversation, call `team_join` with an alias and the same absolute project directory. Codex uses its own thread UUID. Claude binds its own session UUID to an approved lane such as `claude-art`; the dedicated project-pinned lane requires separate local setup. Credentials and approvals stay with the native client.

Once the sessions have joined, one peer calls `peer_team` with the participating aliases. Peers use `team_state`, `peer_sync` and `peer_ack` to receive shared context. They can send a `peer_message`, assign a task with `peer_task_create`, and record the review separately from receipt. An idle agy or Claude session can arm its own read-only `native-team wait --project … --alias … --mode actionable`. Work arriving while a client is closed remains available when it reconnects.

All participating clients must reach the same local state directory. The default is `.local/state/native-team` in the user's home. Put `--state PATH` before the subcommand to use a different private directory. On Windows choose an NTFS location with account-appropriate access controls.

## Review changes across rounds

After `native-team migrate`, the executor can include an optional `changes` object in
`peer_task_report`. Generate one for individual files in the task's declared write scope:

```sh
native-team task-manifest --project . --paths src/app.py src/menu.py
```

Add `--base HEAD` to identify additions and deletions relative to a commit. Without Git the
helper fingerprints existing files, including binaries. Reports accept up to 64 entries and
32 KiB of metadata. Include the complete selection for each round. Omitting the manifest
in a later report leaves that report's change list empty.

Open the task in the workbench and check the files you have reviewed. After a revised report,
a changed fingerprint reopens that file. These marks are personal to the browser profile
and origin. Use the same local UI port across restarts to keep them visible. Task acceptance
still requires the designated reviewer. The workbench reads submitted metadata from the ledger.

## Working in linked Git worktrees

Enroll from the root of an existing linked checkout. On an upgraded ledger, Native Team
recognizes the main repository by verifying Git's worktree metadata in both directions.
Each member retains its own checkout. In the same checkout overlapping write scopes block;
across linked checkouts the workbench and `native-team status` identify paths to reconcile
before merging. Changing checkouts requires releasing open writing claims and declared active
write scopes. Existing worktree-local project records keep their historical project identity.
Worktree creation, branch operations and merges remain with the developer.

## Test the release

The **Native Team compatibility** GitHub Actions workflow runs on push, pull request or manual dispatch. One build produces a Python wheel. Every test job checks its SHA-256, installs it into a fresh virtual environment and runs the same tests. No provider account or API key is required.

| Test job | Native runner |
| --- | --- |
| Linux x64 | `ubuntu-24.04` |
| Windows x64 | `windows-2025` |
| macOS Apple Silicon | `macos-15` |
| macOS Intel (experimental, source-built dependency) | `macos-15-intel` |

The suite exercises real MCP stdio subprocesses, a five-peer handoff, Claude session binding, wait/ack/reconnect behavior, lock release on process exit, and the installed workbench's HTML, JavaScript and API. A Windows host needs directory-symlink privileges to run the two tests for the optional project pin. Any such skips appear in the job log.

To reproduce one job locally, use an empty `dist` directory and run:

```sh
python -m pip wheel --no-deps --wheel-dir dist .
python scripts/check_release.py dist
```

Core inbox waiting uses inotify on Linux and checks the read-only ledger once per second when idle on Windows and macOS. Coordination uses forward-slash project-relative paths such as `src/ui`; on Windows and macOS it treats case and Unicode spelling variants as overlapping write reservations.

Use each Actions job's result for the commit you plan to run. Signed-in model turns, automatic startup at OS login and provider quota recovery require separate native acceptance. The current service installation and process-based agy quota-recovery integration are Linux-specific.

Released under the [MIT License](LICENSE).
