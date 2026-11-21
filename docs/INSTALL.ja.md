# 導入手順

**英語版（[INSTALL.md](INSTALL.md)）と内容が違うときは、英語版が正しい。**

作者の通常環境に加え、2026-11-09に新しいWindowsサンドボックス（管理者権限のユーザー）で、セットアップ、ChatGPTからのファイル読取り、Owner承認付きコマンドの終了コード0まで試しました。2026-11-21には、Python・Git・PowerShell 7 のない新しいサンドボックスで、もう一度試しました。Python 3.13 と Git for Windows を公式のインストーラーで入れ、`tools\setup.cmd`が`.venv`の作成、パッケージの導入、doctorの診断まで進み、ランチャーの起動前の確認（`--check`）も通りました。日本語を含むcheckoutのパスでも同じです。トンネルの起動とChatGPTへの接続はしていません。別の実PC、別のWindowsユーザーでの導入、ASCII以外の文字を含むcheckoutのパスでの実際の起動は未試験です。許可したフォルダの中の日本語のフォルダ名・ファイル名は、合成テストで確認しています。これらの限定的な試行は、本番の受入ではありません。

手順と画面が合わない場合は、issueで知らせてください。キーは書かないでください。

任意の[読み取り専用Museブリッジ](muse-bridge.ja.md)では、このWindows PCで
Tailscaleを起動し、エージェントのノードを同じtailnetに参加させ、PCを起動したままに
します。コマンドは拒否され、ブリッジから承認窓は開きません。試したのは作者自身の
環境だけで、別アカウントのMuseが同じ方法で自分のtailnetに参加できるかは未確認です。
MuseはMetaの製品で、このプロジェクトはMetaと提携しておらず、推奨も受けていません。
2026年11月時点の案内で、ChatGPTとMuseのプラン条件は変わる可能性があります。

v0.3.0から、読み取り専用のMuseブリッジの既定の認証方式はTailscaleのノード識別です。
トークンを引き続き使う場合は、`tools\start-muse-bridge.cmd --auth token`
のように`--auth token`を指定します。

> **まず ChatGPT のプランを確認**
>
> - カスタム MCP 接続は Web 版のみです。OpenAI はフル MCP の対象として Business と Enterprise/Edu を案内しています。[OpenAI ヘルプ](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)（2026-11-20 確認）。
> - 同じページの FAQ では、Pro はカスタム MCP を読み取り・取得の権限でのみ接続できます。コマンドや編集は使えない見込みです。
> - 作者は 2026 年 11 月時点で、**Plus の開発者モード**で Sora Commander を使い、承認付きコマンドも実行しています。
> - このヘルプページに Plus の記載はありません。この環境で使えるかどうかはいつでも変わり得ます。
> - 読み取り専用の Muse ブリッジも使えます。Tailscale と Python のセットアップが必要で、ChatGPT のプランは不要です。

## 必要なもの

- **Python 3.13**（Windows の `py` ランチャー付き。python.org のインストーラーで入ります）。新しい版が案内されても 3.13 を入れてください。3.14 は試しておらず、ヘルパーは `py -3.13` で動きます。
- **Git for Windows**（既定のフォルダ `C:\Program Files\Git` に入れる）。読み取り専用の Git ツールはこの場所だけを使い、`PATH` は見ません。ほかの場所だと `GIT_NOT_INSTALLED` になります。`tools\setup.cmd` は `PATH` 上の `git` も使います。既定のインストーラーで設定されます。
- **PowerShell のインストールは要りません。** ランチャーとヘルパーは `.cmd` と Python だけで動くので、PowerShell 7 も Windows PowerShell 5.1 も必要ありません。以下で `powershell` と書いたいくつかのコマンドは、どちらの PowerShell でも動きます。

## クイックセットアップ

