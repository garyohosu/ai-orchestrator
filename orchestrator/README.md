# orchestrator

Windows 11上で、SQLiteベースのメールシステム（`mail`パッケージ）とAI CLI（Codex CLI / Claude Code）を組み合わせ、未読メールがあるAIだけを必要なときに起動するPythonオーケストレーターです。詳細な要求仕様は[`SPEC.md`](SPEC.md)を参照してください。

## 目的

- 指揮AIと作業AIがメールを介してやり取りする作業依頼・成果物・QandA・回答・修正指示を、人手を介さず巡回・仲介する
- 未読メールがないAIは起動せず、トークン消費とPC負荷を抑える
- CLI起動失敗・異常終了・タイムアウト・返信なしを検出し、元メールの送信者へエラー通知を送る

## 前提

- OS: Windows 11
- シェル: PowerShell
- Python: 3.10以上（PCへ通常インストールされたもの。`py`ランチャーの利用を推奨）
- Codex CLI、Claude CodeなどのAI CLIを、事前にインストール・認証済みであること

## 仮想環境・pipは不要

`orchestrator`はPython標準ライブラリだけで動作します。`.venv`/`venv`/`pipenv`/`poetry`/`conda`の作成や`pip install`は一切不要です。同階層の`mail`パッケージも同様に標準ライブラリだけで動作します。

## mailフォルダとの配置関係

`orchestrator`は、常に次のようにプロジェクトルート直下へ`mail`フォルダと並べて配置します。

```text
project-root/
├── mail/            ← 別途用意するメールシステム本体
│   ├── __init__.py
│   ├── agent_mail.py
│   └── data/
│       └── agent_mail.db
│
├── orchestrator/    ← 本フォルダ
│   ├── SPEC.md
│   ├── README.md
│   ├── orchestrator.py
│   ├── config.json
│   └── ...
│
└── QandA.md
```

すべてのパス解決は`orchestrator.py`自身のファイル位置を基準に行われ、カレントディレクトリには依存しません。`mail`フォルダは`orchestrator`フォルダの親（`project-root`）の直下に存在する必要があります。

## フォルダ単位でのコピー方法

`orchestrator`フォルダは、他のプロジェクトへそのままコピーして再利用できます。

1. コピー先のプロジェクトルートに、動作確認済みの`mail`フォルダを配置する
2. `orchestrator`フォルダを丸ごとコピーする（`logs/`・`checkpoints/`・`runtime/`の中身は空のままでよい。`.gitkeep`だけ残す）
3. コピー先の`orchestrator/config.json`を、そのプロジェクトの実際のAI UID・起動コマンドに合わせて書き換える
4. ソースコード（`.py`ファイル）は一切書き換えずに動作すること

コピー元プロジェクト固有のPythonモジュール・絶対パス・環境変数・設定ファイルへは依存しません。

## config.jsonの設定方法

`orchestrator/config.json`に、プロジェクトごとの設定を記述します。省略した項目には安全な既定値が使われます。

```json
{
  "mail_check_interval_sec": 5,
  "cli_timeout_sec": 1800,
  "reply_check_timeout_sec": 30,
  "max_round_trips": 10,
  "max_retries": 2,
  "max_run_duration_sec": 14400,
  "project_path": "",
  "logs_dir": "logs",
  "checkpoints_dir": "checkpoints",
  "runtime_dir": "runtime",
  "cli_output_max_bytes": 1048576,
  "cli_output_ring_bytes": 65536,
  "notification_tail_bytes": 8192,
  "max_handoffs": 3,
  "agents": [
    { "name": "commander", "uid": "UID000001", "cli_type": "codex", "command": [] },
    { "name": "worker", "uid": "UID000002", "cli_type": "claude_code", "command": [] }
  ]
}
```

- `agents[].uid`は、事前に`mail.register_user()`で登録済みのUID（`UID`+6桁以上の数字）を指定します。未登録のUIDが設定されている場合、起動時に明確なエラーで停止します。
- `agents[].cli_type`は`"codex"`または`"claude_code"`のいずれかです。
- `agents[].command`を空配列のままにすると、CLIは次の順で自動検出されます: (1) `command`に明示されたコマンド, (2) WindowsのPATH上の既定コマンド名（`codex`/`claude`）。特別な起動フラグが必要な環境では、`command`に完全なコマンド配列（例: `["C:\\tools\\claude.exe", "-p", "--dangerously-skip-permissions"]`）を明示してください。
- `agents`の並び順がAIの処理順になります（登録UID順ではありません）。
- `agents[].fallback_agents`にはRATE_LIMITED時の代替AI名を順序付きで指定できます。訪問済みAI、引継ぎ上限、二重送信は依頼ID単位で管理されます。
- `cli_output_max_bytes`（既定1 MiB）はstdout/stderr各証跡ファイルの上限、`cli_output_ring_bytes`（既定64 KiB）は判定用末尾リングバッファ、`notification_tail_bytes`（既定8 KiB）は通知掲載量、`max_handoffs`（既定3）は依頼ID単位の自動引継ぎ上限です。
- `project_path`を省略すると、対象プロジェクトは`project-root`（`orchestrator`の親ディレクトリ）になります。指定する場合は`project-root`を基準とした相対パスとし、正規化後に`project-root`の外へ出る指定は拒否されます。
- APIキー・パスワード・認証トークンを`config.json`へ書いてはいけません。

## CodexとClaude Codeの事前認証

各CLIの認証（ログイン、APIキー設定など）は、`orchestrator`を起動する前に、利用者自身がそれぞれのCLIで完了させておく必要があります。`orchestrator`は認証情報を保持・設定しません。

