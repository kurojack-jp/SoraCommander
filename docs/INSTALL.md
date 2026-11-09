# Install

In addition to the author's normal environment, a fresh Windows Sandbox trial on
2026-11-09 (administrator account) reached setup, ChatGPT file reads and an
Owner-approved command with exit code 0. Python and Git were borrowed read-only
from the host, so installing Python from scratch was not tested. Another physical
PC, a separate Windows-user setup and paths with non-ASCII characters (including
Japanese) remain untested. This limited trial is not production acceptance.
If a step does not match what you see, please open an issue. Do not include your key.

## 1. Get the code and Python packages

Use a folder only you can write to, with **no spaces anywhere in the checkout
path**, for example `C:\SoraCommander`. The four launchers refuse a checkout
path containing a space with exit code 6, including with `--check`. Paths with
non-ASCII characters have not been verified; testing on another PC is pending.
In PowerShell, starting from `C:\`:

```powershell
git clone https://github.com/kurojack-jp/SoraCommander.git C:\SoraCommander
cd C:\SoraCommander
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install "mcp>=1,<2" "pydantic>=2,<3" "pywin32>=306"
```

The launchers read the code from this folder, so only these packages are installed. Do not use `pip install -e .`, because the package then appears twice and a release check refuses it.

## 2. Get the tunnel program

Download `tunnel-client` from OpenAI's Secure MCP Tunnel instructions, and put it here:

```text
tools\tunnel-client\v0.0.16\tunnel-client.exe
```

The launchers expect this exact path, so a newer version needs the path in the launchers changed. Only v0.0.16 has been tried.

## 3. Create a tunnel and a key

1. Create a tunnel at <https://platform.openai.com/settings/organization/tunnels>, and note its ID.
2. Create a runtime API key at <https://platform.openai.com/settings/organization/api-keys>.
3. Store the key. **The prompt does not show it. Do not put it in any other file.**

```powershell
powershell -ExecutionPolicy Bypass -File tools\Set-NextTunnelKey.ps1
```

## 4. Write the tunnel settings

Copy [sora-next.example.yaml](sora-next.example.yaml) to `%USERPROFILE%\.config\tunnel-client\sora-next.yaml`, then replace the three values marked in it.

### Allow your project folders

Create `%USERPROFILE%\.config\sora-commander\allowed-roots.txt` by hand if you
want ChatGPT's file tools to reach your projects. Use UTF-8, one absolute local
folder path per line, without quotes or environment-variable substitution:

```text
# Folders I allow
C:\Work\ProjectOne
C:\Work\ProjectTwo
read: C:\Work\ReferenceNotes
```

**Choose these folders carefully.** In every allowed folder, including
`read:` folders, the file tools read files without an approval window and
send their content to ChatGPT. Files are refused only by name (such as `.env`
or `credentials`), so passwords or connection strings inside files like
`appsettings.json`, `web.config` or `config.yaml` can be read. Do not allow
folders that hold such files.

An unprefixed entry grants file reads, creation, editing, moves, and command
working directories. An entry starting with the exact lowercase `read:` limits
**ChatGPT's file tools** to reads, searches, and listings there. Whitespace after `read:` is ignored;
other prefixes, including `READ:`, are invalid. A read-only folder cannot be a
file-write target or a command working directory. Full and read-only entries
must not name the same folder or contain one another, in either order.

Blank lines and lines starting with `#` are ignored. The file replaces the
default list; include the checkout explicitly if you also want it allowed.
Without the file, only the checkout has full access. Up to 16 folder entries in
total across both modes are
accepted. An existing empty/comment-only file or any invalid line refuses
startup; no invalid entry is silently skipped. Errors show only a line number
and a reason (line 0 means a file-level error). Broker startup reports only the
number of folders in each mode: `[INFO] Allowed folders: N full, M read-only for file tools`.
The same validation and rejected-folder rules below apply to both modes.

The list is read once when each process starts. Restart **both the Broker and
its client/tunnel** after editing it. No MCP tool or Broker message can read,
write or reload this configuration. The `tests` profile keeps its existing
tests-only scope and does not read the file.

Rejected folders include:

- Missing folders, files, symlinks/junctions/reparse paths, UNC paths and drive roots.
- Windows, Program Files (including the x86 folder), ProgramData and their children.
- The user profile itself, AppData and its children.
- Any folder containing this configuration file, `%USERPROFILE%\.config\tunnel-client`
  or `%LOCALAPPDATA%\SoraCommanderNext`; their private key/state folders are also excluded.
- Paths containing a name refused by `file_reads.py`, including hidden names,
  `temp`, `logs`, `runtime`, credential names and protected file suffixes.

