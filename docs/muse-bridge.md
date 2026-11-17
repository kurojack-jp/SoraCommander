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
  yourself. The Tailscale admin console's machine details show the same ID.
  Either way it ends in `CNTRL`.

### On the Muse side (as Muse describes it; not checked by the author)

1. Ask Muse in chat to join your Tailscale network. It shows an approval link.
2. Open the link and approve the device named `muse` for your tailnet. Muse
   says the link stays valid for five days.
3. Put the `muse` device's StableID in `readonly`, start the bridge, then ask
   Muse to connect to your PC's Tailscale address. Muse may ask you once to
   allow that address.

Connections go one way, from Muse to the PC. Muse does not answer ping, so
judge by whether a `status` call works.

### Limit what the agent can reach (recommended)

A new tailnet starts with an allow-all access policy, so once Muse joins it
can reach every port on every device in the tailnet, not only the bridge. In
the Tailscale admin console, under Access controls, limit it to the bridge
port. If your tailnet holds only this PC and Muse, a policy like this is
enough (use the Tailscale addresses shown in the Machines list):

```json
{
  "hosts": {
    "my-pc": "100.64.0.1",
    "muse": "100.64.0.2"
  },
  "grants": [
    { "src": ["muse"], "dst": ["my-pc"], "ip": ["tcp:8765"] }
  ]
}
```

This replaces the default allow-all rule. If other devices share the tailnet,
keep or add the rules they need, or they lose access to each other. Check the
result in the admin console before saving. See Tailscale's documentation on
grants and policy syntax for the exact rules.

### Removing access

Delete the `muse` device in the Tailscale admin console, and remove its
StableID from `muse-bridge-peers.json`, then restart the bridge.

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
