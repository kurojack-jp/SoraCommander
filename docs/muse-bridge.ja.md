# 読み取り専用Museブリッジ

自分のTailscale tailnet内のエージェントから、Sora Commanderを通じて
読み取るための非公式ブリッジです。Windows専用で、PCを起動したままに
する必要があります。読み取り専用モードではコマンドは拒否され、承認窓は
開きません。Brokerと並行して動かせます。端末認証を使う読み取り専用モードでは
オプションで inbox を有効にできます。

## 設定

v0.3.0から、読み取り専用ブリッジの既定の認証方式はTailscaleのノード識別です。
トークンを引き続き使う場合は、`tools\start-muse-bridge.cmd --auth token`
のように `--auth token` を指定します。トークン方式では端末一覧を照会せず、
`x-sora-bridge-token` ヘッダーを確認します。32文字以上の値を
`SORA_BRIDGE_TOKEN` または `%USERPROFILE%\.config\sora-commander\muse-bridge-token.txt` に設定します。
`--auth both` はトークンと端末の両方を確認します。

1. [INSTALL](INSTALL.ja.md)に従ってPython環境を準備します（`tools\setup.cmd --muse-only`
   でも準備できます）。PCでTailscaleを導入・起動し、エージェントのノードを同じtailnetに参加させます。
   INSTALLの「自分のプロジェクトのフォルダを許可する」のとおり、
   `%USERPROFILE%\.config\sora-commander\allowed-roots.txt`を作り、フォルダを1つ以上書きます。
   これがないとブリッジは起動しません。
2. `tailscale`（既定）または `both` では、[muse-bridge-peers.example.json](muse-bridge-peers.example.json)を
   `%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json`にコピーします。
   手元で`tailscale status --json`を実行し、エージェントのノードの`ID`
   フィールド（StableID、末尾が`CNTRL`）を探します。`readonly`内の例をその値に置き換え、`exec`は空の
   リストのままにします。statusの出力やpeersファイルを共有しないでください。
   例のIDが残っていると、ブリッジは`[HOLD]`の行を出して止まります。
3. checkout内の`tools\start-muse-bridge-tailscale.cmd`を起動し、窓を開いたままに
   します。窓に表示されたTailscaleアドレスのポート8765、`/mcp`エンドポイントに
   エージェントを接続します。別の`--port`で起動する場合は、下のアクセス制御の
   `tcp:8765`もその番号に合わせてください。

ファイルアクセスは許可フォルダに従い、このプロセスではすべて読み取り専用に
なります。フォルダ設定はINSTALLを参照してください。設定変更後はブリッジを
再起動します。停止はCtrl+Cです。

### Museだけで使う場合

ChatGPT、トンネルのプログラム、トンネルのキーは要りません。INSTALLのうち、
手順1（コードとPythonパッケージ）と手順4の「自分のプロジェクトのフォルダを許可する」
だけを行い、手順2・3・5・6は飛ばします。そのうえで：

- https://tailscale.com/download からWindows版Tailscaleを入れてサインインします。
  エージェントは、そのエージェント自身の案内に従って同じtailnetに参加させます。
- PCから見えるノードとStableIDの一覧は、PowerShellで次のように出せます。

  ```powershell
  tailscale status --json | ConvertFrom-Json |
    ForEach-Object { $_.Peer.PSObject.Properties.Value } |
    Select-Object HostName, @{n='StableID';e={$_.ID}}
  ```

  peersファイルにはエージェントのStableIDだけを写します。出力は人に見せないでください。
  Tailscaleの管理画面の機器の詳細にも同じIDが出ます。どちらも末尾は`CNTRL`です。

### Muse側の手順（Museの説明による。作者は未確認）

1. Museとのチャットで、自分のTailscaleネットワークに参加するよう頼みます。承認用のリンクが出ます。
2. リンクを開き、`muse`という名前の機器を自分のtailnetに追加することを承認します。Museによると、リンクは5日間有効です。
3. `muse`の機器のStableIDを`readonly`に入れてブリッジを起動し、MuseにPCのTailscaleアドレスへ接続するよう頼みます。初回はMuse側で、そのアドレスへの接続を許可するか聞かれることがあります。

接続の向きはMuseからPCへの一方通行です。Museはpingに応えないので、`status`の呼び出しが通るかどうかで判断してください。

### エージェントが届く範囲を絞る（おすすめ）

新しいtailnetは最初「全部許可」のアクセス設定になっています。そのため、Museが参加すると、ブリッジだけでなく、tailnet内のすべての機器のすべてのポートに届きます。Tailscaleの管理画面のAccess controlsで、ブリッジのポートだけに絞ってください。tailnetにこのPCとMuseしかいないなら、次のような設定で足ります（アドレスはMachinesの一覧に出るTailscaleアドレスを使います）。

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