手順1の `git clone` と `cd` を実行したら、`tools\setup.cmd` を実行します。
Museブリッジだけを使う場合は `tools\setup.cmd --muse-only` を実行します。
このヘルパーは`.venv`を作り、手順1のパッケージを入れます。`%USERPROFILE%\.config\tunnel-client\sora-next.yaml` がまだなければ、同梱の見本をコピーします（既存のファイルは上書きせず、`--muse-only`ではコピーしません）。そのあと doctor で診断して、残りの手作業を一覧で表示します。
`--check` を付けると、何も変えずに確認だけします。終了コードは、0 が HOLD なし（TODO は残ることがあります）、2 が HOLD のある手順あり、6 が対応していない checkout のパスです。
各手順の詳細は以下の手動手順を参照してください。

## 更新する

**v0.4.0 から更新するとき：** v0.4.0 には更新ヘルパーがないので、1 回だけ手で更新します。
トンネル、Broker、Museブリッジを止めます（ログオン時のタスクを登録していれば、それも止めます）。
そのうえで、変更のない `main` の checkout で次を実行します。

```bat
git pull --ff-only
tools\setup.cmd --check
```

TODO や HOLD の行があれば対応してから、起動し直します。次からは下のヘルパーを使えます。

checkoutで `tools\update.cmd --check` を実行すると、追加されるリリースタグと
リリースノートのURLを確認できます。この確認でもタグの取得とリモート追跡参照の
更新が行われます。更新を適用するには `tools\update.cmd` を実行してください。
`main` を `origin/main` まで早送りします。タグはリリースノートを示すためのものです。

公式HTTPSリポジトリの `origin/main` を追跡するクリーンな `main` が必要です。
ローカル変更がある場合、このcheckoutのSora Commanderプロセスが動いている場合、
または早送りできない場合は停止します。プロセスを手動で止める方法を表示し、
上書きされる可能性のある無視対象ファイルも保護します。
コード更新後はPythonパッケージをオフラインで確認します。不足や互換性の問題が
ある場合は、案内に従って再起動前に `tools\setup.cmd` を別途実行してください。
更新のあとは、使っているサービスを再起動し、ChatGPT と Muse 側でツールを再読み込みしてください。

## 1. コードとPythonパッケージを入手する

自分だけが書き込めるフォルダを使い、**checkoutのパスのどこにも空白を入れないでください**。例は`C:\SoraCommander`です。4つのランチャーは、`--check`の場合も含め、checkoutのパスに空白があれば終了コード6で止まります。ランチャー、`tools\setup.cmd`、`tools\update.cmd`、Brokerのログオン時起動は、`& % ^ ! ( ) " < > |`や表示されない文字を含むパスも終了コード6で止めます。ASCII以外の文字を含むcheckoutのパスは、サンドボックスで`tools\setup.cmd`とランチャーの`--check`までは通りましたが、実際の起動はまだ試していません。許可したフォルダの中の日本語の名前は、合成テストで通っています。

