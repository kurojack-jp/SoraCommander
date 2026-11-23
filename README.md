# Sora Commander

**An open-source MCP server for Windows that lets ChatGPT read files and run approved commands on your PC, from an ordinary chat.** Reads inside folders you allow need no approval. Commands that are not plainly safe, and file edits, run only after you click **Approve** in a window on your PC.

[![Sora Commander MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/kurojack-jp/SoraCommander/badges/card.svg)](https://glama.ai/mcp/servers/kurojack-jp/SoraCommander)
[![Windows tests](https://github.com/kurojack-jp/SoraCommander/actions/workflows/tests.yml/badge.svg)](https://github.com/kurojack-jp/SoraCommander/actions/workflows/tests.yml)

![Sora Commander demo: ask in ChatGPT, approve on your PC, see the result](docs/demo.gif)

There is no separate app or terminal to talk to. You chat with ChatGPT as usual, and it can read your files, run commands, check the results and keep going, in the same conversation:

- "Run the tests in my project folder and tell me what failed."
- "Find where this error message comes from, and show me the lines around it."
- "Start the build, and tell me when it finishes."

To use your own project folders in these examples, first configure the allowed
folders in [docs/INSTALL.md](docs/INSTALL.md). Without that file, only this checkout
is allowed. The list is read at startup; restart both the Broker and its client
after changing it.

Under the hood it is a local MCP (Model Context Protocol) server. ChatGPT reaches it through OpenAI's Secure MCP Tunnel, so you do not open a port to the internet.

**About usage limits.** ChatGPT's own limits still apply. The clip below was recorded while the Codex 5-hour limit showed 0%, and that request still ran on the author's setup. It is not a promise about how any limit counts tool calls.

![On ChatGPT Plus with the Codex 5-hour limit at 0%, ChatGPT asks Sora Commander to create a file, the Owner approves it on the PC, and the file appears](docs/demo-5h-limit.gif)

The demo uses `create_file`, which needs `dev-write-tools.txt` set to `on` from v0.6. With the edit/create pilot, `request_text_create` also shows the full content in the approval window.

*26 seconds, Japanese captions. On ChatGPT Plus with the Codex 5-hour limit at 0%, ChatGPT asks Sora Commander to create a file, you approve it in the window on your PC, and the file appears in Explorer. Recorded on the author's PC on 2026-11-21. It shows that this request still ran, not that tool calls never count toward any limit.*

> **v0.6 developer preview. Not accepted for production use.**
> In addition to the author's normal environment, a fresh Windows Sandbox trial on 2026-11-09 (administrator account) reached ChatGPT file reads and an Owner-approved command with exit code 0. Python and Git were borrowed read-only from the host. A second fresh Sandbox trial on 2026-11-21 installed Python 3.13 and Git from their official installers, without PowerShell 7, and `tools\setup.cmd` and the launch checks passed, also from a Japanese checkout path; it did not start a tunnel. These limited trials are not production acceptance. This software lets an AI operate your computer. Read each approval window before you click, and use it only if you can judge what a command does.

> **Check your ChatGPT plan first**
>
> - Custom MCP connections are web-only. OpenAI lists full MCP for Business and Enterprise/Edu. [OpenAI Help Center](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) (checked 2026-11-20).
> - The same page's FAQ says Pro can connect custom MCP servers for read/fetch only, so commands and edits would not be available there.
> - The author uses Sora Commander on **Plus with developer mode**, including approved commands, as of November 2026.
> - OpenAI's page does not list Plus. This setup can change at any time.
> - Muse's read-only bridge is an alternative that needs Tailscale and the Python setup. It requires no ChatGPT plan.

日本語の説明は[下](#日本語)にあります。

## How approval works

| What ChatGPT asks for | What happens |
| --- | --- |
| Read or search files inside the folders you allow | Runs without a window |
| Check a command's output, status or running jobs | Runs without a window |
| Create or edit a supported text file (edit/create pilot, on with the included launchers) | The window shows the full proposed content. It is written only if you approve |
| A command that is not plainly safe | An approval window opens on the PC. It runs only if you approve |
| DEV write tools (`create_file`, `create_directory`, `move_file`, `request_source_patch_dev`, `send_input`) | **Off by default since v0.6.** If you turn them on, each use opens the window |
| File tools on paths outside the allowed folders | Refused |

- There is **no remote approval**. You need to be at the PC, signed in, with the screen unlocked.
- An approved command runs with your own Windows rights.
- While the connector runs, anyone who can use your ChatGPT account can read permitted files without a window. Do not allow folders that hold passwords or keys. See [SECURITY.md](SECURITY.md).
- If a window appears that you did not ask for, click **Reject**.

The optional [read-only Muse bridge](docs/muse-bridge.md)
([日本語](docs/muse-bridge.ja.md)) lets an agent on your own tailnet read through
Sora Commander on a Windows PC that stays on; commands are refused and no
approval window opens from the bridge. Tested on the author's own setup only;
whether another account's Muse can join its own tailnet the same way is not yet
confirmed. Muse is a Meta product; this project is not affiliated with or
endorsed by Meta. As of November 2026, ChatGPT and Muse plan terms can change.
The bridge code also contains an unfinished exec mode for development. It is
unsupported for general use. Every command it receives still opens the approval window.

## What it does

- Runs commands and reports their output, exit codes and running jobs
- Reads and searches files inside the folder you allow
- Creates and edits supported text files inside allowed folders, after you check the proposed content and approve it in the local window
- Opens a local approval window for commands and edits. There is no remote approval
- In Broker mode only, refuses to repeat a command whose outcome is unknown, even after a restart, until you clear it by hand
- Optionally starts ChatGPT Broker mode when you sign in to Windows. [Setup instructions](docs/INSTALL.md#start-at-logon-optional)
- Fails closed when the window cannot open, the session is locked, or its records cannot be written

### Registered tools

| Configuration | Registered tools |
| --- | ---: |
| ChatGPT, started with the included launchers (Broker mode or usual mode; the edit/create pilot is on) | 49 |
| ChatGPT Host with DEV write tools on | 48 (54 with the edit/create pilot) |
| ChatGPT Host with both opt-ins off | 43 |
| Muse read-only bridge | 36, or 37 with `--inbox` |

## New in v0.6.2

Small fixes and a clearer README. The tool count is unchanged (49 with the bundled launchers).

- **Setup names the Muse-only next step.** When only the Muse bridge is left to set up, `tools\setup.cmd` now tells you to copy `docs\muse-bridge-peers.example.json` to `%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json` and put your Muse agent's StableID in `readonly`.
- **Update stops clearly when packages cannot be checked.** If `tools\update.cmd` has moved the code to the new release tag but cannot verify the packages offline, it stops (HOLD) and tells you to run `tools\setup.cmd` before restarting.
- **README:** an approval table, a quick start, "Who it's for", an FAQ and a Windows tests badge. Release history moved to CHANGELOG.md.

Earlier releases: see [CHANGELOG.md](CHANGELOG.md).

## Before you start

- Windows 11, signed in to your own account
- Python 3.13 with the `py` launcher (3.14 is not tested yet)
- Git for Windows in its default folder, `C:\Program Files\Git`
- For ChatGPT: OpenAI's `tunnel-client` and an OpenAI account that can create a Secure MCP Tunnel and a custom MCP connection
- No PowerShell install is needed

**Availability depends on your account and workspace permissions.** Follow the
current OpenAI help, [Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt),
for requirements and connection steps.

The tunnel program is not included. You get it from OpenAI.

## Quick start (Windows 11)

1. **Get the code and set it up** in a folder path with no spaces:
   ```bat
   git clone https://github.com/kurojack-jp/SoraCommander.git C:\SoraCommander
   cd C:\SoraCommander
   tools\setup.cmd
   ```
   `setup.cmd` creates `.venv`, installs the packages, copies the example tunnel settings if missing, runs the doctor check and lists what is left.
2. **Get OpenAI's `tunnel-client`** from the [Secure MCP Tunnels guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) and put it in `tools\tunnel-client\<version>\tunnel-client.exe`.
3. **Create a tunnel and a runtime key**, then store the key with hidden input: `tools\set-tunnel-key.cmd`.
4. **Edit** `%USERPROFILE%\.config\tunnel-client\sora-next.yaml` (tunnel ID, key file path, checkout path). Optionally list your folders in `%USERPROFILE%\.config\sora-commander\allowed-roots.txt`; without it, only this checkout is allowed.
5. **Start Broker mode** with `tools\start-broker-mode.cmd`. It waits for the Broker pipe, then starts the tunnel. Connect the tunnel in ChatGPT, and ask for `status` and `tool_catalog` in a new chat. Expect `remote_ready: false`, `approval: LOCAL_DEMO_ONLY` and 49 tools.

Optional: `tools\install-broker-autostart.cmd` (start at sign-in), `dev-write-tools.txt` set to `on` (DEV write tools) and `tools\update.cmd` (update to the latest release tag). Full steps and troubleshooting: [docs/INSTALL.md](docs/INSTALL.md) ([日本語](docs/INSTALL.ja.md)).

Usual mode (`tools\start-usual-mode.cmd`) is an optional alternative and does not provide Broker mode's duplicate-execution protection after a restart.

## Guided walkthrough (optional)

Sora Commander and the install docs in this repository are free.
If you would rather follow a step-by-step beginner's guide with
screenshots for every stage — including the common errors and how
to recover from them — a paid companion guide is available:

- English edition (Gumroad): https://kurojackson.gumroad.com/l/sora-commander-guide
- Japanese edition (note): https://note.com/kurojack_jp/n/nab0d17f9e491

## Who it's for

- **Good fit:** developers on Windows 11 who want ChatGPT to read their project and run tests or builds, and who will read each approval window.
- **Not a fit:** macOS or Linux only; production machines or folders with important data; anyone who wants approvals from a phone (there is no remote approval).

## FAQ

**Does it work with other MCP clients?** It is built and tested for ChatGPT through the Secure MCP Tunnel, plus the read-only Muse bridge. Other clients are not tested.

**Does it work on macOS or Linux?** No. It is Windows-only.

**Is it free?** Yes, under the AGPL v3.0. An optional paid walkthrough guide is listed above.

**Can I use it in production?** No. v0.6 is a developer preview and reports `remote_ready=false`.

**Can Muse operate my PC?** Not through the read-only bridge. It refuses commands.

## Limits

- `remote_ready=false` and `LOCAL_DEMO_ONLY` are what this version reports. They mean it is not accepted for production.
- The code is not signed and no installer is provided.
- It runs with your own Windows rights. Another program under your account can read the key file, and can edit the launch records.
- There is no automatic reconnect. After a restart, start Broker mode again by hand, or install the optional logon task, which starts it once at your next sign-in. A failed start or an exited process is not retried.
- Tested with synthetic Windows tests only: Japanese and accented names in `allowed-roots.txt` (UTF-8, with or without a BOM), the file read tools, local Git history and the doctor check.
- Tested in a fresh Windows Sandbox only: installing Python 3.13 and Git from scratch, `tools\setup.cmd`, and the launch checks from an ASCII and a Japanese checkout path.
- Not tested: a live start from a checkout path with non-ASCII characters (including Japanese), real links with such names, another physical PC, a separate Windows-user setup, a power loss during a job, or plans other than the author's.

Report security problems as described in [SECURITY.md](SECURITY.md).

## License

The code is under the [GNU AGPL v3.0](LICENSE). You may use and change it for free. If you distribute it, or offer a changed version over a network, you must make your source available under the same license.

The Sora Commander name and character artwork are not covered. See [NOTICE.md](NOTICE.md).

There is no warranty. You are responsible for what you approve.

Sora Commander is an independent project. It is not made, endorsed or supported by OpenAI or Meta, and is not affiliated with either. ChatGPT and Sora are products of OpenAI. Muse is a product of Meta.

---

## 日本語

> **まず ChatGPT のプランを確認**
>
> - カスタム MCP 接続は Web 版のみです。OpenAI はフル MCP の対象として Business と Enterprise/Edu を案内しています。[OpenAI ヘルプ](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)（2026-11-20 確認）。
> - 同じページの FAQ では、Pro はカスタム MCP を読み取り・取得の権限でのみ接続できます。コマンドや編集は使えない見込みです。
> - 作者は 2026 年 11 月時点で、**Plus の開発者モード**で Sora Commander を使い、承認付きコマンドも実行しています。
> - このヘルプページに Plus の記載はありません。この環境で使えるかどうかはいつでも変わり得ます。
> - 読み取り専用の Muse ブリッジも使えます。Tailscale と Python のセットアップが必要で、ChatGPT のプランは不要です。
> - ブリッジのコードには、開発中の exec モードも入っています。一般利用はサポート対象外です。受け取ったコマンドは、すべて承認窓を出します。

**いつもの ChatGPT のチャットで頼むだけで、ChatGPT があなたの Windows PC で作業します。許可したフォルダの読み取りは承認なしで動きます。安全と言い切れないコマンドとファイルの編集は、PC の窓で Approve したときだけ動きます。**

![Sora Commander のデモ：ChatGPT で頼んで、PC で承認して、結果を見る](docs/demo.gif)

別のアプリやターミナルで話す必要はありません。普段どおり ChatGPT とチャットするだけで、同じ会話の中でファイルを読み、コマンドを実行し、結果を確かめて、次の作業に進めます。

- 「プロジェクトのフォルダでテストを流して、失敗したものを教えて」
- 「このエラーメッセージがどこから出ているか探して、前後の行を見せて」
- 「ビルドを始めて、終わったら教えて」

自分のプロジェクトを扱う例は、[導入手順](docs/INSTALL.ja.md)で許可フォルダを
設定した場合のものです。設定ファイルがなければ、このcheckoutだけを許可します。
変更は起動時に読み込み、反映にはBrokerとクライアント両方の再起動が必要です。

**使用量について。** ChatGPT 自体の上限はかかります。下の録画は Codex の5時間枠が0%のときに撮ったもので、作者の環境ではその依頼が動きました。ツール呼び出しがどの上限にどう数えられるかを約束するものではありません。

![ChatGPT Plus で Codex の5時間枠が0%のまま、ChatGPT から Sora Commander にファイル作成を頼み、PC で承認するとファイルができる様子](docs/demo-5h-limit.gif)

デモの `create_file` はv0.6から `dev-write-tools.txt` に `on` の設定が必要です。編集・作成pilotの `request_text_create` でも、承認窓に全文が表示されます。

*26秒、日本語字幕。ChatGPT Plus で Codex の5時間枠が0%の状態で、ChatGPT から Sora Commander にファイル作成を頼み、PC の窓で承認すると、エクスプローラーにファイルができます。2026-11-21 に作者の PC で録画しました。この依頼が動いたことを示すもので、ツール呼び出しがどの枠も使わないことまでは示していません。*

OpenAI の Secure MCP Tunnel で ChatGPT とつなぐ、Windows 用の MCP サーバーです。安全と言い切れないコマンドは PC の画面に承認窓を出して、承認したときだけ実行します。**承認するときは PC の前にいる必要があります。** ファイルを読むことや結果の確認には、窓は出ません。

許可フォルダの中の対応するテキストファイルを、承認窓で提案された内容を確認して承認した後に、ChatGPT が作成・編集できます。

> **v0.6 開発者向けプレビューです。実用としての受入はしていません。**
> 作者の通常環境に加え、2026-11-09に新しいWindowsサンドボックス（管理者権限のユーザー）で、ChatGPTからのファイル読取りとOwner承認付きコマンドの終了コード0まで試しました。PythonとGitはホストから読み取り専用で借りていました。2026-11-21には、PowerShell 7 のない新しいサンドボックスで Python 3.13 と Git を公式のインストーラーから入れ、`tools\setup.cmd`とランチャーの起動前の確認が、日本語を含むcheckoutのパスでも通りました。トンネルは起動していません。限定的な試行で、実用・本番の受入ではありません。AIにPCを操作させるソフトです。承認窓の内容を読んでから押してください。コマンドの意味を判断できる方だけ使ってください。

合成テストだけで確認したもの：`allowed-roots.txt`（UTF-8、BOMの有無どちらも）の日本語・アクセント付きの名前、ファイル読み取りツール、ローカルのGit履歴、doctorの診断。

新しいWindowsサンドボックスだけで確認したもの：Python 3.13 と Git の導入からの準備、`tools\setup.cmd`、ASCIIと日本語のcheckoutのパスでのランチャーの起動前の確認。

未試験：日本語などASCII以外の文字を含むcheckoutのパスでの実際の起動、そうした名前の実際のリンク、別の実PC、別のWindowsユーザーでの導入、ジョブ中の電源断、作者と異なるChatGPTプラン。

**必要なもの：** Windows 11、Python 3.13（`py`ランチャー付き。3.14 はまだ試していません）、既定のフォルダ`C:\Program Files\Git`に入れた Git for Windows。ChatGPT で使うときは、OpenAI の`tunnel-client`と、Secure MCP Tunnel とカスタム MCP 接続を作れる OpenAI のアカウント。PowerShell のインストールは要りません。

**利用可否はアカウントとワークスペースの権限に依存します。**
接続はOpenAI公式ヘルプの最新の[「ChatGPT でのカスタム MCP サーバーへの接続」](https://help.openai.com/ja-jp/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)に従ってください。

### 承認のしくみ

| ChatGPT が頼むこと | どうなるか |
| --- | --- |
| 許可したフォルダの中のファイルを読む・探す | 窓なしで動きます |
| コマンドの出力・状態・実行中のジョブを確かめる | 窓なしで動きます |
| 対応するテキストファイルの作成・編集（編集・作成 pilot。同梱のランチャーではオン） | 窓に提案の全文が出ます。承認したときだけ書き込みます |
| 安全と言い切れないコマンド | PC に承認窓が出ます。承認したときだけ動きます |
| DEV 書き込みツール（`create_file`、`create_directory`、`move_file`、`request_source_patch_dev`、`send_input`） | **v0.6 から既定でオフです。** オンにした場合も、使うたびに窓が出ます |
| 許可したフォルダの外へのファイル操作 | 断ります |

- **遠隔での承認はありません。** PC の前でサインインし、画面のロックを解いておく必要があります。
- 承認したコマンドは、あなたの Windows の権限で動きます。
- コネクタが動いている間は、ChatGPT のアカウントを使える人なら、許可したフォルダのファイルを窓なしで読めます。パスワードや鍵のあるフォルダは許可しないでください。詳しくは [SECURITY.md](SECURITY.md) を見てください。
- 頼んでいない窓が出たら、**Reject** を押してください。

### クイックスタート（Windows 11）

1. **コードを取ってきて準備します。** フォルダのパスに空白を入れないでください。
   ```bat
   git clone https://github.com/kurojack-jp/SoraCommander.git C:\SoraCommander
   cd C:\SoraCommander
   tools\setup.cmd
   ```
   `setup.cmd` は `.venv` を作り、パッケージを入れ、トンネル設定の見本がなければコピーし、doctor で診断して、残りの作業を一覧にします。
2. **OpenAI の `tunnel-client` を入手します。** [Secure MCP Tunnels のガイド](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)から取ってきて、`tools\tunnel-client\<version>\tunnel-client.exe` に置きます。
3. **トンネルとランタイムキーを作り**、キーを画面に出さずに保存します：`tools\set-tunnel-key.cmd`。
4. **`%USERPROFILE%\.config\tunnel-client\sora-next.yaml` を編集します**（トンネル ID、キーファイルのパス、checkout のパス）。任意で、使うフォルダを `%USERPROFILE%\.config\sora-commander\allowed-roots.txt` に書きます。書かなければ、この checkout だけを許可します。
5. **`tools\start-broker-mode.cmd` で Broker モードを起動し**、ChatGPT でトンネルをつなぎます。新しいチャットで `status` と `tool_catalog` を頼み、`remote_ready: false`、`approval: LOCAL_DEMO_ONLY`、49 ツールと出れば準備完了です。

任意：`tools\install-broker-autostart.cmd`（サインイン時に起動）、`dev-write-tools.txt` に `on`（DEV 書き込みツール）、`tools\update.cmd`（最新のリリースのタグへ更新）。詳しい手順と困ったときは [docs/INSTALL.ja.md](docs/INSTALL.ja.md) を見てください。

**標準はBrokerモードです。** `tools\start-broker-mode.cmd`を1回起動します。
Brokerのパイプが用意できるまで待ってからトンネルを起動します。
結果が分からないコマンドを再起動後も繰り返さない機能はBrokerモードだけです。
補足の通常モードには、この二重実行の防止がありません。
再起動したあとは Broker モードを手で起動し直します。任意のログオン時のタスクを登録すれば、次のサインイン時に1回起動します。起動に失敗したり終了したりしても、やり直しはしません。
任意で、Windows サインイン時に ChatGPT 用 Broker モードを起動できます。
[設定手順](docs/INSTALL.ja.md#サインイン時に起動する任意)

### 登録ツール数

| 構成 | 登録ツール数 |
| --- | ---: |
| ChatGPT（同梱のランチャーで起動。Broker モードでも通常モードでも、編集・作成 pilot はオン） | 49 |
| DEV書き込みツールをオンにした ChatGPT 用 Host | 48（編集・作成 pilot もオンなら54） |
| DEV 書き込みツールと編集・作成 pilot を両方オフにした ChatGPT 用 Host | 43 |
| 読み取り専用 Muse ブリッジ | 36（`--inbox` 付きは 37） |

## v0.6.2 の変更

小さな直しと、分かりやすくした README です。ツール数は変わりません（同梱のランチャーで 49）。

- **setup が、Muse だけ残ったときの次の手順を示します。** 残りが Muse ブリッジの設定だけのとき、`tools\setup.cmd` は次の手順を示します。`docs\muse-bridge-peers.example.json` を `%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json` にコピーし、Muse のエージェントの StableID を `readonly` に書きます。
- **パッケージを確かめられないとき、update ははっきり止まります。** `tools\update.cmd` がコードを新しいリリースのタグへ動かしたあと、パッケージをオフラインで確かめられなかった場合は、止まって（HOLD）、再起動の前に `tools\setup.cmd` を実行するよう伝えます。
- **README：** 承認のしくみの表、クイックスタート、向いている人、よくある質問、Windows テストのバッジを足しました。変更履歴は CHANGELOG.ja.md に移しました。

これまでの変更は [CHANGELOG.ja.md](CHANGELOG.ja.md) を見てください。

導入は [docs/INSTALL.ja.md](docs/INSTALL.ja.md) を見てください。

### 向いている人

- **向いている：** Windows 11 の開発者で、ChatGPT にプロジェクトを読ませ、テストやビルドを回させたい人。承認窓を毎回読む人。
- **向いていない：** macOS や Linux だけの人。本番の PC や大事なデータのあるフォルダ。スマホから承認したい人（遠隔での承認はありません）。

### よくある質問

**ほかの MCP クライアントでも使えますか？** ChatGPT（Secure MCP Tunnel 経由）と、読み取り専用の Muse ブリッジ向けに作って試しています。ほかのクライアントは試していません。

**macOS や Linux で動きますか？** 動きません。Windows 専用です。

**無料ですか？** はい。AGPL v3.0 です。任意の有料ガイドもあります。

**本番で使えますか？** 使えません。v0.6 は開発者向けプレビューで、`remote_ready=false` を返します。

**Muse は PC を操作できますか？** 読み取り専用のブリッジでは操作できません。コマンドは断ります。

**ライセンス：** コードは [GNU AGPL v3.0](LICENSE) です。無料で使えて、変更もできます。配布するときや、変更版をネットワーク越しに提供するときは、同じライセンスでソースを公開する必要があります。Sora Commander の名前とキャラクター画像は対象外です。詳しくは [NOTICE.md](NOTICE.md) を見てください。

保証はありません。承認した操作の責任は、利用者にあります。

Sora Commander は個人の独立したプロジェクトで、OpenAI や Meta の製品ではありません。OpenAI や Meta が作ったり、推奨したり、サポートしたりしているものでもなく、提携もしていません。ChatGPT と Sora は OpenAI の製品です。Muse は Meta の製品です。