## PowerShellからの起動方法

プロジェクトルートから:

```powershell
python .\orchestrator\orchestrator.py
```

`orchestrator`フォルダから:

```powershell
python .\orchestrator.py
```

Python Launcherを使う場合（`python`コマンドがWindowsストアのエイリアスに奪われている環境など）:

```powershell
py -3 .\orchestrator\orchestrator.py
```

実行場所やカレントディレクトリに関わらず、同じ`mail`フォルダと対象プロジェクトを検出します。

### `--once`（一巡モード）

```powershell
py -3 .\orchestrator\orchestrator.py --once
```

全AIの未読を一度確認し、必要なAIを起動して結果を確認したうえで終了します。起動直前の競合で未読が0件になった場合は「対象なし（NO_WORK）」として正常終了します。

### 常時監視モード

引数なしで起動すると、`mail_check_interval_sec`ごとに未読メールを確認し続けます。`max_run_duration_sec`（既定14400秒=4時間）に達すると安全停止します。`0`を指定した場合だけ無期限で稼働します。

## 停止方法

- **Ctrl+C**: 1回目は新規AIの起動を止め、実行中AIの終了を（`cli_timeout_sec`まで）待ってから終了します。実行中に2回目のCtrl+Cを送ると、実行中のCLIを強制終了し、その旨をログ・引継ぎへ記録したうえで元メールの送信者へ通知してから終了します。
- **`orchestrator/runtime/stop.request`**: このファイルを作成すると、Ctrl+Cの1回目と同じ扱いで安全停止します。処理済みの`stop.request`は自動的に削除されます。

## ログとチェックポイント

- `orchestrator/logs/orchestrator.jsonl`: AIの起動・終了ごとの記録（JSON Lines形式）。日時・依頼ID・メールID・起動AI・実行コマンド（秘密情報を除く）・開始/終了時刻・終了コード・実行結果・エラー・次の宛先を含みます。APIキー・認証情報・Cookie・秘密の環境変数は記録されません。
- `orchestrator/checkpoints/{依頼ID}.json`: 依頼ごとの引継ぎ情報（目的・現在の状態・決定事項・成果物・未解決事項・次に行うこと）。オーケストレーターやAIが再起動しても、この情報から処理を継続できます。
- `orchestrator/runtime/`: 二重起動防止用の実行中情報、`terminal_mail_ids.json`（失敗確定済みメールの記録。同じ失敗通知が無限に生成されるのを防ぎます）、`stop.request`を保存します。

## エラー通知

CLIが見つからない・起動できない・異常終了した・タイムアウトした・正常終了したが返信がない、などの場合、`orchestrator`はメールシステムへ`orchestrator`という名前でシステムユーザー登録し、そのUIDから元メールの送信者へエラー通知メールを送信します。通知本文には、状態・依頼ID・元メールID・元の送信者UID・処理対象AI・失敗段階・失敗理由・CLI終了コード・実行時間・再試行回数・最終試行日時・元メールの状態・推奨する次の対応が含まれ、秘密情報や本文全文は含まれません。

対象AIがメールを受信していないことが明らかな場合（CLI起動失敗など）に限り、`max_retries`まで自動再試行します。一度失敗が確定したメールは`runtime/terminal_mail_ids.json`に記録され、以後同じメールに対して再起動やCLI起動、重複した通知が発生しないようになっています。

## 既知の制限

- **往復回数（`max_round_trips`）の判定**: 完了報告か修正指示かを外部から機械的に区別できないため、「完了と確認できた返信（`SUCCESS`）」の回数だけをカウントします。カウントは`runtime/round_trips.json`へ永続化され、オーケストレーターを再起動しても引き継がれます。
- **CLIの正確な非対話フラグ**: `claude`（Claude Code）の非対話実行に必要な正確なコマンドラインオプション（権限モードなど）は環境によって異なる場合があります。既定では`claude -p`のみを付与するため、環境に応じて`config.json`の`agents[].command`で必要なフラグを明示してください。
- **返信先UIDの本文指定書式**: 元メール本文に返信先を明示する場合は、`返信先UID: UIDxxxxxx`という行を含めてください。この書式はSPEC.md上で正式に定義されたものではなく、本実装での運用上の取り決めです。
- **同時実行AI数**: 初期版はAIを1人ずつ直列に起動します。複数AIの同時実行はサポートしません。

## テスト方法

### 単体テスト（`mail`パッケージ不要）

```powershell
Set-Location .\orchestrator
py -3 -m unittest discover -s tests -p "test_*.py"
```

すべてインメモリのテストダブル（`tests/fakes.py`の`InMemoryMailAdapter`）を明示的に注入して実行されるため、実物の`mail`パッケージは不要です。`pytest`などの外部パッケージにも依存しません。

### 実`mail`パッケージとの統合試験・コピー試験

1. 動作確認済みの`mail`フォルダと、`orchestrator`フォルダだけを一時プロジェクトへ配置する
2. `config.json`に、その一時プロジェクトで`mail.register_user()`により登録した実際のUIDを設定する
3. `py -3 .\orchestrator.py --once`をPowerShellから実行し、未読メールの検出・CLI起動・`find_mails`による返信確認・エラー通知が正しく動作することを確認する

`orchestrator`はSQLiteのテーブル名・列名・接続方法を一切知らず、常に`mail`パッケージの公開関数（`register_user` / `list_users` / `send_mail` / `check_mail` / `receive_mail` / `find_mails`）だけを経由します。`orchestrator`フォルダを削除しても、`mail`パッケージ単体でのメール送受信は引き続き動作します。
