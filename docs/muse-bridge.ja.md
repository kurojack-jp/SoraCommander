# 読み取り専用Museブリッジ

自分のTailscale tailnet内のエージェントから、Sora Commanderを通じて
読み取るための非公式ブリッジです。Windows専用で、PCを起動したままに
する必要があります。ファイルツールは読み取り専用、コマンドは拒否され、
ブリッジから承認窓は開きません。Brokerと並行して動かせます。
このリリースで提供するのは読み取り専用アクセスだけです。

## 設定

1. [INSTALL](INSTALL.ja.md)に従ってPython環境を準備します。PCでTailscaleを
   導入・起動し、エージェントのノードを同じtailnetに参加させます。
2. [muse-bridge-peers.example.json](muse-bridge-peers.example.json)を
   `%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json`にコピーします。
   手元で`tailscale status --json`を実行し、エージェントのノードの`ID`
   フィールド（StableID、末尾が`CNTRL`）を探します。`readonly`内の例をその値に置き換え、`exec`は空の
   リストのままにします。statusの出力やpeersファイルを共有しないでください。
3. checkout内の`tools\start-muse-bridge-tailscale.cmd`を起動し、窓を開いたままに
   します。窓に表示されたTailscaleアドレスのポート8765、`/mcp`エンドポイントに
   エージェントを接続します。

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
| エージェントにHTTP 401が返る | そのノードが`readonly`に無い、またはtailnetに入っていない | StableIDを確かめ、ファイルを直したらブリッジを再起動する |
| コマンドが`DENY`になる | 想定どおり。読み取り専用ではコマンドは拒否される | 何もしない。読み取り用のツールを使う |
| コマンドで`Invalid tool request`になる | 要求の形が不正（例：`executable`が絶対パスでない） | どちらにせよ実行されない。読み取り専用ではコマンドは拒否される |
| `[HOLD] allowed-roots.txt is missing` | 読み取り専用ブリッジにはフォルダーの指定が必要 | `allowed-roots.txt`を作成し、公開するフォルダーを記載してブリッジを再起動する |

## 拒否される場合

読み取りツールとコマンドは、UNCパスとWindowsデバイスパスを解決前に拒否します。
`allowed-roots.txt`が無い場合、読み取り専用ブリッジは起動しません。
exec用トークンには、`SORA_BRIDGE_TOKEN`を含むどの読み取り専用トークンとも異なる値を使います。

peersファイルの欠落・空ファイル・不正形式、または`readonly`が空の場合は
起動しません
（`tests.test_muse_bridge_identity.StartupTests.test_missing_invalid_empty_lists_never_listen`、
`PeerFileTests.test_schema`）。Tailscaleが停止していればアドレス検出に失敗して
起動できない場合があります。代替アドレスで起動できても、本人確認の照会失敗は
拒否されます。CLIの欠落、照会失敗、リスト外のノードはHTTP 401になります
（`tests.test_muse_bridge_identity.IdentityTests.test_all_lookup_refusals_are_401`、
`test_timeout_negative_cache`）。照会成功は最大60秒、失敗は5秒キャッシュされます。

読み取り専用ポリシー、コマンド拒否、承認dispatcherがないことは
`tests.test_muse_bridge_exec.MainTests.test_default_readonly_host_and_banner_are_preserved`
が確認する契約です。これらは合成テストで、別ユーザーの環境の受入証拠ではありません。

## 制限

試したのは作者自身の環境だけです。別アカウントのMuseが同じ方法で自分の
tailnetに参加できるかは、まだ確認できていません。MuseはMetaの製品であり、
このプロジェクトはMetaと提携しておらず、Metaの推奨も受けていません。
2026年11月時点の案内で、ChatGPTとMuseのプラン条件は変わる可能性があります。
本番・一般公開の安全ゲートはHOLD、`remote_ready=false`のままです。
