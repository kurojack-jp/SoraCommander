# Sora Commander

**Ask in an ordinary ChatGPT chat, and ChatGPT works on your Windows PC. You approve each step in a window on your screen.**

![Sora Commander demo: ask in ChatGPT, approve on your PC, see the result](docs/demo.gif)

[![Sora Commander MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/kurojack-jp/SoraCommander/badges/card.svg)](https://glama.ai/mcp/servers/kurojack-jp/SoraCommander)

There is no separate app or terminal to talk to. You chat with ChatGPT as usual, and it can read your files, run commands, check the results and keep going, in the same conversation:

- "Run the tests in my project folder and tell me what failed."
- "Find where this error message comes from, and show me the lines around it."
- "Start the build, and tell me when it finishes."

To use your own project folders in these examples, first configure the allowed
folders in [docs/INSTALL.md](docs/INSTALL.md). Without that file, only this checkout
is allowed. The list is read at startup; restart both the Broker and its client
after changing it.

**It saves the usage of your other tools.** The ChatGPT chat you already use does the reading, running and checking, so that work does not use up the limits of a separate coding agent. ChatGPT's own message limits still apply.

It also works in an ordinary chat in Instant mode — no separate app or mode to switch into. From December, ChatGPT and Codex share usage quotas on Plus/Pro/Business plans, but OpenAI says chat in Instant mode does not consume quota, so working here does not draw on your Work quota. That statement covers chat, not tool calls, so it is not a promise that every tool call is free.

Sora Commander is an MCP server for Windows. It connects to ChatGPT through OpenAI's Secure MCP Tunnel. Commands that are not plainly safe open an approval window on your PC, and they run only if you approve them. **So you need to be at the PC to approve them.** Reading files and checking results need no window.

> **v0.1 developer preview. Not accepted for production use.**
> In addition to the author's normal environment, a fresh Windows Sandbox trial on 2026-11-09 (administrator account) reached ChatGPT file reads and an Owner-approved command with exit code 0. Python and Git were borrowed read-only from the host; installing Python was not tested. This limited trial is not production acceptance. This software lets an AI operate your computer. Read each approval window before you click, and use it only if you can judge what a command does.

日本語の説明は[下](#日本語)にあります。

## What it does

- Runs commands and reports their output, exit codes and running jobs
- Reads and searches files inside the folder you allow
- Creates and edits supported text files inside allowed folders, after you check the proposed content and approve it in the local window
- Opens a local approval window for commands and edits. There is no remote approval
- In Broker mode only, refuses to repeat a command whose outcome is unknown, even after a restart, until you clear it by hand
- Fails closed when the window cannot open, the session is locked, or its records cannot be written

## New in v0.1.7

- Approval windows now distinguish Reject (`DENIED`) from no decision: X gives `TIMED_OUT`, `reason=CLOSED`; no answer in time gives `TIMED_OUT`, `reason=EXPIRED`. **X changed meaning: before this version, closing the window counted as a reject.** No decision means nothing ran or was written; ChatGPT reports and waits for your next instruction, without resubmitting or asking for the window again. A no-answer result is not a refusal or proof that you were away. Time limits are unchanged. Live-checked on 2026-11-13 with real windows through ChatGPT in Broker mode: command windows (Approve, Reject, X, no answer) and `create_file` (X, Approve). Other paths behave the same way in tests but have not been checked with real windows: no answer for `create_file` and the other DEV file tools, DEV `send_input`, the interactive second window, the text edit/create pilot, `run_task` and `ogf_cue`. These DEV checks do not establish production acceptance. See the INSTALL guide for scope and uncertain outcomes.
- `activity` answers which operations and IDs are known in the current run, without an approval window. It shows only IDs, states and times.
- **After updating, restart the Broker and its client, then refresh the tools in ChatGPT.** The DEV tool list with the optional edit/create pilot grew to 54 tools, and the tool help changed; the visible count depends on the Host configuration.

## New in v0.1.5

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

## Before you start

- Windows 11, signed in to your own account
- Python 3.13
- An OpenAI account that can create a Secure MCP Tunnel and a custom MCP connection

**Availability depends on your account and workspace permissions.** Follow the
current OpenAI help, [Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt),
for requirements and connection steps.

The tunnel program is not included. You get it from OpenAI.

## Install

See [docs/INSTALL.md](docs/INSTALL.md).

**Use Broker mode by default:** start `tools\start-broker-mode.cmd` once. It waits
for the Broker pipe to be present, then starts the tunnel. Usual mode is an
optional alternative and does not provide Broker mode's duplicate-execution
protection after a restart.

## Guided walkthrough (optional)

Sora Commander and the install docs in this repository are free.
If you would rather follow a step-by-step beginner's guide with
screenshots for every stage — including the common errors and how
to recover from them — a paid companion guide is available:

- English edition (Gumroad): https://kurojackson.gumroad.com/l/sora-commander-guide
- Japanese edition (note): https://note.com/kurojack_jp/n/nab0d17f9e491

## Limits

- `remote_ready=false` and `LOCAL_DEMO_ONLY` are what this version reports. They mean it is not accepted for production.
- The code is not signed and no installer is provided.
- It runs with your own Windows rights. Another program under your account can read the key file, and can edit the launch records.
- There is no automatic reconnect. If the PC restarts, you start it again by hand.
- Not tested: another physical PC, a separate Windows-user setup, installing Python from scratch, paths with non-ASCII characters (including Japanese), a power loss during a job, or plans other than the author's.

Report security problems as described in [SECURITY.md](SECURITY.md).

## License

The code is under the [GNU AGPL v3.0](LICENSE). You may use and change it for free. If you distribute it, or offer a changed version over a network, you must make your source available under the same license.

The Sora Commander name and character artwork are not covered. See [NOTICE.md](NOTICE.md).

There is no warranty. You are responsible for what you approve.

Sora Commander is an independent project. It is not made, endorsed or supported by OpenAI. ChatGPT and Sora are products of OpenAI.

---

## 日本語

**いつもの ChatGPT のチャットで頼むだけで、ChatGPT があなたの Windows PC で作業します。1 つずつ、画面の窓で承認します。**

![Sora Commander のデモ：ChatGPT で頼んで、PC で承認して、結果を見る](docs/demo.gif)

別のアプリやターミナルで話す必要はありません。普段どおり ChatGPT とチャットするだけで、同じ会話の中でファイルを読み、コマンドを実行し、結果を確かめて、次の作業に進めます。

- 「プロジェクトのフォルダでテストを流して、失敗したものを教えて」
- 「このエラーメッセージがどこから出ているか探して、前後の行を見せて」
- 「ビルドを始めて、終わったら教えて」

自分のプロジェクトを扱う例は、[導入手順](docs/INSTALL.ja.md)で許可フォルダを
設定した場合のものです。設定ファイルがなければ、このcheckoutだけを許可します。
変更は起動時に読み込み、反映にはBrokerとクライアント両方の再起動が必要です。

**他のツールの使用量を節約できます。** 読む、実行する、確かめるといった作業を、普段使っている ChatGPT のチャットが受け持つので、別のコーディング AI の使用量の上限を使わずに済みます。ChatGPT 自体のメッセージの上限はあります。

いつものチャット（Instant モードを含む）でそのまま使えます。12月から Plus/Pro/Business では ChatGPT と Codex で利用枠が共有されますが、OpenAI によれば Instant モードのチャットは枠を消費しないため、こちらの作業で Work の枠を使わずに済みます。ただしこの説明はチャットについてのもので、ツール呼び出しの扱いまでは書かれていないので、無料を断定するものではありません。

OpenAI の Secure MCP Tunnel で ChatGPT とつなぐ、Windows 用の MCP サーバーです。安全と言い切れないコマンドは PC の画面に承認窓を出して、承認したときだけ実行します。**承認するときは PC の前にいる必要があります。** ファイルを読むことや結果の確認には、窓は出ません。

許可フォルダの中の対応するテキストファイルを、承認窓で提案された内容を確認して承認した後に、ChatGPT が作成・編集できます。

> **v0.1 開発者向けプレビューです。実用としての受入はしていません。**
> 作者の通常環境に加え、2026-11-09に新しいWindowsサンドボックス（管理者権限のユーザー）で、ChatGPTからのファイル読取りとOwner承認付きコマンドの終了コード0まで試しました。PythonとGitはホストから読み取り専用で借りており、Pythonの導入は試していません。限定的な試行で、実用・本番の受入ではありません。AIにPCを操作させるソフトです。承認窓の内容を読んでから押してください。コマンドの意味を判断できる方だけ使ってください。

未試験：別の実PC、別のWindowsユーザーでの導入、Pythonの導入からの準備、日本語などASCII以外の文字を含むパス、ジョブ中の電源断、作者と異なるChatGPTプラン。

**必要なもの：** Windows 11、Python 3.13、Secure MCP Tunnel とカスタム MCP 接続を作れる OpenAI のアカウント。

**利用可否はアカウントとワークスペースの権限に依存します。**
接続はOpenAI公式ヘルプの最新の[「ChatGPT でのカスタム MCP サーバーへの接続」](https://help.openai.com/ja-jp/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)に従ってください。

**標準はBrokerモードです。** `tools\start-broker-mode.cmd`を1回起動します。
Brokerのパイプが用意できるまで待ってからトンネルを起動します。
結果が分からないコマンドを再起動後も繰り返さない機能はBrokerモードだけです。
補足の通常モードには、この二重実行の防止がありません。

## v0.1.7 の新機能

- 承認窓でReject（`DENIED`）と未決定を区別します。Xは`TIMED_OUT`, `reason=CLOSED`、時間内の無回答は`TIMED_OUT`, `reason=EXPIRED`です。**Xの意味が変わり、以前のバージョンの「閉じると却下」から未決定になりました。** 未決定なら何も実行・書込みされず、ChatGPTは再送や窓の再表示を要求せず、報告して次の指示を待ちます。無回答は拒否でも離席の証拠でもありません。制限時間は変わりません。2026-11-13にBrokerモードでChatGPTを通じた実際の窓を確認しました。確認済みはコマンド窓（Approve、Reject、X、無回答）と`create_file`（X、Approve）です。他の経路はテストでは同じ動作ですが、実際の窓では未確認です。未確認の範囲は`create_file`と他のDEVファイルツールの無回答、DEV `send_input`、対話型の2つ目の窓、テキスト編集・作成pilot、`run_task`、`ogf_cue`です。このDEV確認は本番受入を意味しません。対象範囲と結果不明の場合は導入手順を参照してください。
- `activity`は現在の実行で分かっている操作とIDを承認窓なしで確認し、ID・状態・時刻だけを表示します。
- **更新後はBrokerとそのクライアントを再起動し、その後ChatGPTのツールを再読み込みしてください。** 任意の編集・作成pilot付きDEV構成のツール一覧は54ツールに増え、ツールヘルプも変わりました。表示数はHost構成によります。

v0.1.5の任意の編集・作成pilot付きDEV構成では53ツールです。表示数はHost構成によります。`git_snapshot`（状態）、`git_diff`（差分）、
`git_log`（履歴）、`git_show`（コミットや過去のファイル）、`git_remote_check`
（HTTPS upstreamとの比較）は承認窓なしで読み取ります。hooks・filters・設定された
外部プログラムは無効です。通信するのはremote確認だけで、認証の入力窓とcredential helperも無効です。
`list_tasks`はOwnerが書いたプリセットを窓なしで一覧表示し、`run_task`は毎回承認後に
固定コマンドを実行します。`tasks.json`の形式は導入手順にあります。
`read_image_file`は許可された読取りフォルダのPNG/JPEG（4 MiB以下）を窓なしで読み、
ChatGPT側で画像itemに`image(item)`を呼ぶ必要があります。
`tools/scn_capture_window.py`の単一ウィンドウ撮影は、別途承認するタスクプリセット用です。
`ogf_cue`は連携アプリのBridge inboxへ制限された演出cueを送り、毎回承認が必要です。
Ownerが`%USERPROFILE%\.config\sora-commander\ogf-bridge.json`を作らなければ無効です。

導入は [docs/INSTALL.ja.md](docs/INSTALL.ja.md) を見てください。

**ライセンス：** コードは [GNU AGPL v3.0](LICENSE) です。無料で使えて、変更もできます。配布するときや、変更版をネットワーク越しに提供するときは、同じライセンスでソースを公開する必要があります。Sora Commander の名前とキャラクター画像は対象外です。詳しくは [NOTICE.md](NOTICE.md) を見てください。

保証はありません。承認した操作の責任は、利用者にあります。

Sora Commander は個人の独立したプロジェクトで、OpenAI の製品ではありません。OpenAI が作ったり、推奨したり、サポートしたりしているものでもありません。ChatGPT と Sora は OpenAI の製品です。