PowerShellで、`C:\`から始めます。

```powershell
git clone https://github.com/kurojack-jp/SoraCommander.git C:\SoraCommander
cd C:\SoraCommander
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install "mcp>=1,<2" "pydantic>=2,<3" "pywin32>=306"
```

ランチャーはこのフォルダのコードを読み込むため、上記の依存パッケージだけをインストールします。`pip install -e .`は使わないでください。同じパッケージが2つ見える状態になり、リリース確認のテストが拒否します。

## 2. トンネルプログラムを入手する

OpenAIの[Secure MCP Tunnelsのガイド](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)から`tunnel-client`をダウンロードし、次の場所に置きます。

```text
tools\tunnel-client\<version>\tunnel-client.exe
```

バージョンフォルダーは v0.0.16 のように、小文字の`v`とピリオドで区切った3つの数字で指定します。ランチャーは有効な候補の中から最新のバージョンを使い、リンクのフォルダやファイルは飛ばします。作者が試したバージョンは v0.0.16 だけです。

## 3. トンネルとキーを作る

1. <https://platform.openai.com/settings/organization/tunnels>でトンネルを作成し、そのIDを控えます。
2. <https://platform.openai.com/settings/organization/api-keys>でruntime APIキーを作成します。
3. キーを保存します。**入力内容は画面に表示されません。他のファイルには書かないでください。**

```bat
tools\set-tunnel-key.cmd
```

`%USERPROFILE%\.config\tunnel-client\sora-next.key` に、自分の Windows ユーザーだけが開ける形で保存します。すでにキーがあれば、置き換える前に確認します。
`tools\set-tunnel-key.cmd --check` は、保存したファイルの形だけを確かめます。キーは表示せず、OpenAI にも問い合わせません（終了コード 0 は正常、4 はファイルなし、5 は形が不正、2 は確認できなかった、6 は対応していない checkout のパス）。
`--from-stdin` は標準入力からキーを読み、既存のキーを確認なしで置き換えます。
従来の PowerShell コマンド `tools\Set-NextTunnelKey.ps1` も引き続き使えます。中では同じヘルパーを呼びます。

## 4. トンネルの設定を書く

`tools\setup.cmd` がまだコピーしていなければ、[sora-next.example.yaml](sora-next.example.yaml)を`%USERPROFILE%\.config\tunnel-client\sora-next.yaml`へコピーします。例の中で指定されている3つの値（トンネルのID、キーファイルの絶対パス、checkoutのパス）を書き換えます。ファイルがすでにあれば、上書きせずにそのファイルを編集してください。

### 自分のプロジェクトのフォルダを許可する

v0.1.9から、このファイルと`language.txt`・`tasks.json`は、手で書く代わりに任意のConfig Builderの画面でも作成・編集できます（`py -3.13 -B tools\scn_config_builder.py`）。Hostが起動時に使うのと同じコードで内容を確認します。[Config Builderの説明](config-builder.ja.md)を参照してください。以下の決まりはそのまま適用されます。

ChatGPTのファイル操作ツールで自分のプロジェクトを扱う場合は、`%USERPROFILE%\.config\sora-commander\allowed-roots.txt`を手元で作成します。UTF-8で、1行に1つのローカルフォルダの絶対パスを書きます。引用符で囲まず、環境変数も使わないでください。

```text
# Folders I allow
C:\Work\ProjectOne
C:\Work\ProjectTwo
read: C:\Work\ReferenceNotes
```

**許可するフォルダは慎重に選んでください。** `read:`のフォルダも含めて、許可フォルダの中のファイルは、承認窓なしで読まれ、内容がChatGPTに送られます。拒否は名前だけで判断します（`.env`や`credentials`など）。そのため、`appsettings.json`、`web.config`、`config.yaml`などに書かれたパスワードや接続文字列は読まれる可能性があります。そのようなファイルがあるフォルダは許可しないでください。

接頭辞のない行は、読取り・作成・編集・移動と、コマンドの作業場所をすべて許可します。小文字の`read:`で始まる行は、読取り・検索・一覧表示だけを許可します。`read:`の後ろの空白は無視します。`READ:`など、それ以外の接頭辞は不正です。読むだけのフォルダは、ファイル書込みの対象やコマンドの作業場所には使えません。全権限の一覧と読むだけの一覧の間で、同じフォルダや、一方がもう一方を含むフォルダを指定すると、行のエラーとして起動を拒否します。記載順序は問いません。

空行と`#`で始まる行は無視します。設定ファイルは既定の一覧を置き換えます。checkoutも許可したい場合は、そのパスも明示的に書いてください。ファイルがなければcheckoutだけに全権限を許可します。上限は両方の合計で16件です。ファイルが存在するのに空またはコメントだけの場合や、1行でも不正な行がある場合は起動を拒否します。不正な行を読み飛ばして続行することはありません。エラーには行番号と理由だけを表示します。行0はファイル全体のエラーを意味します。Broker起動時には`[INFO] Allowed folders: N full, M read-only for file tools`（全権限N件、読むだけM件）と数だけを表示します。以下の確認条件と拒否対象は、両方に同じように適用されます。

一覧は各プロセスの起動時に1回だけ読み込みます。変更後は、**Brokerとクライアント／トンネルの両方を再起動**してください。MCPのツールやBrokerのメッセージから、この設定を読んだり、書いたり、再読込みしたりすることはできません。`tests` profileは従来のtestsフォルダだけの範囲を維持し、このファイルを読みません。

次のフォルダは拒否します。

