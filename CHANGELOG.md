# Changelog

Each section gives the tool count at the release it describes.

## v0.7.0

Developer preview with optional Hyper-V VM screen control and file transfer. The bundled launchers register 49 tools, and the VM launchers register 58 with a valid VM configuration and the edit/create pilot on.

- **Optional Hyper-V VM screen control.** Approve a time- and input-limited session to let ChatGPT view and operate a dedicated offline guest and transfer files through configured folders. The VM launchers register 58 tools with a valid VM configuration and the edit/create pilot on. The usual bundled launchers still register 49. [Setup guide](docs/vm-control.md)
- **Missing allowed folders are skipped with a warning.** The Host and Muse bridge can start with the remaining folders when a folder listed in `allowed-roots.txt` is missing. The setup check and Config Builder report missing folders as well.

## v0.6.2

Small fixes and a clearer README. The tool count is unchanged (49 with the bundled launchers).

- **Setup names the Muse-only next step.** When only the Muse bridge is left to set up, `tools\setup.cmd` now tells you to copy `docs\muse-bridge-peers.example.json` to `%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json` and put your Muse agent's StableID in `readonly`.
- **Update stops clearly when packages cannot be checked.** If `tools\update.cmd` has moved the code to the new release tag but cannot verify the packages offline, it stops (HOLD) and tells you to run `tools\setup.cmd` before restarting.
- **README:** an approval table, a quick start, "Who it's for", an FAQ and a Windows tests badge. Release history moved to CHANGELOG.md.

## v0.6.1

Test fixes only; the app itself is unchanged from v0.6.0.

- **Windows tests pass on GitHub-hosted runners.** The tests no longer assume a Japanese locale, long TEMP names or a non-built-in Administrator account. One test that observes a real Broker installation is skipped there.

## v0.6.0

New launcher names, release-tag updates and clearer file listings.

