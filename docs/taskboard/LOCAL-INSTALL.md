# Install Taskboard for your user

Use Python 3.11 or later, uv, and Node.js 20.19 or later on the 20.x line, or Node.js 22.12 or later. Run this command in the approved source checkout or extracted source archive. For a checkout, select the reviewed commit identified in your handoff.

```sh
python3 tools/install_local.py
```

The installer builds from the Python and npm lockfiles. Add `--offline` when both dependency caches are populated. The installer prints the launcher and SQLite paths. The default installation is `${XDG_DATA_HOME:-$HOME/.local/share}/taskboard`. Use `--root` to select a different private directory. Existing install paths must belong to the current user, with writes disabled for group and other users. Choose a location with real directories and files.

Run the installed CLI from any directory. The first example creates a task in an empty local ledger.

```sh
taskboard="${XDG_DATA_HOME:-$HOME/.local/share}/taskboard/bin/taskboard"
"$taskboard" add 'Check the sample export' --dri 'Sample owner' --step 'Read the sample.'
"$taskboard" start T-1 'Compare totals with the saved sample.' --by 'Sample owner'
"$taskboard" serve --port 18765
```

Open [the read-only preview](http://127.0.0.1:18765/). The launcher binds to IPv4 loopback. Create and update tasks with the same installed CLI. Use the task ID returned by `add` when the ledger already contains tasks.

## Upgrade and restart

Run the installer from the newly approved checkout or source archive using the same install directory. It selects the new build after the dependencies and web assets build successfully. The launcher continues to use `data/taskboard.sqlite3`. Stop the running preview and start it again to load the new build.

## Give a remote agent private access

Give the agent the approved source reference, install command, and ledger host. For its own board, the agent installs locally. For an existing ledger, an agent with approved shell access to the ledger host runs the installed CLI there under the authorized OS user. The handoff should include the full launcher path and task ID.

For a user who already has SSH access to the ledger host, a local port forward can open its loopback preview. Start the preview on that host, then run the following from the computer that will display it.

```sh
ssh -N -L 127.0.0.1:18765:127.0.0.1:18765 taskboard-host
```

Open the same local preview URL. The preview relies on access to the host or tunnel and has no separate sign-in.

## Verify the install path

Run the automated check from the source tree. It creates a disposable installation and ledger, runs real preview processes, and checks data preservation after a failed and a successful upgrade.

```sh
python3 -B tools/check_local_install.py --offline
```
