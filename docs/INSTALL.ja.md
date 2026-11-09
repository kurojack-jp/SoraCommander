# 導入手順

**英語版（[INSTALL.md](INSTALL.md)）と内容が違うときは、英語版が正しい。**

作者の通常環境に加え、2026-11-09に新しいWindowsサンドボックス（管理者権限のユーザー）で、セットアップ、ChatGPTからのファイル読取り、Owner承認付きコマンドの終了コード0まで試しました。PythonとGitはホストから読み取り専用で借りたため、Pythonの導入からの準備は試していません。別の実PC、別のWindowsユーザーでの導入、日本語などASCII以外の文字を含むパスも未試験です。この限定的な試行は、本番の受入ではありません。

手順と画面が合わない場合は、issueで知らせてください。キーは書かないでください。

## 1. コードとPythonパッケージを入手する

自分だけが書き込めるフォルダを使い、**checkoutのパスのどこにも空白を入れないでください**。例は`C:\SoraCommander`です。4つのランチャーは、`--check`の場合も含め、checkoutのパスに空白があれば終了コード6で止まります。ASCII以外の文字を含むパスは未確認で、別のPCでの試験が残っています。

PowerShellで、`C:\`から始めます。

```powershell
git clone https://github.com/kurojack-jp/SoraCommander.git C:\SoraCommander
cd C:\SoraCommander
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install "mcp>=1,<2" "pydantic>=2,<3" "pywin32>=306"
```

ランチャーはこのフォルダのコードを読み込むため、上記の依存パッケージだけをインストールします。`pip install -e .`は使わないでください。同じパッケージが2つ見える状態になり、リリース確認のテストが拒否します。

## 2. トンネルプログラムを入手する

OpenAIのSecure MCP Tunnelの案内から`tunnel-client`をダウンロードし、次の場所に置きます。

```text
tools\tunnel-client\v0.0.16\tunnel-client.exe
```

ランチャーはこのパスを指定しています。新しいバージョンを使うには、ランチャー内のパスの変更が必要です。試したバージョンはv0.0.16だけです。

## 3. トンネルとキーを作る

1. <https://platform.openai.com/settings/organization/tunnels>でトンネルを作成し、そのIDを控えます。
2. <https://platform.openai.com/settings/organization/api-keys>でruntime APIキーを作成します。
3. キーを保存します。**入力内容は画面に表示されません。他のファイルには書かないでください。**

```powershell
powershell -ExecutionPolicy Bypass -File tools\Set-NextTunnelKey.ps1
```

## 4. トンネルの設定を書く

[sora-next.example.yaml](sora-next.example.yaml)を`%USERPROFILE%\.config\tunnel-client\sora-next.yaml`へコピーし、例の中で指定されている3つの値を書き換えます。

### 自分のプロジェクトのフォルダを許可する

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
- 隠し名、`temp`、`logs`、`runtime`、認証情報の名前、保護対象の拡張子など、`file_reads.py`が拒否する名前を含むパス。

リンク経由の設定ファイルや、不正なUTF-8も拒否します。ファイルの上限は64 KiBです。この一覧はファイル操作ツールとコマンドの作業ディレクトリの範囲を指定するもので、明示的に承認したコマンドをサンドボックスで隔離するものではありません。

**読むだけでも、承認したコマンドはOwnerのWindows権限で動くため、そのフォルダへの書込みは防げません。** コマンドが絶対パスを指定したり、自分で作業ディレクトリを変更したりすれば、書き込めます。`read:`はその書込みを防止する仕組みではないため、毎回の承認内容を確認してください。

**`read:` で守れるのは、ChatGPT のファイル用の道具からだけです。承認したコマンドからは守れません。** 承認窓に読むだけのフォルダの中のパスが出てきたら、よく読んでから承認してください。

## 5. 起動する

モードを1つ選びます。**両方を同時に動かさないでください。**

**Brokerモード（推奨・標準）。** ジョブと承認窓は別プロセスにあり、トンネルを再起動しても残ります。`tools\start-broker-mode.cmd`を1回ダブルクリックします。Brokerを別の窓で起動し、Brokerのパイプが存在するまで待ってから、トンネルを起動します。これはパイプの存在確認であり、Brokerの応答確認ではありません。両方の窓を開いたままにしてください。停止するときは、先にトンネルの窓で`Ctrl+C`、次にBrokerの窓で`Ctrl+C`を押します。

トンネルだけを再起動するには、Brokerを動かしたまま`tools\start-sora-commander-broker-dev.cmd`を使います。

Windowsにサインインしたままにしてください。画面がロックされていると承認窓を開けません。

**補足の通常モード：** `tools\start-sora-commander-local.cmd`を1回ダブルクリックし、その窓を開いたままにします。このモードにはBrokerモードの再起動後の二重実行防止がありません。結果不明のコマンドをやり直すためにモードを変えないでください。例のYAMLはBrokerクライアントを使っています。通常モードでは、起動前にcommandを同じフォルダの`next-development.cmd`へ変更する必要があります。

## 6. ChatGPTに接続する

利用条件、ワークスペースの権限、接続方法は、OpenAI公式ヘルプの最新の[Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)（[日本語](https://help.openai.com/ja-jp/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)）に従ってください。案内に従って自分のSecure MCP Tunnelを使います。画面名や手順は変わることがあります。接続後、新しいチャットで`ping`、`status`、`list_sessions`、`tool_catalog`を依頼してください。

期待する状態は、`status`の`remote_ready: false`、`approval: LOCAL_DEMO_ONLY`と、ツール数44です。

ChatGPTのファイル操作ツールは、設定した許可フォルダに届きます。設定ファイルがなければcheckoutだけです。**承認したコマンドは本人のWindows権限で動き、そのアカウントでできる操作を実行できます。**

## 問題が起きたとき

| 表示 | 意味 | 対応 |
|---|---|---|
| `A tunnel is already running or port 8080 is in use` | トンネルが動作中、または他のプログラムがポートを使用中 | 2つ目を起動しないでください |
| `A tunnel is running. Stop it first` | Brokerをトンネルより先に起動する必要がある | トンネルを止め、上記の順に起動してください |
| `No key file`または`The key file must hold the key only` | キーがない、または形式が不正 | 手順3をやり直してください |
| `Development virtual environment not found` | `.venv`がない | 手順1を実施してください |
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

## テストを実行する

```powershell
$env:PYTHONPATH = "src;."
$m = Get-ChildItem tests\test_*.py | ForEach-Object { "tests." + $_.BaseName }
.venv\Scripts\python.exe tests\run_with_real_dialog_guard.py @m
```

公開版に含まれるのは、開発用テストの一部です。このコマンドは手元のコピーに含まれるテストモジュールを、実際の承認窓を開こうとすると失敗させるguard付きで実行します。開発用の全回帰テストではありません。トンネルは不要で、数分かかります。パスに`temp`というフォルダや、ドットで始まるフォルダを含めないでください。一部のテストがスキップされることがあります。
