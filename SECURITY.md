# Security

Sora Commander lets an AI run commands on your PC. Please treat a problem in its approval or isolation as serious.

## Reporting a problem

**Do not open a public issue for a security problem.** Use [private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository.

Include what you did, what you expected, and what happened. Remove keys, private files and paths, tunnel IDs, IP addresses and device names from your report. This is a personal project, so there is no promised response time.

## What it is meant to protect against

- A command running without your approval, when the policy asks for a window
- In Broker mode only: the same command running twice after a restart when its outcome is unknown. Usual mode does not provide this protection after a restart.
- A remote caller approving its own request, or clearing a launch record
- Another Windows user reaching the Broker's local pipe

## What it does not protect against

- **Files in allowed folders.** File tools read any non-denied file without a window and can send it to ChatGPT. The deny list checks names, so files such as `appsettings.json`, `web.config` or `config.yaml` holding passwords or connection strings can be read. Do not allow folders that hold them.
- **Approved command output.** It goes to ChatGPT without being shown in the window.
- **A shared ChatGPT connector.** Other workspace members may be able to use the read tools. Do not share it. This OpenAI-side behaviour has not been verified.
- **A command you approved.** The window shows what will run. It cannot tell you whether that is wise.
- **Other programs under your own Windows account.** They can read the key file, edit the launch records, and change this code. Keep the checkout in a folder only you can write to.
- **Your account with OpenAI or ChatGPT.** While your connector is running, anyone who can use the account can read permitted files without a window. Requests subject to local review still require your decision. The optional logon task makes the connector available when you sign in.
- **A different command with the same effect.** The repeat check compares exact commands.

## Optional VM screen control

A Hyper-V VM is not a perfect wall. VM screenshots go to ChatGPT, and VM files can reach ChatGPT if you allow the receive folder to be read. Keep real accounts, credentials and private data out of the guest, and keep the guest off the Internet and your LAN. Treat everything shown on the VM screen and everything it sends as untrusted.

The host firewall blocks all inbound connections from the VM's subnet.

Create a separate key for each VM and use it only for that VM. The guest holds a copy, so software in the guest could read it and impersonate the agent. Delete the temporary copies after setup. The key does not grant approval on the host.

The host requires session approval and enforces the configured time and input limits, up to 30 minutes or 200 inputs. The always-on-top Stop window lets you end access early. Locking the host blocks VM operations while the Owner desktop is unavailable. VM file tools use only the configured host send and receive folders and the guest inbox and outbox. VM screen and input tools cannot see or control the host desktop, read the host clipboard or browse host files. ChatGPT's separately enabled Host tools still follow their own allowed folders and approval rules. [VM screen control guide](docs/vm-control.md)

## Your key

Your tunnel key is stored as plain text in `%USERPROFILE%\.config\tunnel-client\sora-next.key`. Do not paste it into a chat, a command line or an issue. If it may have leaked, revoke it at OpenAI and make a new one.