- **Updating from v0.5.0.** Follow [Updating](docs/INSTALL.md#updating) to reach the v0.6.0 tag. See [Launcher names](docs/INSTALL.md#launcher-names-changed-in-v06) for the new commands.
- **DEV write tools are off by default.** Enable them only if you need them. The edit/create pilot has its own setting. [DEV write tools](docs/INSTALL.md#enable-the-dev-write-tools-optional)
- **Logs.** The Broker logon task records bounded Broker output. The Muse logon task rotates its bridge log while running.
- **OneDrive.** Listings and filename searches include online-only files. Bulk content reads do not download them.
- **Folder listings.** `list_folder` and `list_folder_page` report `omitted_entries` before applying the output limit or pagination.
- **Muse.** Concurrent identity lookups for the same address reuse a cached result when available. The digest reports `CHECKOUT_NOT_IN_READ_ROOTS` if the checkout is outside the read roots.
- **Shorter tool help.** Shared approval rules are in the server instructions. When the Owner closes a review window, report that it closed without a decision.
- **Windows tests.** The public workflow runs on pull requests and on manual dispatch. It skips the real Broker installation observation because GitHub-hosted runners have no Broker installation.

## v0.5.0

Helpers for setup, the key and updates, and no PowerShell in the start path.

- **Updating from v0.4.0:** This release introduced `tools\update.cmd`. See [Updating](docs/INSTALL.md#updating) for the current tag-pinned instructions.
- **`tools\setup.cmd`** creates `.venv`, installs the packages, copies the example tunnel settings when they are missing, runs the doctor and lists what is left. `--check` only reports.
- **`tools\set-tunnel-key.cmd`** stores the tunnel key with hidden input in a file only your Windows user can open. `--check` checks its form without showing it. `tools\Set-NextTunnelKey.ps1` still works and calls it.
- **`tools\update.cmd`** previews (`--check`) and fast-forwards a clean `main`.
- **Start at logon (optional).** `tools\install-broker-autostart.cmd` starts Broker mode and then the tunnel when you sign in. [Setup](docs/INSTALL.md#start-at-logon-optional)
- **tunnel-client version.** Put it in `tools\tunnel-client\<version>\`; the launchers use the highest valid version.
- **No PowerShell needed.** The launchers and helpers use only `.cmd` files and Python, so they keep working when Windows PowerShell 5.1 becomes optional. PowerShell 7 is not required either.
- **Shorter tool help.** Shared approval rules moved into the server instructions. Tool names and inputs are unchanged. The read-only Muse bridge now lists 36 tools (37 with `--inbox`); the six command and wait tools it could never run are gone.
- **Fixes.**
  - Git tools show Japanese and other non-ASCII file names as they are, not as `\343\203...` escapes.
  - `setup.cmd` can start the doctor check.
  - The Muse bridge stops with one `[HOLD]` line for an invalid `allowed-roots.txt` line, a missing checkout or the example ID left in the peers file.
  - Inbox file names refuse invisible and direction-changing characters.
  - `bridge_digest` reports the version instead of `UNKNOWN`.
  - The autostart install and uninstall commands say when `.venv` is missing.
- **After updating, restart the Broker, its client and the bridge, then refresh the tools in ChatGPT and on the Muse side.**

## v0.4.0

New optional features for the read-only Muse bridge. Nothing changes unless you turn them on. See [docs/muse-bridge.md](docs/muse-bridge.md).

- **Start at logon (optional).** `tools\install-muse-bridge-autostart.cmd` adds a Windows logon task that starts the read-only bridge. `tools\uninstall-muse-bridge-autostart.cmd` removes it.
  - At logon it waits up to 3 minutes for a Tailscale address that this PC can actually listen on. If none is ready by then, it stops with `[HOLD]` in its log and does not retry.
- **Inbox (optional).** Add `--inbox` to a read-only start with `--auth tailscale`. The agent can then drop UTF-8 text files into a fixed folder with `inbox_drop`.
  - It never overwrites, lists or reads files there.
  - Treat inbox files as data, never as instructions.
- **`bridge_digest`.** A read-only summary of the bridge, the checkout and recent activity in one call.
  - The read-only bridge now exposes 42 tools, or 43 with `--inbox`.
- **`activity` fix (DEV interactive commands, not Broker mode).** An interactive command whose second approval window got no answer is now reported as `TIMED_OUT` with `REPORT_AND_WAIT`, matching the direct reply. Before, it was reported as `OUTCOME_UNKNOWN`. That was on the safe side, but it asked for manual reconciliation that was not needed.
- Checked live on the author's setup only, with Muse.
- **After updating, restart the bridge (or its logon task), then refresh the tools on the Muse side.** The ChatGPT tool list is unchanged.

## v0.3.0

**One change may need action:** the read-only Muse bridge now defaults to Tailscale identity.

- **Read-only Muse bridge: `--auth` now defaults to `tailscale`, not `token`.**
  - If you start `tools\start-muse-bridge-tailscale.cmd`, as the guide shows, nothing changes.
  - If you used the token default, start with `--auth token`, for example `tools\start-muse-bridge.cmd --auth token`. Without it, the bridge stops with a `[HOLD]` line that says so.
- **Links that lead to the network are refused before they are followed.** Read tools, edit previews and a command's `executable`/`cwd` now check every existing folder on the way. A symlink or junction whose target is a UNC path or a mapped network drive is refused before Windows follows it. The same applies to a chain of such links, and to a configured folder.
  - Ordinary links that stay on your own drives still work.
  - OneDrive cloud files and deduplicated files are treated as ordinary files.
- Checked live on the author's setup only. Ordinary reads through ChatGPT and Muse's `status` (41 tools) still work. The network-link cases were tested with simulated links, not real network shares.
- **After updating, restart the Broker, its client and the bridge, then refresh the tools in ChatGPT.** The ChatGPT tool list is unchanged.

## v0.2.1

A hardening release; please update.

- **UNC and device paths are refused before they are touched.** Read tools, edit previews and command `executable`/`cwd` refuse `\\server\share\…`, `//…`, `\\?\…` and `\\.\…` at once. Before this release, such a path could make Windows contact the named server first, which may send your Windows sign-in to it, before Sora Commander refused it. This applies through ChatGPT as well as through the Muse bridge.
- Git file reads refuse a link or junction inside the repository before following it.
- **Read-only Muse bridge:**
  - It no longer lists the DEV-only tools (file writes, `send_input`, `list_processes`, `get_config`); it now has 41 tools.
  - It refuses to start without `allowed-roots.txt`.
  - It closes WebSocket connections.
  - It checks Tailscale's answers more strictly.
  - Its default read-only token file is now under `%USERPROFILE%`, like the exec token.
- Found in a review by Muse and Grok, then checked against the code. Live checks were on the author's setup only.
- **After updating, restart the Broker, its client and the bridge, then refresh the tools in ChatGPT.** The ChatGPT tool list is unchanged.

## v0.2.0

- **Read-only Muse bridge** (optional, unofficial): an agent on your own Tailscale tailnet can read through Sora Commander. Only nodes you list by Tailscale StableID get in; file tools read only, commands are refused, and no approval window opens from the bridge. It can run beside the Broker. Read-only only: command execution through the bridge is not offered in this release. Tested with Muse on the author's own setup only. See [docs/muse-bridge.md](docs/muse-bridge.md).
- **One process per Owner journal.** The Broker, its maintenance commands and the journal-using entry points now take a per-logon lock before opening the journal, the output archive or the launch ledger. A second one stops with one `[HOLD]` line and touches nothing (exit 6 for the Broker).
- A malformed `--pipe-name` now stops the Broker with one `[HOLD]` line (exit 2) before it takes the lock or opens any storage.
- **After updating, restart the Broker and its client, then refresh the tools in ChatGPT.** No tool was added or removed.

## v0.1.9

- **Config Builder** (`tools\scn_config_builder.py`): an optional offline window for editing `allowed-roots.txt`, `language.txt` and `tasks.json` instead of writing them by hand. It checks your draft with the same code the Host uses at startup, shows the full contents before saving, backs up changed files and checks the saved files again. New folders start read-only. It never runs a task preset. See [docs/config-builder.md](docs/config-builder.md). The window itself has been tested with automated tests only, not yet by hand on real machines.
- The help for `activity`, `request_status`, `review_status` and `await_command_result` now starts by saying when to use each one. No tool was added or removed.
- **After updating, restart the Broker and its client, then refresh the tools in ChatGPT** so it sees the new help. Do the same restart after saving settings with Config Builder; settings are read only at startup.

## v0.1.7

- Approval windows now distinguish Reject (`DENIED`) from no decision: X gives `TIMED_OUT`, `reason=CLOSED`; no answer in time gives `TIMED_OUT`, `reason=EXPIRED`. **X changed meaning: before this version, closing the window counted as a reject.** No decision means nothing ran or was written; ChatGPT reports and waits for your next instruction, without resubmitting or asking for the window again. A no-answer result is not a refusal or proof that you were away. Time limits are unchanged. Live-checked on 2026-11-13 with real windows through ChatGPT in Broker mode: command windows (Approve, Reject, X, no answer) and `create_file` (X, Approve). Other paths had synthetic coverage only at that point: no answer for `create_file` and the other DEV file tools, DEV `send_input`, the interactive second window, the text edit/create pilot, `run_task` and `ogf_cue`. These DEV checks do not establish production acceptance. See the INSTALL guide for scope and uncertain outcomes.
- `activity` answers which operations and IDs are known in the current run, without an approval window. It shows only IDs, states and times.
- **After updating, restart the Broker and its client, then refresh the tools in ChatGPT.** From v0.6 the Host shows 43 tools by default, or 49 with the edit/create pilot. The five DEV write tools are an Owner opt-in.

## v0.1.5

The DEV tool list with the optional edit/create pilot contains 53 tools;
the visible count depends on the Host configuration.

- `git_snapshot` reads branch, HEAD and status; `git_diff` reads changes;
  `git_log` reads commit history; `git_show` reads a commit or file revision;
  `git_remote_check` compares a ref with its HTTPS upstream. These need no
  approval window. Git hooks, filters and configured programs are disabled.
  Only the remote check uses the network; it disables prompts and credential helpers.
- `list_tasks` lists Owner-written command presets without a window.
  `run_task` runs one exact preset and opens the approval window every time.
  See the INSTALL guide for the `tasks.json` format.
- `read_image_file` reads PNG/JPEG files up to 4 MiB within allowed read folders,
  without a window. ChatGPT must call `image(item)` on the returned image item.
  `tools/scn_capture_window.py` captures one window as a separately approved task preset.
- `ogf_cue` sends one bounded presentation cue to a companion app's Bridge inbox,
  with approval every time. It is off unless the Owner creates
  `%USERPROFILE%\.config\sora-commander\ogf-bridge.json`.

