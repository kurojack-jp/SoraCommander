# Security

Sora Commander lets an AI run commands on your PC. Please treat a problem in its approval or isolation as serious.

## Reporting a problem

**Do not open a public issue for a security problem.** Use [private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository.

Include what you did, what you expected, and what happened. Do not include keys or private files. This is a personal project, so there is no promised response time.

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
- **Your account with OpenAI or ChatGPT.** Anyone who controls it can send requests, though they still need your approval window.
- **A different command with the same effect.** The repeat check compares exact commands.

## Your key

Your tunnel key is stored as plain text in `%USERPROFILE%\.config\tunnel-client\sora-next.key`. Do not paste it into a chat, a command line or an issue. If it may have leaked, revoke it at OpenAI and make a new one.
