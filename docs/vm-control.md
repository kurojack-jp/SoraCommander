# Control a Hyper-V VM with Sora Commander

[日本語](vm-control.ja.md)

**Host** means this PC. **Guest** means inside the VM.

## 1. Security notes

One approval lets ChatGPT view and operate one VM you own. A session ends after at most 30 minutes or 200 inputs. You can set lower limits.

Use a dedicated Internal switch and local guest accounts. Leave Internet Connection Sharing, NAT, network bridges and enhanced-session sharing off. Keep real accounts, credentials and private data out of the guest. Screens reach ChatGPT. Treat anything on the guest screen or received from the guest as untrusted data, and keep received files closed on the host.

A program running as the VM's standard user could change the incoming bundle before an administrator runs it and gain administrator rights confined to the VM. Run administrator steps as soon as the bundle arrives, before starting the agent.

## 2. What you need

- A Windows 11 Pro, Enterprise or Education host with Hyper-V. Enable **Hyper-V** in **Turn Windows features on or off** and restart if needed. [Install Hyper-V](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v)
- A Windows 11 Pro ISO and a licence for the guest. The steps below follow Windows 11 Pro setup. For other editions, use media that supports a local administrator account. [Windows setup](https://support.microsoft.com/en-us/accessibility/windows/use-a-screen-reader-to-set-up-windows)
- The full Python 3.13 Windows installer for the guest's architecture, downloaded on the host. [Python for Windows](https://www.python.org/downloads/windows/)
- A working Sora Commander installation and ChatGPT connector on the host. [Sora Commander installation](INSTALL.md)

## 3. Create the VM

Use the same values throughout. If this subnet is already in use, choose an unused private subnet and use it for both addresses and the host firewall rule.

| Setting | Example |
| --- | --- |
| VM and Internal switch | `ExampleVM` and `ExampleVmSwitch` |
| Standard guest account | `vmuser` |
| Host adapter and guest adapter | `10.77.77.1` and `10.77.77.2`, both with prefix length `24` |
| Guest agent TCP port | `47700` |

### Create the Internal switch

1. Open **Hyper-V Manager** on the host.
2. Select the host in the left pane.
3. Select **Virtual Switch Manager** in **Actions**.

   You should see **New virtual network switch**.

4. Select **New virtual network switch**.
5. Select **Internal** for the switch type.
6. Select **Create Virtual Switch**.

   You should see **Virtual Switch Properties**.

7. Enter `ExampleVmSwitch` in **Name**.
8. Select **OK**.

   You should see **Hyper-V Manager** again.

9. Open **Command Prompt** on the host.
10. Run `ipconfig /all`.

    Find `vEthernet (ExampleVmSwitch)`. Before assigning the static address, it may have a link-local address or no IPv4 address. If it has an address from your LAN router or a default gateway, stop and check that the switch type is **Internal**. An **External** switch connects the guest to the LAN.

### Set the host adapter address and block inbound connections

1. Open **Windows PowerShell** using **Run as administrator** on the host.
2. Set the host adapter address.

   ```powershell
   New-NetIPAddress -InterfaceAlias 'vEthernet (ExampleVmSwitch)' -IPAddress '10.77.77.1' -PrefixLength 24
   ```

   You should see the new host adapter address.

3. Block all inbound connections from the VM subnet on the host in the same PowerShell window.

   ```powershell
   New-NetFirewallRule -DisplayName "ExampleVM inbound block" -Direction Inbound -Action Block -RemoteAddress 10.77.77.0/24 -Profile Any
   ```

   You should see the new enabled inbound Block rule. Use your VM name and subnet if they differ from the example.

Without this rule, the guest reached the host's file sharing ports 135, 139 and 445 in the live run.

### Create the virtual machine

1. Select **New** in **Actions**.
2. Select **Virtual Machine**.

   You should see **New Virtual Machine Wizard** at **Before You Begin**.

3. Select **Next**.

   You should see **Specify Name and Location**.

4. Enter `ExampleVM` in **Name**.
5. To choose another location, select **Store the virtual machine in a different location**.
6. If you selected that option, choose the folder using **Browse**.

   You can use a local drive with at least 100 GB free. Keep the folder outside OneDrive.

7. Select **Next**.

   You should see **Specify Generation**.

8. Select **Generation 2**.
9. Select **Next**.

   You should see **Assign Memory**.

10. Enter `8192` MB in **Startup memory**. Use `4096` MB if the host is short on free memory.
11. Leave **Use Dynamic Memory for this virtual machine** as shown.
12. Select **Next**.

    You should see **Configure Networking**.

13. Select `ExampleVmSwitch` in **Connection**.
14. Select **Next**.

    You should see **Connect Virtual Hard Disk**.

15. Select **Create a virtual hard disk**.
16. Enter `64` GB in **Size**.
17. Select **Next**.

    You should see **Installation Options**.

18. Select **Install an operating system from a bootable image file**.
19. Choose your Windows ISO using **Browse**.
20. Select **Next**.

    You should see **Summary**.

21. Select **Finish**.

    You should see `ExampleVM` listed as **Off**.

Keep the VM off for the next steps. [Hyper-V wizard](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/create-a-virtual-machine-in-hyper-v)

### Set security and hardware before first start

1. Select `ExampleVM`.
2. Select **Settings** in **Actions**.

   You should see the VM settings tree.

3. Select **Security**.

   You should see **Secure Boot** and **Encryption Support**.

4. Select **Enable Secure Boot**.
5. Select **Microsoft Windows** in **Template**.
6. Select **Enable Trusted Platform Module**.
7. Select **Processor**.

   You should see **Number of virtual processors**.

8. Set **Number of virtual processors** to `2` or more.
9. Select **Integration Services**.

   You should see a list of services.

10. Select **Guest services**.
11. Select **Network Adapter**.

    You should see `ExampleVmSwitch` in **Virtual switch**.

12. Select **OK**.

    You should see the VM still listed as **Off**.

Note. **Guest services** is required for the host's manual copy into the VM.

### Install Windows and create the guest accounts

Run guest commands inside **Virtual Machine Connection**, with your VM name in the title. The terminal prompt normally shows the guest's local user folder, and the guest should have just one network adapter.

1. Select **Connect** for `ExampleVM` in **Actions**.

   You should see **Virtual Machine Connection**.

2. If the display-size dialog appears, close it with **×**.

   In a basic session, **Enhanced session** is unchecked in **View**. Close the dialog with **×** on reconnect too, so `vmuser` can sign in and enhanced-session sharing of host drives and the clipboard stays off.

3. Select **Start** in **Virtual Machine Connection**.

   You should see **Press any key to boot from CD or DVD**.

4. Press a key in the VM window.

   You should see **Windows 11 Setup**.

5. Select your language.
6. Select **Next**.

   You should see the keyboard selection on current installation media.

7. Select your keyboard layout.
8. Select **Next**.

   You should see the setup choices.

9. Select **Install Windows 11**.
10. Select the checkbox confirming that files on the new virtual disk will be deleted.
11. Select **Next**.

    You should see **Product key**.

12. Type your Windows product key yourself.
13. Select **Next**.

    You should see the licence terms for the edition selected by the key.

    A key accepted during setup only selects the edition at that point. Activation is separate and requires a network connection or an offline method such as phone activation where available. Keep the guest isolated. [Windows activation](https://support.microsoft.com/en-gb/windows/activation/activate-windows-online-or-offline-using-installation-and-confirmation-id)

14. Select **Accept**.

    You should see the disk selection.

15. Select **Disk 0 Unallocated Space**.
16. Select **Next**.

    You should see **Ready to install**.

17. Select **Install**.

    You should see installation progress, then Windows first-run setup after the restart.

18. Select your region.
19. Select **Yes**.

    You should see the keyboard layout prompt.

20. Select your keyboard layout.
21. Select **Yes**.

    You should see the option to add another layout.

22. Select **Skip** if you only need one layout.

    You should see the next first-run screen. Continue to **Let's name your device**.

23. Enter a device name in **Let's name your device**.

    Use up to 15 letters, digits and hyphens, with no personal information.

24. Select **Next**.

    After any restart, you should see the setup choices.

25. Select **Set up for work or school**.
26. Select **Next**.

    You should see the work or school sign-in screen.

27. Select **Sign-in options**.

    You should see **Domain join instead**.

28. Select **Domain join instead**.

    You should see the local account screen.

29. Enter a dedicated local administrator name in **Who's going to use this device?**
30. Select **Next**.

    You should see the password screen.

31. Enter a dedicated password.
32. Select **Next**.

    You should see the password confirmation.

33. Enter the password again.
34. Select **Next**.

    You should see the security questions.

35. Complete the security questions.
36. Choose your privacy settings on the following screens.

    You should see the Windows desktop for the local administrator.

37. Open **Windows PowerShell** using **Run as administrator** in the guest.
38. Run `net user vmuser * /add`.
39. Enter a dedicated password when prompted.
40. Enter the password again to confirm it.

    You should see that the command completed successfully.

41. Run `net localgroup Administrators`.
42. If the group is missing, run `net localgroup (Get-LocalGroup -SID 'S-1-5-32-544').Name` in the same PowerShell window.
43. Verify that `vmuser` is absent from the list of administrator group members.

Setup screen titles can vary with the ISO. Use media that offers the local account flow.

### Set the guest adapter address

1. Open **Windows PowerShell** using **Run as administrator** in the guest.
2. Run `Get-NetAdapter` in the guest.

   You should see the name of the guest adapter.

3. Run this command in the guest, substituting the actual adapter name for `Ethernet` if needed.

   ```powershell
   New-NetIPAddress -InterfaceAlias 'Ethernet' -IPAddress '10.77.77.2' -PrefixLength 24
   ```

   You should see the new guest adapter address.

4. Run `ipconfig /all` on the host.
5. Run `ipconfig /all` in the guest.

   You should see the example static addresses and no default gateway or DNS server on either dedicated adapter.

Note. Leave the default gateway blank and DNS automatic without manually entered servers on both dedicated adapters. If an address command fails, inspect that adapter with `ipconfig /all` before repeating it.

### Install Python in the guest

1. On the host, open a Python 3.13 release page from **Python for Windows** in section 2.
2. Look for **Windows installer (64-bit)** under **Files**.

   If the page only lists source archives, choose another 3.13 release with the installer.

3. Save the Windows installer as `python313.exe` in the host's Downloads folder.
4. Run this command from the host administrator's Windows PowerShell while the VM is running.

   ```powershell
   Copy-VMFile -Name ExampleVM -SourcePath "$env:USERPROFILE\Downloads\python313.exe" -DestinationPath 'C:\SoraFromHost\python313.exe' -FileSource Host -CreateFullPath
   ```

   You should see `C:\SoraFromHost\python313.exe` in the guest.

5. Open `C:\SoraFromHost\python313.exe` in the guest as the local administrator.
6. Select **Customize installation**.
   On **Optional Features**, the **for all users** checkbox next to **py launcher** is optional.

7. Select **Next**.

   You should see **Advanced Options**.

8. Select the **for all users** checkbox for Python itself on **Advanced Options**.
9. Set **Customize install location** to `C:\Program Files\Python313`, using the guest's actual Program Files drive.
10. Select **Install**.

   You should see **Setup was successful**.

11. Select **Close**.
12. Open the chosen installation folder, normally `C:\Program Files\Python313`, in the guest's **File Explorer**.

    You should see `python.exe` in that folder.

13. Run this command in the guest's Windows PowerShell.

    ```powershell
    & "$env:ProgramFiles\Python313\python.exe" -c "import ssl; print(ssl.HAS_PSK)"
    ```

    You should see `True`.

14. Sign out of the local administrator account in the guest.
15. If the display-size dialog appears, close it with **×**.
16. Sign in to the guest as `vmuser`.

    You should see the standard user's desktop.

Note. If copying stops early, check the VM state, **Guest services** and the destination before retrying. If Python setup stops, reopen the installer and use its **Repair** option if available before checking TLS-PSK again. [Python installer options](https://docs.python.org/3.13/using/windows.html)

## 4. Configure the host

1. Create a private local folder named `VmControl` under your Windows user profile.
2. Create an empty `send` folder inside it.
3. Create a separate empty `receive` folder inside it.
4. Save the following JSON as `%USERPROFILE%\.config\sora-commander\vm-control.json` in UTF-8, substituting your absolute paths.

```json
{
  "vm_name": "ExampleVM",
  "address": "10.77.77.2",
  "bind_local": "10.77.77.1",
  "port": 47700,
  "key_file": "C:\\Users\\YOUR_NAME\\VmControl\\vm-guest.key",
  "send_folder": "C:\\Users\\YOUR_NAME\\VmControl\\send",
  "receive_folder": "C:\\Users\\YOUR_NAME\\VmControl\\receive",
  "max_minutes": 30,
  "max_inputs": 200,
  "tools": ["screenshot", "wait", "elements", "click", "double_click", "move", "drag", "scroll", "type", "keypress", "file_put", "file_get"]
}
```

   You should see a file named `vm-control.json`, with no `.txt` extension.

5. To let ChatGPT's ordinary file tools read received files, add `read:C:\Users\YOUR_NAME\VmControl\receive` with your actual path to `%USERPROFILE%\.config\sora-commander\allowed-roots.txt`.

   After the next start, ChatGPT's ordinary file tools should be able to read files in the receive folder.

Note. JSON uses doubled backslashes and literal absolute paths in ordinary local folders. The file is limited to 4,096 bytes. Use all ten fields once. The port range is 1024–65535. Set minutes from 1 to 30 and inputs from 1 to 200. Keep `screenshot` in `tools` and omit capabilities you do not need. Keep the key and send folder outside all allowed roots. Keep a `read:` root separate from any full-access parent or child. Create missing allowed folders before restarting. Startup may stop if all entries are skipped. [Allowed folders and restart](INSTALL.md#allow-your-project-folders)

## 5. Prepare the guest bundle on the host

1. Open **Command Prompt** in the Sora Commander folder.
2. Run the bundle command.

   ```bat
   tools\vm-setup.cmd make-guest --guest-user vmuser --out "%USERPROFILE%\VmControl\vm-guest"
   ```

   You should see `Guest bundle:`, the SHA-256 list, `Guest key:` and `Host key:`.

3. Save a private copy of `vm-guest.json` separately for future maintenance.

   You should see the complete `vm-guest` bundle with `vm-guest.manifest.json`, plus the separate host key.

The output parent must already exist outside Sora Commander on a local drive. The final `vm-guest` folder and configured host key must be absent for first setup. Use the local user name only, up to 20 characters. Run the administrator command from the incoming folder in section 7. It checks that bundle against the manifest automatically.

### Restart a first setup that stopped before `install.cmd`

1. Confirm that `install.cmd` never ran for this guest and that the host key belongs only to this unfinished setup.
2. Delete the generated `vm-guest` folder on the host if it exists.
3. Delete the configured host key for this unfinished setup if it exists.
4. Rerun the first-setup `make-guest` command above with neither `--replace-key` nor `--update`.

Use the newly sent bundle in the guest. For an installed guest, use section 10. `--replace-key` is for rotating a key on an installed guest.

## 6. Install and use the host send shortcut

### Before reinstalling for a different VM name

1. Open **Run** on the host with **Windows+R**.
2. Run `shell:sendto`.
3. Delete the **Send to** shortcut for the previous VM.
4. Delete its desktop shortcut if it is present.

The shortcuts share one installed target and configuration. Reinstalling changes where even an old shortcut sends files.

### Install the shortcut

1. Open **Windows PowerShell** on the host in the Sora Commander folder.
2. Run `Get-ExecutionPolicy -List`.

   You should see the policy at each scope.

3. Run the installer in a Windows PowerShell process using `RemoteSigned`.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\vm_host\Install-SendToVm.ps1 -VmName ExampleVM
   ```

   You should see a Windows administrator prompt.

4. Approve the Windows administrator prompt yourself.

   You should see two `Created:` lines and `SHA-256`. The installer writes `Send-ToVm.ps1` and `send-to-vm.json` under `%ProgramFiles%\SoraVmTools` and creates shortcuts on the desktop and in **Send to**.

Note. Use a verified local distribution. If Windows marked the downloaded installer script as Internet content, review it first, then use **Properties** → **Unblock** if available and permitted. Honor any enforced execution policy. [Windows PowerShell execution policies](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1)

Restart from the installer command if elevation was canceled or installation stopped partway. Check the two installed files and both shortcuts first. Rerun with the same VM name after correcting the cause. Wait for both shortcut messages and the digest before sending.

### Send the bundle

1. Drop only the entire `vm-guest` folder onto **Send to ExampleVM** while the VM is running.

   You should see a host administrator prompt.

2. Approve the host administrator prompt yourself.

   You should see the destination VM and the file list.

3. Review the destination VM and file list.
4. Type `y`.
5. Press **Enter** to send.

   You should see `Done. The files are in … on the VM.`

6. Open the reported folder in the guest.

   You should see `C:\SoraFromHost\<yyyyMMdd-HHmmss>-<32-hex-suffix>\vm-guest`.

Note. Each attempt gets a new suffix. If the copy stops, inspect the reported partial folder. Leave it out of the setup steps, then resend the complete verified bundle and use the new destination. The VM key is sent only as part of one complete setup or rotation bundle.

## 7. Set up the guest agent

1. As `vmuser`, open `check.cmd` in the incoming bundle.

   You should see the correct VM name, `OK: standard user`, Python 3.13, TLS-PSK `True` and the configured guest IPv4 address.

2. Right-click the incoming `firewall-admin.cmd`.
3. Select **Run as administrator**.
4. Respond to the guest administrator prompt yourself.

   You should see exit code 0 and a verified inbound `SoraVmAgent` rule with a generated suffix.

Note. The rule permits TCP to the configured guest address and port from the host's Internal-switch address. Keep Windows Firewall enabled. The incoming manifest checks bundle completeness and detects mismatched listed files. The key has separate handling. Use the newly arrived bundle before starting the agent.

Restart at the firewall step after checking the printed rule name and scope if the command stops. It attempts to roll back a failed add. Confirm the rule is absent before retrying an add, or confirm the existing rule and its checks succeeded before continuing.

5. As `vmuser`, open `install.cmd` in the incoming bundle.

   You should see `OK: copied to`, exit code 0 and the key ACL showing only the guest user, SYSTEM and Administrators.

Restart at `install.cmd` if the command rolled back and `%LOCALAPPDATA%\SoraVmAgent` is absent. If the folder remains, run `check.cmd` and inspect the folder and key ACL first. If it is a partial first install, preserve needed evidence and clear the incomplete folder before rerunning. For a prior working install, use section 10.

6. Open `%LOCALAPPDATA%\SoraVmAgent`.
7. Open `netcheck.cmd`.

   Pass when every host port in the output shows `timed out`, the external TCP probes show `timed out` or `unreachable`, and the DNS lookup fails or times out.

Check the output even if the command exits successfully. Any host result other than `timed out`, external `connected` or `connection refused`, an indeterminate result or a successful DNS lookup needs investigation. Check the adapters, routes and the host block rule from section 3 before rerunning the probe.

To share guest output, take a screenshot of the VM window on the host while the output is visible. Keep shared clipboard access off.

8. Open `start-full.cmd` for the capabilities configured in `tools`, or `start-view.cmd` for screenshots and waiting.

   You should see the agent console. Full mode with file transfer enabled creates `%USERPROFILE%\SoraVmFiles\inbox` and `%USERPROFILE%\SoraVmFiles\outbox`.

9. Remove the incoming `vm-guest` folder from the guest after successful installation and agent startup.
10. Remove the prepared `vm-guest` folder from the host.

   You should still have the installed guest key, the host key and the separate maintenance copy of `vm-guest.json`.

Note. Keep the agent console and standard user's desktop open. The two temporary setup bundles contain a copy of the key.

## 8. Start the host with VM tools

### Broker mode

1. Stop the current tunnel with **Ctrl+C** in its window.
2. Run `tools\start-broker.cmd` on the host if the DEV Owner Broker is stopped.

   You should see the Broker ready.

3. Run `tools\start-vm-tunnel.cmd` from the Sora Commander folder.

   You should see one tunnel running with `tools\mcp-broker-client-vm.cmd`.

### Direct mode

1. Stop the current tunnel with **Ctrl+C** in its window.
2. Stop the Broker if it is running, following the installation guide.
3. Set the tunnel YAML `command` to the following, substituting your actual absolute Sora Commander path without spaces.

   ```yaml
   command: "cmd.exe /d /c C:/SoraCommander/tools/mcp-direct-vm.cmd"
   ```

4. Run `tools\start-usual-mode.cmd`.

   You should see one tunnel running with the direct VM launcher.

### ChatGPT connector

1. Refresh the Sora Commander connector's tools in ChatGPT.
2. Open a new chat if the tools did not refresh in the current chat.

   You should see these tools when the host is in development mode and `vm-control.json` is valid.

| Tool | What it does |
| --- | --- |
| `vm_session_start` | Requests approval for a VM session. |
| `vm_session_status` | Shows the session, limits and transfer progress. |
| `vm_look` | Takes a screenshot and can return permitted UI names. |
| `vm_act` | Sends guest input or waits. |
| `vm_file_list` | Lists names and sizes in the host send folder and guest inbox and outbox. |
| `vm_file_put` | Sends a named host file to the guest inbox. |
| `vm_file_get` | Fetches a named guest outbox file into the host receive folder. |
| `vm_file_status` | Checks progress by transfer ID. |
| `vm_file_cancel` | Requests cancellation by transfer ID. |

Note. Use one launch mode and keep its required windows open. [Host startup](INSTALL.md#5-start-it)

## 9. Check it works

1. Prepare a small plain-text sample in the host send folder.
2. Open **Notepad** on the guest desktop.
3. Ask ChatGPT for a guest screenshot before starting a session.

   You should see `VM_SESSION_REQUIRED`.

4. Ask ChatGPT to start a session with `vm_session_start`.
5. Review the VM name, permissions and limits in the host approval window.
6. Approve the session yourself.

   You should see **VM Lease** with a countdown and **Stop**.

7. Ask ChatGPT for one screenshot using `vm_look`.

   You should see the guest's **Notepad** window.

8. Ask ChatGPT to use `vm_act` once to click the editing area and type one sample word.

   You should see the word in **Notepad**.

9. Ask ChatGPT to send the sample using `vm_file_put`.

   You should see the file in the guest `inbox` and a completed transfer with a SHA-256 digest. Use `vm_file_status` with the same `transfer_id` to check progress.

10. Select **File** in Notepad.
11. Select **Save as**.

    You should see the file picker.

12. Enter a new file name with its full path in the guest `outbox`.
13. Select **Save**.

    You should see the new file in `outbox`.

14. Ask ChatGPT to fetch that file using `vm_file_get`.

    You should see the new file in the host receive folder and a completed transfer with a SHA-256 digest.

15. Compare each transfer digest with the SHA-256 of the corresponding sample on each endpoint using `Get-FileHash`.
16. Click **Stop** in the host's **VM Lease** window.

    You should see the lease window close.

17. Ask ChatGPT for one more screenshot.

    You should see the request refused until you approve a new session.

Note. Keep the host unlocked and stay at the PC. Use full mode with `click`, `type`, `file_put` and `file_get` for this check. Each file is limited to 1 GiB, each session has a cumulative transfer budget of 4 GiB, and the receiving volume must have room for the file plus 10 GiB. Use single file names in Unicode NFC, at most 120 characters and 120 UTF-16 code units. Avoid path separators, Windows reserved names and characters, control characters, leading or trailing spaces, and leading or trailing dots. Use only the named send, receive, inbox and outbox folders. **Stop** ends the host lease. **Ctrl+C** in the guest console also stops the agent when you are done.

## 10. Maintain or remove the guest

### Update code with the same settings

1. Click **Stop** in **VM Lease**.
2. Stop the guest agent with **Ctrl+C**.
3. Run the command in section 5 with `--update` appended.
4. Send the complete keyless bundle using section 6.
5. Open `update.cmd` in the incoming bundle as `vmuser`.

   You should see `OK: updated` and the copied file hashes. The installed folder now has the refreshed code.

6. Run `netcheck.cmd` from the installed folder and check the results against section 7.
7. Restart the agent from the installed folder after the check passes.
8. Delete the two update bundles after checking that the agent works.

Note. If update stops, inspect both folders and the printed status before restarting or repeating it. The old configuration and key must match the update and stay installed.

### Replace the key with the same settings

1. Click **Stop** in **VM Lease**.
2. Stop the guest agent with **Ctrl+C**.
3. Retain the old guest key securely for recovery.
4. Prepare the new key on the host using `make-guest` with `--replace-key`, `--previous-guest-config` pointing to the saved old `vm-guest.json` and `--previous-host-key` pointing to the current host key.

   You should see `Prepared host key:`. The old host key is still active.

5. Send the new bundle using section 6.
6. Open `remove.cmd` as `vmuser` from the current installed folder, or from a matching incoming bundle with the old settings.

   You should see the old installation removed.

7. Open `check.cmd` in the new incoming bundle as `vmuser`.
8. Open `install.cmd` in that bundle as `vmuser`.

   You should see the new guest installation and its key ACL.

9. On the host, run `tools\vm-setup.cmd activate-key` with the same `--guest-user`, `--previous-guest-config` and `--previous-host-key` used to prepare the new key.

   You should see `Activated host key:` and the retained old host key path.

10. Start the guest agent from the installed folder.
11. Verify the connection with a new approved session.
12. Remove the old key copies, rotation record and temporary bundles after verification.

Note. Reuse the existing firewall rule while settings match. If guest installation fails, keep the old host key and restore the guest using the old settings and guest key. If activation stops, keep the agent off and inspect the active, prepared and backup key paths printed by the command before retrying or restoring either side.

### Remove the feature or change settings

1. Click **Stop** in **VM Lease**.
2. Stop the guest agent with **Ctrl+C**.
3. Stop the host tunnel with **Ctrl+C**.
4. Prepare a keyless bundle with `--update` while the host still has the old settings.
5. Send that bundle to the old VM using section 6.
6. Right-click `firewall-remove-admin.cmd` in the newly arrived matching bundle.
7. Select **Run as administrator**.
8. Respond to the guest administrator prompt yourself.

   You should see exit code 0 and confirmation the matching rule was removed.

9. Open `remove.cmd` as `vmuser` from the matching bundle or the installed folder.

   You should see the guest agent removed.

10. Remove the old incoming bundles.
11. Preserve any guest sample files you need.
12. Remove the guest file folders for a full removal.
13. Shut down the old VM before removing its host block rule.
14. If retiring the VM or its subnet, open **Windows PowerShell** using **Run as administrator** on the host.
15. If retiring the VM or its subnet, remove the old host block rule using its unique display name from section 3.

   ```powershell
   Remove-NetFirewallRule -DisplayName "ExampleVM inbound block"
   ```

   Keep the rule if the VM still needs protection on the same subnet. For a different subnet, use section 3 to set up its host block rule before starting the VM again.

16. Remove the host's obsolete key and temporary bundles when recovery is no longer needed.
17. Remove the optional receive-folder line from `allowed-roots.txt` if it is no longer needed.
18. For a full removal, remove `vm-control.json`.
19. For a full removal, remove `%ProgramFiles%\SoraVmTools`.
20. For a full removal, remove both send shortcuts.
21. For a full removal in direct mode, restore the original tunnel YAML `command`.
22. For a full removal, start Sora Commander with your ordinary launcher.
23. For a settings change, start again at section 4 with the new values.
24. Refresh the ChatGPT connector's tools after host startup.

Note. Keep a copy of the old settings until cleanup succeeds. If removal stops, inspect the reported rule, remaining files and old configuration. Use the freshly received matching bundle to continue a partial removal. If the VM name changes, rerun the send shortcut installer in section 6 with the new name.

## 11. Troubleshooting

| Message or symptom | Where to restart |
| --- | --- |
| `[HOLD] Development virtual environment not found:` | Run `tools\setup.cmd` and check for `.venv\Scripts\python.exe`. |
| `[HOLD] The folder path contains a space.` or a required component is missing | Check the checkout path and required files in the installation guide. |
| A tunnel is already running or the tunnel port is in use | Stop the previous tunnel or resolve the port conflict before restarting section 8. |
| `vm-control.json` is missing, invalid or requires a DEV Host | Check the file name, encoding, paths and ten fields in section 4, then the launch mode in section 8. |
| Bundle output path or user name refused | Use a new local `vm-guest` folder outside the checkout and the local guest username. Inspect existing output and key paths before retrying. |
| Send script blocked by policy or `send-to-vm.json` is invalid | Check policy and reinstall the shortcut from section 6 with the intended VM name. |
| Reinstall reports that it could not copy the files | Check that you are using the current distribution. An older installer could stop while replacing existing files. Follow the reinstall checks in section 6. |
| The shortcut asks for a file after you dropped the whole bundle folder | Confirm that you dropped the complete `vm-guest` folder, then reinstall the shortcut from the current distribution as in section 6. An older send script could misread the dropped path. |
| VM missing, off or missing **Guest services** | Check the VM name, state and integration services. Inspect partial results in the exact `<yyyyMMdd-HHmmss>-<32-hex-suffix>` destination before using a fresh send. |
| `NG: administrator` from `check.cmd` | Sign in as the configured standard user. This direct check prints its own status. |
| `NG: use "Run as administrator".` or administrator verb refused from the installed folder | Use the appropriate administrator CMD in a complete, newly arrived bundle. |
| `NG: bundle hash mismatch:` or `NG: unexpected bundle file set.` | Prepare and send a fresh complete bundle. |
| Firewall rule already exists, a Block rule is present, or the check is inconclusive | Inspect **Windows Defender Firewall with Advanced Security** and the exact scoped rule before attempting any change. |
| `firewall-admin.cmd` cannot read inbound rules, reports no rule heading, or cannot verify the exact scope | Check the rule in the guest firewall, including whether it remains after a failed check. Older bundles had trouble reading Windows firewall output. For an unfinished first setup, use section 5 to prepare a fresh bundle from the current distribution. Follow section 7 to decide where to resume. |
| `netcheck.cmd` reports NG even though all host ports timed out and DNS failed | Check the external TCP results too. An older installation could misread the DNS result. For an installed guest, use `--update` and `update.cmd` as in section 10, then rerun the check from the installed folder. |
| Guest operation prints `Nothing was done.` or `Some changes may have been made. Run check.cmd and look at the folder before trying again.` | These are exception diagnostics. Inspect the reported folder or firewall rule before resuming. |
| `VM_SESSION_REQUIRED`, `VM_SESSION_ENDED` or `OWNER_SESSION_UNAVAILABLE` | Check `vm_session_status` and the host window. Keep the Owner desktop unlocked and obtain a new decision for a new session. |
| `DENIED`, `TIMED_OUT` or `PENDING` | Respect the Owner's decision. `EXPIRED` means no answer in time and `CLOSED` means the window was closed. For `PENDING`, check the original `pending_id` only. |
| `VM_UNREACHABLE` | Check the guest console, settings, adapter, inbound rule and matching keys before another attempt. |
| `PAUSED`, `UNKNOWN` or `OUTCOME_UNKNOWN` | Inspect the original result before taking another action. Check input with `vm_look` and transfers with the same `transfer_id`. Ask the Owner before resuming a paused transfer. |