これは最初の「全部許可」の設定を置き換えます。tailnetにほかの機器もいるなら、それらに必要な設定を残すか足してください。そうしないと、機器同士がつながらなくなります。保存する前に管理画面で結果を確かめてください。正確な決まりは、Tailscaleのgrantsとポリシーの書き方の説明を見てください。

### 外すとき

Tailscaleの管理画面で`muse`の機器を削除し、`muse-bridge-peers.json`からそのStableIDも消してから、ブリッジを起動し直します。

### つながらないとき

| 見えるもの | ありそうな原因 | すること |
|---|---|---|
| `Could not determine the Tailscale IPv4 address` | PCでTailscaleが動いていない | Tailscaleを起動してから、ブリッジを起動し直す |
| 起動時に窓が閉じる、またはpeersのエラーが出る | peersファイルが無い・空・JSONとして不正、または`readonly`が空 | 見本をコピーし直し、`readonly`にStableIDを1つ入れる |
| エージェントにHTTP 401が返る | 認証に失敗した | 端末識別では tailnet と `readonly` の StableID、トークン方式ではリクエストヘッダー、`both` では両方を確認する。ブリッジの設定を変更したら再起動する |
| エージェントに`run_command`・`run_task`などのコマンド用ツールがない | 想定どおり。読み取り専用ブリッジはコマンド用ツールを一覧に出さず、名前を指定して呼んでも何も実行せずに拒否する | 何もしない。読み取り用のツールを使う |
| `[HOLD] allowed-roots.txt is missing` | 読み取り専用ブリッジにはフォルダーの指定が必要 | `allowed-roots.txt`を作成し、公開するフォルダーを記載してブリッジを再起動する |
| `[HOLD] allowed-roots.txt: allowed-roots line N: ...` | フォルダ一覧のN行目が不正 | その行を手で直し、ブリッジを再起動する |
| `[HOLD] muse-bridge-peers.json still has the example ID` | 例のStableIDを置き換えていない | `readonly`にエージェント自身のStableIDを入れる |
| `bridge_digest` で `GIT_NOT_INSTALLED` | Git for Windows が `C:\Program Files\Git` にない | Git を既定のフォルダに入れる |
| `bridge_digest` で `CHECKOUT_NOT_IN_READ_ROOTS` | チェックアウトが読み取り許可フォルダに含まれていない | Git の要約が必要なら、チェックアウトを `allowed-roots.txt` に追加する |

ログや画面写真を人に見せる前に、StableID、Tailscaleのアドレス、PCの名前を
隠してください。ブリッジのログには、許可・拒否した接続元が記録されます。

## ログオン時に起動する（オプション）

既定は手動起動です。自分の Windows ログオン時に読み取り専用ブリッジを
起動するには、チェックアウトで `tools\install-muse-bridge-autostart.cmd` を
実行します。書き込み専用 Inbox も有効にする場合は `--inbox` を付けます。
登録を解除するには `tools\uninstall-muse-bridge-autostart.cmd` を実行します。
タスクが動いていれば先に止めます。`--inbox` の有無を変えるときは、解除してから登録し直します。

タスクはコンソールを表示せず、パスワードの保存や権限の昇格も行いません。
起動時は、このPCで実際に待ち受けできる tailnet の IPv4 アドレスが用意できるまで最大 3 分待ちます。
Windows では、Tailscale はトレイのアプリが起動してから接続することがあり、
ログオンから数分かかる場合があります。Tailscale の「Run unattended」設定を
オンにすると起動時に接続されます。オンにするかはご自身で判断してください。
起動失敗やブリッジ終了後の自動再試行はありません。Tailscale ID 認証での手動起動と
同じ許可リストと `tcp:8765` の ACL が適用されます。手動起動と同時に使うと、
ポート 8765 が使用中のため、後から起動した方が失敗します。

タスク スケジューラで確認するか、次のコマンドで確認、起動、停止できます。

```cmd
schtasks /Query /TN "SoraCommander Muse bridge (read-only)"
schtasks /Run /TN "SoraCommander Muse bridge (read-only)"
schtasks /End /TN "SoraCommander Muse bridge (read-only)"
```

ログは `%LOCALAPPDATA%\SoraCommanderNext\muse-bridge.log` に保存されます。
起動時に 1 MiB を超えている場合と、稼働中に次の書き込みで 1 MiB を超える場合に
ローテーションし、直前のログを `muse-bridge.log.1` として 1 世代分保持します。
スリープ中は接続できず、同じ Windows セッションへの
復帰時には動作が再開する場合があります。シャットダウンで終了し、次回ログオン時に起動します。