Configuration files reached through links or with invalid UTF-8 are refused too.
The file is limited to 64 KiB. This list scopes file tools and command working
directories; it does not sandbox a command you explicitly approve. Even for a
read-only folder, an approved command runs with the Owner's Windows rights and
can write there by using an absolute path or changing its own working directory.
The `read:` setting cannot prevent those command writes; inspect each approval.

**`read:` protects a folder from ChatGPT's file tools only, not from commands you
approve.** If an approval window shows a path inside a read-only folder, read it
carefully before you approve.

### Choose the window language (optional)

Approval windows are shown in English or Japanese. By default they follow the
Windows display language: Japanese Windows shows Japanese, any other language
shows English. To choose yourself, create
`%USERPROFILE%\.config\sora-commander\language.txt` with one line, `en` or `ja`
(lowercase; lines starting with `#` are ignored). Any other content stops
startup with a `[HOLD]` message. The language is read at startup; restart the
Broker and its client after changing it. The Broker reports
`[INFO] Window language: ...` when it starts. The language changes only the
window text, never what is checked or approved.

## 5. Start it

Choose one mode. **Never run both at once.**

**Broker mode (recommended/default).** Jobs and approval windows live in a separate process, so they survive a tunnel restart. Double-click `tools\start-broker-mode.cmd` once. It opens the Broker in its own window, waits until the Broker pipe is present, then starts the tunnel. This checks pipe existence, not a Broker response. Keep both windows open. To stop, press `Ctrl+C` in the tunnel window first, then in the Broker window.

To restart only the tunnel, use `tools\start-sora-commander-broker-dev.cmd` while the Broker stays up.

Stay signed in. The approval window cannot open on a locked session.

**Optional usual mode:** double-click `tools\start-sora-commander-local.cmd` once,
and keep its window open. This mode has no Broker-mode duplicate-execution
protection after a restart. Never use a mode change to retry an unknown command.
The example YAML uses the Broker client; usual mode requires changing its command
to the same folder's `next-development.cmd` before starting it.

## 6. Connect ChatGPT

Follow OpenAI's latest [Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
([Japanese](https://help.openai.com/ja-jp/articles/12584461-connecting-custom-mcp-servers-in-chatgpt))
for eligibility, workspace permissions and connection steps. Use your Secure MCP
Tunnel as directed there; screen names and procedures can change. After connecting,
in a new chat ask for `ping`, `status`, `list_sessions` and `tool_catalog`.

Expected: `status` shows `remote_ready: false` and `approval: LOCAL_DEMO_ONLY`, and there are 44 tools.

ChatGPT's file tools reach the configured allowed folders (the checkout only if
the file is absent). **A command you approve runs with your own Windows rights,
and it can do anything your account can.**

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `A tunnel is already running or port 8080 is in use` | A tunnel is up, or something else holds the port | Do not start a second one |
| `A tunnel is running. Stop it first` | The Broker must start before the tunnel | Stop the tunnel, then start in the order above |
| `No key file` or `The key file must hold the key only` | The key is missing or malformed | Run step 3 again |
| `Development virtual environment not found` | `.venv` is missing | Run step 1 |
| `The folder path contains a space` (exit 6) | Unsupported checkout path | Move the checkout to a path without spaces, such as `C:\SoraCommander` |
| `allowed-roots line ...` | Invalid or unreadable local folder configuration | Correct the indicated line by hand, then restart both processes |
| A window you did not ask for | Possibly an old or repeated request | **Reject it** |
| `DENY_DUPLICATE_UNKNOWN` | The result of the same command is unknown, so it is refused | See below |

### Clearing a command with an unknown outcome

**Broker mode only:** if the Broker stops suddenly during a job, the same command stays refused. Usual mode does not provide this protection after a restart.

1. Check by hand what the command did. It may have run.
2. Stop the Broker.
3. In a console window, from this folder:

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m sora_commander_next.owner_broker_main --list-unknown
.venv\Scripts\python.exe -m sora_commander_next.owner_broker_main --resolve-unknown <ID>
```

Type `yes` when asked. Piped input is refused.

## Run the tests

```powershell
$env:PYTHONPATH = "src;."
$m = Get-ChildItem tests\test_*.py | ForEach-Object { "tests." + $_.BaseName }
.venv\Scripts\python.exe tests\run_with_real_dialog_guard.py @m
```

The public release includes only a subset of the development tests. This command
runs the test modules included in your copy, with a guard that fails any attempt
to open a real approval window; it is not the full development regression suite.
The tests do not need a tunnel and take a few minutes. Run them from a path with
no folder named `temp` and no folder starting with a dot, or some tests skip.