- 存在しないフォルダ、ファイル、シンボリックリンク・ジャンクション・reparseを含むパス、UNCパス、ドライブのルート。
- Windows、Program Files（x86用も含む）、ProgramDataと、それぞれの配下。
- ユーザープロファイル自体、AppDataとその配下。
- この設定ファイル、`%USERPROFILE%\.config\tunnel-client`、`%LOCALAPPDATA%\SoraCommanderNext`を内側に含むフォルダ。キーや状態データを置くこれらの非公開フォルダも除外します。
- 保護対象の名前（下の一覧）を含むパス。

**保護対象の名前。** 許可フォルダの中でも、次の名前のファイルやフォルダは読めず、一覧や検索からも黙って外れます。ChatGPT や Muse からはまったく見えません。直接読もうとすると `sensitive path excluded from read scope` が返ります。大文字と小文字は区別しません。

- `.` で始まる名前（隠し名）、空白か `.` で終わる名前、`:` や制御文字を含む名前、`CON`・`NUL`・`COM1`・`LPT1` などのデバイス名。
- この名前のフォルダやファイル：`state`、`runtime`、`logs`、`temp`、`venv`、`node_modules`、`__pycache__`、`secret`、`secrets`、`token`、`tokens`、`credential`、`credentials`、`private_key`、`private-key`、`private_keys`、`private-keys`、`auth.json`、`oauth.json`、`api-keys.json`、`apikeys.json`、`id_rsa`、`id_ed25519`、`id_dsa`、`id_ecdsa`、Sora Commander 自身の journal ファイル。
- 次で始まる名前：`credential.`、`credentials.`、`secret.`、`secrets.`、`token.`、`tokens.`、`api_key.`、`api-key.`、`private_key.`、`private-key.`（`token.py` や `token.ts` も見えなくなります）。
- 次で終わる名前：`.user.txt`、`.env`、`.pem`、`.key`、`.pfx`、`.p12`、`.jks`、`.keystore`、`.kdbx`、`.sqlite`、`.sqlite3`、`.db`、`.db3`、`.wal`、`.shm`、`.journal`、`.zip`、`.7z`、`.rar`、`.tar`、`.tgz`、`.gz`、`.bak`、`.backup`。

安全側に倒した決まりなので、`src/state` のような普通のフォルダも見えなくなることがあります。必要なら、そのファイルは自分で開いてください。

リンク経由の設定ファイルや、不正なUTF-8も拒否します。ファイルの上限は64 KiBです。この一覧はファイル操作ツールとコマンドの作業ディレクトリの範囲を指定するもので、明示的に承認したコマンドをサンドボックスで隔離するものではありません。

**読むだけでも、承認したコマンドはOwnerのWindows権限で動くため、そのフォルダへの書込みは防げません。** コマンドが絶対パスを指定したり、自分で作業ディレクトリを変更したりすれば、書き込めます。`read:`はその書込みを防止する仕組みではないため、毎回の承認内容を確認してください。

**`read:` で守れるのは、ChatGPT のファイル用の道具からだけです。承認したコマンドからは守れません。** 承認窓に読むだけのフォルダの中のパスが出てきたら、よく読んでから承認してください。

### 承認窓の言語を選ぶ（任意）

承認窓は英語か日本語で表示します。何も設定しなければ、Windowsの表示言語に合わせます。日本語のWindowsなら日本語、それ以外の言語なら英語です。自分で選ぶ場合は、`%USERPROFILE%\.config\sora-commander\language.txt`を作り、`en`か`ja`を1行だけ書いてください（小文字。`#`で始まる行は無視します）。それ以外の内容だと、`[HOLD]`を表示して起動を止めます。言語は起動時に読み込むので、変更したらBrokerとクライアントの両方を再起動してください。Brokerは起動時に`[INFO] Window language: ...`と表示します。言語で変わるのは窓の文字だけです。確認や承認の内容は変わりません。

### 作業の型を登録する（任意）

よく使うコマンドを、ChatGPTから名前で起動できるようにするには、`%USERPROFILE%\.config\sora-commander\tasks.json`を作ります。

