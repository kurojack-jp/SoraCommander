# Sora Commander Config Builder

An offline Owner companion for `allowed-roots.txt`, `language.txt` and
`tasks.json`. Windows and Python 3.13 with Tcl/Tk are required. It uses only
the standard library and the included Sora Commander validation modules.

From the Sora Commander folder, open the editor:

```powershell
py -3.13 -B tools\scn_config_builder.py
```

1. **Allowed folders.** Add a folder using the picker. New entries are
   read-only. Select an entry to change its mode or remove it. Full access
   requires a warning confirmation. Disable the setting to use the checkout
   default. An enabled empty list is invalid. The validation panel shows line
   numbers and reasons. Comments are retained when changing entries.
2. **Window language.** Choose Windows default, English or Japanese.
   Windows default schedules deletion of an existing `language.txt`.
3. **Task presets.** Select a task or use the npm/pytest template. Set a project
   cwd, executable, description and arguments, then apply the form to JSON.
   One argument occupies one line. Use the editable JSON for multiline or
   single-empty-string arguments. JSON is validated as you edit; unapplied
   form changes block saving. Templates contain `cmd.exe /d /s /c` and only
   prepare a preset. The editor never runs it. A cwd outside full-access
   folders produces a warning. Disable task settings to remove `tasks.json`.
4. **Review and save.** Review the full contents of all three files and line
   addition/removal counts. Acknowledge the read-access warning, then confirm
   the explicit WRITE/DELETE list. Any draft change requires a fresh review.
   Saving backs up existing changed files as `*.bak-YYYYMMDD-HHMMSS`, verifies
   the backup bytes, then atomically replaces each changed file. A backup
   name collision stops the save. It never overwrites an earlier backup.
5. Read the post-save startup-validation result. Restart the Broker and its
   client by hand. In usual mode, restart the Host. Restart the Muse bridge
   if it uses the allowed folders.

**Files in allowed folders can be read by ChatGPT without an approval window.**
Do not allow folders containing secrets. Read-only applies to file tools.
Approved commands run with the Owner's permissions and may still write there.
The editor only touches these three settings and their backups/temporary files
under `%USERPROFILE%\.config\sora-commander`. It can create missing settings
directories after confirmation. It does not access tunnel keys or YAML.

The UI selector switches Japanese/English independently of `language.txt`.
Core diagnostic reason strings may appear in English.

## Read-only check

```powershell
py -3.13 -B tools\scn_config_builder.py --check
```

Outputs a `[PASS]`, `[WARN]` or `[HOLD]` status per setting and an extra `[WARN]`
line per skipped folder. Exit 0 means validation completed without HOLD.
Exit 1 means at least one HOLD. No files, directories, caches or GUI are created.
It checks actual startup readers and refuses linked/non-ordinary config paths.

## Recovery and limits

- Close other configuration editors before saving. External changes since
  loading are refused. Cooperating Builders use an exclusive temporary lock.
- Replacement is atomic **per file**. The three files are not a single
  transaction. On failure, the editor reports completed files and backups,
  retains `.config-builder.lock` and any remaining staged files, and stops.
  Inspect these files and verify no Builder is saving before manually resolving
  the lock. There is no automatic rollback, cleanup retry or approval retry.
  Close and reopen the editor after manual reconciliation.
  A changed lock or an error removing it is also a stopped save, even if some
  settings have already been written; check the reported completed files first.
- Local software acting as the same user is not isolated by this utility.
  Startup validation and post-save checks do not establish a security boundary
  against a malicious concurrent process or power-loss durability for the set.
- Invalid UTF-8 or oversized existing files stop GUI loading to prevent data
  loss. Correct those files manually first. Ordinary invalid text can be fixed
  by replacing root entries, selecting a language or editing tasks JSON.
- Roots and tasks allow UTF-8 BOMs and at most 64 KiB. Language allows 256
  bytes. Same-mode root overlap is permitted by core; cross-mode overlap is
  rejected. The editor warns about folders that do not exist and can save the
  list if at least one usable entry remains. After creating a skipped folder,
  restart the processes that use the list to include it.
- Filesystem-save tests, GUI/device acceptance and non-ASCII-path acceptance
  remain pending. Passing in-memory validation/controller tests does not establish
  that Windows file replacement or the visible GUI has been accepted.

## Offline package

```powershell
py -3.13 -B tools\build_config_builder.py
```

Creates `artifacts/config-builder/config-builder.zip` with the editor, the matching
core validators, licenses, both guides, EN/JA quickstarts and a SHA-256 file manifest. Existing output
folders are refused. Extract the whole ZIP into a dedicated folder before
launching. When no roots file exists, **that extracted folder** is the checkout
default shown by Builder; configure explicit allowed folders before using the
package with another Sora Commander checkout. Retest against that checkout.
The script does not publish or install anything. Config Builder and this script
are part of the public source. The ZIP stays on your PC; distributing the ZIP
itself requires a separate Owner release decision.
