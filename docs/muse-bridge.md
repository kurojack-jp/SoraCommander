# Read-only Muse bridge

An unofficial bridge lets an agent on your own Tailscale tailnet read through
Sora Commander. Windows only; the PC must stay on. File tools read only,
commands are refused, and no approval window opens from the bridge. It can run
beside the Broker. This release offers read-only access only.

## Setup

1. Prepare the Python environment in [INSTALL](INSTALL.md). Install and run
   Tailscale on the PC, with the agent's node in the same tailnet.
2. Copy [muse-bridge-peers.example.json](muse-bridge-peers.example.json) to
   `%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json`.
   Run `tailscale status --json` locally and find the agent node's `ID` field
   (its StableID, ending in `CNTRL`). Replace the example entry in `readonly` with that value; keep `exec`
   an empty list. Do not share the status output or your peers file.
3. From the checkout, start `tools\start-muse-bridge-tailscale.cmd` and leave
   its window open. Connect the agent to the Tailscale address and `/mcp`
   endpoint shown in that window, on port 8765.

File access follows your allowed folders, all demoted to read-only for this
process. See INSTALL for folder setup. Restart the bridge after changing its
settings. Stop it with Ctrl+C.

### Using it with Muse only

You do not need ChatGPT, the tunnel program or a tunnel key. From INSTALL, do
only step 1 (code and Python packages) and "Allow your project folders" in
step 4; skip steps 2, 3, 5 and 6. Then:

- Install Tailscale for Windows from https://tailscale.com/download and sign
  in. Add the agent to the same tailnet as its own help describes.
- To list the nodes your PC can see with their StableIDs, run in PowerShell:

  ```powershell
  tailscale status --json | ConvertFrom-Json |
    ForEach-Object { $_.Peer.PSObject.Properties.Value } |
    Select-Object HostName, @{n='StableID';e={$_.ID}}
  ```

  Copy only the agent's StableID into the peers file. Keep the output to
  yourself.

### When it does not connect

| What you see | Likely cause | What to do |
|---|---|---|
| `Could not determine the Tailscale IPv4 address` | Tailscale is not running on the PC | Start Tailscale, then the bridge again |
| The bridge window closes or prints a peers error at start | The peers file is missing, empty or not valid JSON, or `readonly` is empty | Copy the example again and put one StableID in `readonly` |
| The agent gets HTTP 401 | Its node is not in `readonly`, or it is not in your tailnet | Check the StableID; restart the bridge after editing the file |
| A command returns `DENY` | Expected: commands are refused in read-only mode | Nothing; use the read tools |
| `Invalid tool request` for a command | The request is malformed, for example `executable` is not an absolute path | Nothing runs either way; commands are refused in read-only mode |

## Refusals

A missing, empty or malformed peers file, or an empty `readonly` list, refuses
startup (`tests.test_muse_bridge_identity.StartupTests.test_missing_invalid_empty_lists_never_listen`
and `PeerFileTests.test_schema`). Tailscale not running can prevent address
detection and startup; if a fallback address lets startup continue, identity
lookup still fails closed. A missing CLI, lookup failure or node absent from
the list answers HTTP 401
(`tests.test_muse_bridge_identity.IdentityTests.test_all_lookup_refusals_are_401`
and `test_timeout_negative_cache`). Successful identity lookups are cached for
up to 60 seconds; failed lookups for 5 seconds.

Read-only policy, command refusal and the absence of an approval dispatcher are
covered by
`tests.test_muse_bridge_exec.MainTests.test_default_readonly_host_and_banner_are_preserved`.
These are synthetic tests, not acceptance of another user's setup.

## Limits

Tested on the author's own setup only. Whether another account's Muse can join
its own tailnet the same way is not yet confirmed. Muse is a Meta product;
this project is not affiliated with or endorsed by Meta. As of November 2026,
the plan terms of ChatGPT and Muse can change. Production and public-release
safety gates remain HOLD; `remote_ready=false`.