```json
{
  "tasks": [
    {
      "name": "my-check",
      "description": "Full check; log in artifacts\\check.log",
      "cwd": "C:\\Work\\ProjectOne",
      "executable": "C:\\Windows\\System32\\cmd.exe",
      "argv": ["/d", "/s", "/c", "npm.cmd test > artifacts\\check.log 2>&1"],
      "timeout": 1800,
      "output_limit": 65536
    }
  ]
}
```

ChatGPTは`list_tasks`で一覧を見て、`run_task`で起動します。**起動のたびに承認窓が出ます。** 窓には実際のコマンドが表示され、作業フォルダは許可フォルダである必要があります。ChatGPTは引数を足したり変えたりできません。1つの作業には、ちょうどこの7つのキーが必要です。名前は小文字の英字・数字・ハイフンで、最大32件、64 KiBまでです。不正な内容があると`[HOLD] tasks.json: ...`を表示して起動を止めます。起動時に読み込むので、変更したらBrokerとクライアントの両方を再起動してください。

## 5. 起動する

モードを1つ選びます。**両方を同時に動かさないでください。**

**Brokerモード（推奨・標準）。** ジョブと承認窓は別プロセスにあり、トンネルを再起動しても残ります。`tools\start-broker-mode.cmd`を1回ダブルクリックします。Brokerを別の窓で起動し、Brokerのパイプが存在するまで待ってから、トンネルを起動します。これはパイプの存在確認であり、Brokerの応答確認ではありません。両方の窓を開いたままにしてください。停止するときは、先にトンネルの窓で`Ctrl+C`、次にBrokerの窓で`Ctrl+C`を押します。

トンネルだけを再起動するには、Brokerを動かしたまま`tools\start-sora-commander-broker-dev.cmd`を使います。

Windowsにサインインしたままにしてください。画面がロックされていると承認窓を開けません。

**補足の通常モード：** `tools\start-sora-commander-local.cmd`を1回ダブルクリックし、その窓を開いたままにします。このモードにはBrokerモードの再起動後の二重実行防止がありません。結果不明のコマンドをやり直すためにモードを変えないでください。例のYAMLはBrokerクライアントを使っています。通常モードでは、起動前にcommandを同じフォルダの`next-development.cmd`へ変更する必要があります。

### サインイン時に起動する（任意）

セットアップ後、まず上の手順で Broker モードを手で起動できることを確かめてください。
そのあと `tools\install-broker-autostart.cmd` を実行すると、自分の
Windows サインイン時に実行するタスクが登録されます。登録時には何も起動しません。
登録済みのタスクを変えるときは、先に登録を解除してください。
サインイン時はローカルのデフォルトルートを最大3分待ち、Broker を起動します。
Broker の準備ができたら ChatGPT 用トンネルを1回起動します。起動に失敗したり、
どちらかのプロセスが終了したりしても、自動で再試行しません。

サインイン中は手動起動と同じように、ChatGPT が許可済みフォルダを承認窓なしで
読み取れます。承認が必要な操作では、引き続き PC 上で応答してください。

ログは `%LOCALAPPDATA%\SoraCommanderNext\broker-autostart.log` にあります。
起動時に 1 MiB を超えていれば `broker-autostart.log.1` に移します。
以下のコマンドでタスクの確認、即時起動、停止ができます。

```cmd
schtasks /Query /TN "SoraCommander Broker mode"
schtasks /Run /TN "SoraCommander Broker mode"
schtasks /End /TN "SoraCommander Broker mode"
```

タスクを削除するには `tools\uninstall-broker-autostart.cmd` を実行します。
タスクが動いていれば先に止めるので、実行中のジョブがないか確かめてから解除してください。

## 6. ChatGPTに接続する