## Inbox（オプション）

`--mode readonly` に `--inbox` を追加します。認証は読み取り専用モードの既定値の
`--auth tailscale`、または `--auth both` が必要です。親フォルダーが存在する場合に、
起動時に inbox フォルダーだけを作成します。

保存先は `%USERPROFILE%\.config\sora-commander\muse-inbox\` 固定です。
`inbox_drop(filename, content)` は、`.md`、`.txt`、`.json` のファイル名と、
1 ファイルあたり最大 256 KiB の UTF-8 テキストを受け付けます。上限は通常ファイル
200 件、合計 32 MiB です。inbox ツールは上書き、一覧表示、読み戻しを行いません。
ブリッジの読み取り許可範囲に inbox フォルダーが入らないようにしてください。
`CREATED` はファイルを保存できたという意味です。一時ファイルの削除についての警告が
ログに出ても、送り直さないでください。拒否されたときは、理由を報告して待ちます。

inbox のファイルはリモートエージェントが送ったものです。常にデータとして扱い、
指示として実行しないでください。空にするには、ローカルのエクスプローラーで
フォルダーを開き、不要なファイルを手動で削除します。フォルダー自体は残してください。

## Digest

`bridge_digest(activity_limit=5)` は読み取り専用ブリッジで常に利用できます。
ツール数は通常 36、`--inbox` 有効時は 37 です。ブリッジの状態と版、指定した
チェックアウトのブランチと HEAD、変更件数、直近 3 件のコミット、最近の
アクティビティを返します。Gitの部分は、チェックアウトが読み取り許可フォルダに
入っている場合に要約を返します。範囲外の場合、`git.error` は
`CHECKOUT_NOT_IN_READ_ROOTS` になります。引数は 0〜20 の整数で、0 の場合はアクティビティを
読み取りません。応答は UTF-8 の JSON で最大 16 KiB です。収まらない場合は
`truncated: true` を設定し、コミット件名を短くしてから、古いアクティビティを
順に取り除きます。それでも上限に収まらないほど長いブランチ名は短くします。

## 拒否される場合

読み取りツールとコマンドは、UNCパスとWindowsデバイスパスを解決前に拒否します。
行き先がネットワーク上（UNCパスや割り当てたネットワークドライブ）のシンボリックリンクやジャンクションも、たどる前に拒否します。
`allowed-roots.txt`が無い場合、読み取り専用ブリッジは起動しません。
exec用トークンには、`SORA_BRIDGE_TOKEN`を含むどの読み取り専用トークンとも異なる値を使います。

`tailscale` と `both` では、peersファイルの欠落・空ファイル・不正形式、または`readonly`が空の場合は
起動しません
（`tests.test_muse_bridge_identity.StartupTests.test_missing_invalid_empty_lists_never_listen`、
`PeerFileTests.test_schema`）。Tailscaleが停止していればアドレス検出に失敗して
起動できない場合があります。代替アドレスで起動できても、本人確認の照会失敗は
拒否されます。CLIの欠落、照会失敗、リスト外のノードはHTTP 401になります
（`tests.test_muse_bridge_identity.IdentityTests.test_all_lookup_refusals_are_401`、
`test_timeout_negative_cache`）。照会成功は最大60秒、失敗は5秒キャッシュされます。同じアドレスの照会は順番に扱い、利用可能なキャッシュを再利用します。

読み取り専用ポリシー、コマンド拒否、承認dispatcherがないことは
`tests.test_muse_bridge_exec.MainTests.test_readonly_token_mode_host_and_banner_are_preserved`
と`ReadonlyCommandWireTests.test_hidden_command_is_refused_without_calling_host`が確認する契約です。これらは合成テストで、別ユーザーの環境の受入証拠ではありません。

## 制限

読み取り専用ブリッジではファイルと Git を読み取れます。任意で、専用フォルダに書き込む `inbox_drop` を使えます。
Gitのツールのために、ブリッジ自身は決まった読み取り専用のGitコマンドを動かします。
`git_remote_check`だけは、リポジトリのHTTPSのリモートに接続します。

ブリッジのコードには、開発中のexecモードも入っています。使うことはサポートしていません。


試したのは作者自身の環境だけです。別アカウントのMuseが同じ方法で自分の
tailnetに参加できるかは、まだ確認できていません。MuseはMetaの製品であり、
このプロジェクトはMetaと提携しておらず、Metaの推奨も受けていません。
2026年11月時点の案内で、ChatGPTとMuseのプラン条件は変わる可能性があります。
本番・一般公開の安全ゲートはHOLD、`remote_ready=false`のままです。