利用条件、ワークスペースの権限、接続方法は、OpenAI公式ヘルプの最新の[Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)（[日本語](https://help.openai.com/ja-jp/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)）に従ってください。案内に従って自分のSecure MCP Tunnelを使います。画面名や手順は変わることがあります。接続後、新しいチャットで`ping`、`status`、`list_sessions`、`tool_catalog`を依頼してください。

期待する状態は、`status`の`remote_ready: false`、`approval: LOCAL_DEMO_ONLY`です。

| 構成 | 登録ツール数 |
| --- | ---: |
| ChatGPT（手順5のとおり起動。Broker モードでも通常モードでも、編集・作成 pilot はオン） | 54 |
| 編集・作成 pilot をオフにした ChatGPT 用 Host | 48 |
| 読み取り専用 Muse ブリッジ | 36（`--inbox` 付きは 37） |

過去の変更点にあるツール数は、各リリース時点の数です。

ChatGPTのファイル操作ツールは、設定した許可フォルダに届きます。設定ファイルがなければcheckoutだけです。**承認したコマンドは本人のWindows権限で動き、そのアカウントでできる操作を実行できます。**

## 問題が起きたとき

ChatGPTが承認は未決定で終わったと伝えた場合、何も実行されていません。準備ができたら改めて依頼してください。

`.venv\Scripts\python.exe tools\scn_doctor.py`で、修正先の案内付きの読取り専用セットアップ確認を実行できます。
何も起動せず、トンネルのキーやYAMLの内容も読みません。`--lang en`または`--lang ja`で、この診断結果の言語を選べます。承認窓の言語は`language.txt`で決まります。
WARNは情報であり、HOLDがある場合は終了コード1になります。設定変更後はBrokerとクライアントを再起動してください。

ランチャーの終了コード6は、対応していない checkout のパスという意味です。Broker 自身（Broker の窓）の終了コード6は、別のプロセスがすでに Owner の journal を使っているという意味です。

| 表示 | 意味 | 対応 |
|---|---|---|
| `A tunnel is already running or port 8080 is in use` | トンネルが動作中、または他のプログラムがポートを使用中 | 2つ目を起動しないでください |
| `A tunnel is running. Stop it first` | Brokerをトンネルより先に起動する必要がある | トンネルを止め、上記の順に起動してください |
| `A Broker is already running` | Broker モードがすでに動いている | `tools\start-sora-commander-broker-dev.cmd` でトンネルだけを起動してください |
| `The Broker pipe was not ready within 30 seconds` | Broker が起動しなかった | Broker の窓を読んで原因を直し、もう一度起動してください |
| `Could not check ...`（終了コード2） | 起動前の確認に Windows が答えなかったので、何も起動していない | 時間をおいてやり直してください。続くときはその行を報告してください |
| `Required local component is missing`（終了コード2） | ランチャーが使うファイルがない（多くは`.venv`） | `tools\setup.cmd` を実行するか、checkout の中身を確かめてください |
| `GIT_NOT_INSTALLED` | Git for Windows が `C:\Program Files\Git` にない | 既定のフォルダに入れてください |
| `No key file`または`The key file must hold the key only` | キーがない、または形式が不正 | 手順3をやり直してください |
| `Development virtual environment not found` | `.venv`がない | `tools\setup.cmd` か手順1を実施してください |
| `The folder path contains a space`（終了コード6） | checkoutのパスに空白があり、未対応 | `C:\SoraCommander`など、空白を含まないパスへ移してください |
| `allowed-roots line ...` | 手元の許可フォルダ設定が不正、または読取り不可 | 指定行を手元で直し、両プロセスを再起動してください |
| 依頼していない窓 | 古い要求や重複した要求の可能性 | **Reject（拒否）してください** |
| `DENY_DUPLICATE_UNKNOWN` | 同じコマンドの結果が不明なので拒否された | 次の項目を確認してください |

### 結果が不明なコマンドの記録を解除する

**Brokerモードだけの機能：** ジョブの実行中にBrokerが突然止まると、同じコマンドは拒否されたままになります。通常モードには、再起動後のこの保護がありません。

1. コマンドが何をしたか、手元で確認してください。すでに実行された可能性があります。
2. Brokerを停止します。
3. コンソールの窓で、このフォルダから次を実行します。

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m sora_commander_next.owner_broker_main --list-unknown
.venv\Scripts\python.exe -m sora_commander_next.owner_broker_main --resolve-unknown <ID>
```

確認を求められたら`yes`と入力します。パイプからの入力は拒否されます。

## v0.1.5の追加ツール（任意）

v0.1.5の時点で、任意の編集・作成pilot付きDEV構成は53ツールでした（v0.1.7から54ツール）。表示数はHost構成によります。開発者向けプレビューの制限は継続します。
`remote_ready=false`、`LOCAL_DEMO_ONLY`で、本番・公開の安全ゲートはHOLDです。

### 読取り専用Git

`git_snapshot`はブランチ・HEAD・状態・最近のコミット、`git_diff`は差分、
`git_log`は履歴、`git_show`はコミットや過去のファイルを読みます。
`git_remote_check`はrefと現在のブランチのHTTPS upstreamを比較します。
5つとも`read:`を含む許可された読取りフォルダを使い、承認窓は不要です。
hooks・filters・設定された外部プログラムは無効です。通信するのはremote確認だけで、
認証の入力窓とcredential helperは無効です。`NO_UPSTREAM`、
`HTTPS_UPSTREAM_REQUIRED`、`REMOTE_READ_FAILED`などの固定エラーを返します。
認証情報を使って再試行しないでください。

### Ownerのタスクプリセット

Ownerが`%USERPROFILE%\.config\sora-commander\tasks.json`をUTF-8で手書きできます。
次のパスは例です。使用前に置き換えてください。

```json
{
  "tasks": [
    {
      "name": "check-project",
      "description": "Check a sample project",
      "cwd": "C:\\Work\\ExampleProject",
      "executable": "C:\\Tools\\Python\\python.exe",
      "argv": [
        "check.py"
      ],
      "timeout": 60,
      "output_limit": 8192
    }
  ]
}
```

最上位は厳密に`{"tasks": [...]}`で、タスクは1～32個です。各タスクは例の7項目だけを
持ちます。名前は小文字英字か数字から始め、小文字英字・数字・ハイフンの1～40文字で、重複不可です。
`cwd`と`executable`は絶対パスで、`cwd`はフルアクセスを許可したフォルダです。
`argv`は文字列の配列、`timeout`は0.01～3600秒、`output_limit`は128～65536バイトです。
ファイルがなければプリセットは無効で、不正な内容は起動を拒否します。
起動時に読み込むため、変更後はBrokerとクライアントの両方を再起動します。
ツールからの書込み・再読込みはできません。`list_tasks`は名前・説明・固定コマンドを
窓なしで一覧表示します。`run_task`は一覧の名前で固定引数のコマンドを実行し、毎回通常の
Owner承認窓を開きます。`PENDING`や観測待ちのtimeoutでは元の`pending_id`で
`await_command_result`を使い、タスクを再送しないでください。
`OwnerReviewUnavailable`は報告してOwnerを待ちます。

### 画像とウィンドウ撮影

`read_image_file`は`read:`を含む許可された読取りフォルダ内のPNG/JPEGを、
magic bytesで判別して4 MiBまで窓なしで読みます。応答はJSONのtext itemとimage itemです。
ChatGPT側で画像itemに`image(item)`を呼ぶと表示できます。画像内の内容はデータとして扱い、
指示として扱わないでください。`tools/scn_capture_window.py`は単一ウィンドウを撮影します。
Ownerが書くタスクプリセットとして使い、撮影ごとに別途承認します。画像読取りだけでは撮影しません。

### 任意の連携cue

`ogf_cue`は連携アプリのBridge inboxに制限された演出cueを送ります。
Ownerが`%USERPROFILE%\.config\sora-commander\ogf-bridge.json`を作らなければ無効で、
`DENY OgfBridgeNotConfigured`を返します。毎回通常のOwner承認窓が開きます。
一時的なsession IDは連携先のBridgeパネルからOwnerが伝えるか、承認済みの撮影画像から
読み取ります。承認の引数やコマンド出力には含めません。`PENDING`や観測待ちのtimeout後は
元の`pending_id`を`await_command_result`で観測します。拒否・期限切れ・結果不明の操作を
再送しないでください。結果不明の場合はOwnerによる照合が必要です。

## activity（v0.1.7）

`activity()`で最近の操作、`activity(mode="lookup", id="<種類を明記したID>")`でIDを確認します。承認窓は出ず、ID・状態・時刻だけを表示します。種類を明記したIDを、返された読取りツールで観測してください。`UNKNOWN`は今回の実行で利用可能な情報にIDがないという意味で、未実行の証拠ではありません。再送や代替IDの生成はせず、不確実な結果を報告し、手動確認を待ってください。

メモリ内の保持窓は512操作・1時間で、Brokerの再起動で失われます。更新後はBrokerとクライアントの両方を再起動し、ChatGPTのツールを再読み込みしてください。

## 承認が未決定で終わったとき（v0.1.7）

2026-11-13にBrokerモードでChatGPTを通じた実際の窓を確認しました。確認済みはコマンド窓（Approve、Reject、X、無回答）と`create_file`（X、Approve）です。他の経路はテストでは同じ動作ですが、実際の窓では未確認です。未確認の範囲は`create_file`と他のDEVファイルツールの無回答、DEV `send_input`、対話型の2つ目の窓、テキスト編集・作成pilot、`run_task`、`ogf_cue`です。このDEV確認は本番受入を意味しません。

| Ownerの操作 | ChatGPTが受け取る結果 | 意味 |
|---|---|---|
| Rejectを押した | `DENIED` | Ownerが却下した |
| Xで窓を閉じた | `TIMED_OUT`, `reason=CLOSED` | 未決定。何も実行・書込みされていない |
| 時間内に回答しなかった | `TIMED_OUT`, `reason=EXPIRED` | 未決定。何も実行・書込みされていない |

**Xの意味が変わりました。** 以前のバージョンでは窓を閉じると却下扱いでした。今後は未決定で承認を終了します。無回答は拒否ではなく、Ownerが離席していた証拠でもありません。45秒と60秒の制限時間は変わりません。

回答を確認できなかった場合、`ERROR`と`NOT_STARTED`は何も始まっていないことを意味します。実行・書込みの有無が分からない`OUTCOME_UNKNOWN` / `UNKNOWN`は手動確認が必要です。書込み検証エラーを含め、`NOT_STARTED`が証明されていない`ERROR`も手動確認が必要です。これらと表の3つの終了すべてについて、ChatGPTは報告してOwnerの次の指示を待ちます（`REPORT_AND_WAIT`）。再送、同じ操作の新規リクエスト、窓の再表示の要求は禁止です。新規リクエストにはOwnerの新しい明示指示が必要です。`OwnerReviewUnavailable`は窓が表示されず何も実行されなかったことを意味します。報告して待ち、Ownerの明示指示がある場合だけ新規リクエストを送ります。

対象はコマンドの窓（`run_command*`, `run_task`, `ogf_cue`）、DEVファイルツール（`create_directory`, `create_file`, `move_file`, `request_source_patch_dev`）、DEVの`send_input`と対話実行の2番目の窓、有効にした場合のテキスト編集・作成pilotです。コマンドは`launch_outcome=NOT_STARTED`、ファイル・入力は今回の操作の`write_outcome=NOT_STARTED`を使い、既存プロセスが起動していないという意味ではありません。例外として、対話実行の`SECOND_GUI`でRejectすると、従来どおり`ERROR/OwnerAuthorizationRequired`と`launch_outcome=NOT_STARTED`を返します。報告して待ちます。Brokerの入力は引き続き`DENY/DevInteractiveOnly`です。`PENDING`は観測待ちだけを意味し、同じIDを既存のstatus/waitツールで観測します。終端の`TIMED_OUT`とは異なります。

## テストを実行する

```powershell
$env:PYTHONPATH = "src;."
$m = Get-ChildItem tests\test_*.py | ForEach-Object { "tests." + $_.BaseName }
.venv\Scripts\python.exe tests\run_with_real_dialog_guard.py @m
```

公開版に含まれるのは、開発用テストの一部です。このコマンドは手元のコピーに含まれるテストモジュールを、実際の承認窓を開こうとすると失敗させるguard付きで実行します。開発用の全回帰テストではありません。トンネルは不要で、数分かかります。パスに`temp`というフォルダや、ドットで始まるフォルダを含めないでください。一部のテストがスキップされることがあります。

※ 実画面のスクリーンショット付きで一歩ずつ進めたい方向けに、有料の初心者向け実践ガイドもあります：
https://note.com/kurojack_jp/n/nab0d17f9e491
