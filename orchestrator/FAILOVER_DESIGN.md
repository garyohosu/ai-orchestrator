# CLI出力保存・利用制限引継ぎ設計

## 目的と範囲

CLIの実行結果を後から調査できるようにし、利用制限を通常のCLI失敗と区別する。`RATE_LIMITED`の場合は同じAIを再起動せず、同一依頼IDを維持したまま設定済みの代替AIへ引き継ぐ。対象は`orchestrator`側の仕様・設計・試験であり、CLIや`mail`パッケージの仕様変更は含めない。

## 設定項目と既定値

既存の`config.json`に次を追加する。省略時は安全な既定値を適用する。

```json
{
  "cli_output_max_bytes": 1048576,
  "cli_output_ring_bytes": 65536,
  "notification_tail_bytes": 8192,
  "max_handoffs": 3,
  "agents": [
    {
      "name": "worker",
      "uid": "UID000002",
      "cli_type": "claude_code",
      "command": [],
      "fallback_agents": ["reviewer"]
    }
  ]
}
```

* `cli_output_max_bytes`: stdout、stderrそれぞれの保存上限。正の整数。上限を超えたデータは保存せず、読み捨てしながらパイプの読み取りは継続する。
* `cli_output_ring_bytes`: stdout、stderrそれぞれの判定用末尾リングバッファ上限。既定値65536バイト（64 KiB）。
* `notification_tail_bytes`: 通知本文へ掲載する各ストリームのマスキング後末尾上限。正の整数。
* `max_handoffs`: 依頼ID単位の自動引継ぎ回数上限。既定値3。0は自動引継ぎを無効にする。
* `agents[].fallback_agents`: エージェント名の順序付き配列。未指定は空配列。UIDやCLIコマンドは重複記載せず、元の`agents`定義を参照する。

設定値はプロジェクトルート外へ出ない既存の設定パス検証を受ける。出力ファイル名・ログ相対パスへメール本文、依頼本文、CLI出力を直接使用しない。

## 出力取得と保存

`CliLauncher`はstdoutとstderrをそれぞれ`subprocess.PIPE`へ接続する。親プロセスは専用の読み取りスレッド（または同等の非同期ストリームポンプ）をストリームごとに起動し、EOFまで最後のバイトを読み続け、次を行う。

1. チャンクを受信する。
2. チャンク境界をまたぐ秘密情報を見逃さないよう、マスカーの保留バッファを持つ。
3. マスキング後のバイト列を、証跡ファイルへ最大`cli_output_max_bytes`まで書き込む。
4. 上限到達後も子プロセスのパイプを読み続け、追加データは破棄する。
5. 証跡ファイルとは別に、マスキング後の最新`cli_output_ring_bytes`を末尾リングバッファへ保持する。
6. 保存バイト数、総読取バイト数、切捨て有無、マスキング規則ID、SHA-256を記録する。

これにより、出力全文をメモリへ蓄積せず、子プロセスのstdout/stderrパイプ詰まりも起こさない。利用制限判定は証跡ファイルの先頭部分だけでなく、末尾リングバッファを対象にする。保存ファイルは必ず次の形式で`orchestrator/logs/`配下に作成する。

```text
logs/cli-output/<safe-job-id>/<attempt>-<safe-agent-name>-stdout.log
logs/cli-output/<safe-job-id>/<attempt>-<safe-agent-name>-stderr.log
```

ファイルはマスキング済み出力であり、SHA-256も保存済みマスキング後バイト列に対して計算する。出力ディレクトリは起動前に作成し、パス正規化後も`logs/`の配下であることを検証する。ファイル作成・書込み失敗はCLI結果とは別の内部障害としてログ・チェックポイントへ記録し、依頼は`HUMAN_REQUIRED`とする。

## マスキングと通知

共通マスカーは、少なくともAPIキー、Bearerトークン、パスワード、シークレット、Cookie、URL埋込み認証情報、既知の環境変数形式を対象とする。CLIアダプターはCLI固有の認証エラー表現を追加登録できる。検出不能な秘密があり得るため、出力ファイル・ログ・メールのいずれにも生出力を保存しない。

エラー通知には判定用リングバッファからstdout/stderrそれぞれのマスキング後末尾だけを掲載する。末尾はUTF-8の文字境界を壊さず、`notification_tail_bytes`を超えない。掲載が切り捨てられた場合は`[TRUNCATED]`を付加する。出力全文、プロンプト全文、個人情報を含む絶対パスは掲載しない。詳細調査用にはログ相対パスとSHA-256を掲載する。

## ログ形式

既存の`orchestrator/logs/orchestrator.jsonl`へJSON Linesで1実行1結果を記録し、次のフィールドを追加する。

```json
{
  "event": "cli_outcome",
  "job_id": "JOB-...",
  "origin_mail_id": 12,
  "agent_name": "worker",
  "status": "RATE_LIMITED",
  "exit_code": 1,
  "stdout": {
    "relative_path": "logs/cli-output/JOB-.../1-worker-stdout.log",
    "sha256": "...",
    "saved_bytes": 1234,
    "truncated": false
  },
  "stderr": {
    "relative_path": "logs/cli-output/JOB-.../1-worker-stderr.log",
    "sha256": "...",
    "saved_bytes": 5678,
    "truncated": true
  },
  "classification": {
    "source": "claude_code_adapter",
    "matched_stream": "stderr",
    "rule_id": "claude.rate_limit.session_limit",
    "evidence": "session limit"
  },
  "handoff_count": 1,
  "visited_agents": ["worker"],
  "next_agent": "reviewer"
}
```

`evidence`は秘密情報を除去した短い定型句またはマッチした規則IDに限定し、生の行全文を記録しない。コマンド、環境変数、出力内容の未加工値はログへ書かない。

## CLIアダプターによる判定

共通インターフェースを次のように拡張する。

```text
class CliAdapter:
    classify_output(exit_code, timed_out, stdout_tail, stderr_tail) -> CliEvidence
```

`CliEvidence`は`rate_limited: bool`、`rule_id`、`stream`、マスキング済みの短い`evidence`を持つ。stdout/stderr全文は渡さず、保存ポンプが作った有限長の末尾とストリーム中に検出した規則IDだけを渡す。Claude Code、Codexなど各アダプターが既知の文言・終了理由を実装し、共通分類器は判定根拠を変更せず記録する。

状態判定の優先順位は次のとおりとする。

1. プロセスを起動できない、または対象パス不在 → `DELIVERY_FAILED`
2. 起動済みでタイムアウトまたは強制終了 → `TIMEOUT`（同時にレート制限根拠が確定した場合は`RATE_LIMITED`）
3. アダプターが`rate_limited=true` → `RATE_LIMITED`
4. 0以外の終了コード → `FAILED`
5. 0終了かつ期待返信なし → `NO_REPLY`
6. 期待返信あり → `SUCCESS`

この順序は終了コードだけで利用制限を判定しないことを保証する。`RATE_LIMITED`は通常の`max_retries`再試行対象外とする。

## 引継ぎ状態と選択

チェックポイントへ次を追加する。

```json
{
  "handoff_count": 1,
  "visited_agents": ["worker"],
  "handoff_history": [
    {
      "from_agent": "worker",
      "to_agent": "reviewer",
      "reason": "RATE_LIMITED",
      "at": "2026-07-31T...Z",
      "classification_rule": "claude.rate_limit.session_limit"
    }
  ]
}
```

代替AIは、現在担当エージェントの`fallback_agents`を先頭から調べ、次の条件をすべて満たす最初の候補を選ぶ。

* `agents`に定義され、UIDが有効である。
* `visited_agents`に含まれない。
* `handoff_count < max_handoffs`である。
* 現在の依頼を同時実行中でない。

選択した候補へ、オーケストレーターのシステムUIDから同じ依頼IDを含む`[JOB][HANDOFF]`メールを送信する。本文には元メールID、元送信者UID、目的、現在状態、成果物、未解決事項、次の行動、担当履歴、引継ぎ回数、前担当のRATE_LIMITED判定根拠、出力ログの相対パスとSHA-256を含める。元メール本文やCLI出力全文は転送しない。

送信成功後にチェックポイントを保存し、`HANDOFF_SENT`から代替AIの`RUNNING`へ遷移する。送信失敗は候補を利用不能とみなし、次の候補へ進む。候補が尽きた、循環が検出された、上限に達した、または通知・チェックポイント保存に失敗した場合は`HUMAN_REQUIRED`へ遷移し、元の送信者へ通知する。RATE_LIMITEDの元AI自身、同じUIDの別名、既訪問AIへは引き継がない。

## 状態遷移

```text
RUNNING
  ├─ 起動失敗 ───────────────> DELIVERY_FAILED ─> 通常の未受信再試行
  ├─ 通常の異常終了 ──────────> FAILED
  ├─ タイムアウト ────────────> TIMEOUT
  ├─ レート制限根拠あり ───────> RATE_LIMITED
  └─ 正常終了＋返信なし ───────> NO_REPLY

RATE_LIMITED
  ├─ 候補あり ─> HANDOFF_PENDING ─> HANDOFF_SENT ─> RUNNING(代替AI)
  └─ 候補なし／循環／上限 ────────> HUMAN_REQUIRED
```

`RATE_LIMITED`、`HANDOFF_PENDING`、`HANDOFF_SENT`、担当履歴は依頼ID単位で永続化し、再起動時に再送による二重引継ぎを防止する。新しい担当の処理失敗は、RATE_LIMITED根拠がない限り自動的にさらに引き継がず、既存の`FAILED`／`TIMEOUT`／`NO_REPLY`規則に従う。

## メール本文の必須項目

RATE_LIMITED通知およびHUMAN_REQUIRED通知には既存のエラー通知項目に加え、次を含める。

* 状態（`RATE_LIMITED`または`HUMAN_REQUIRED`）
* 依頼ID、元メールID、元送信者UID
* 失敗したAI名・UID、現在または最終担当AI
* CLI終了コード、失敗段階、実行時間、最終試行日時
* 判定根拠（アダプター名、規則ID、stdout/stderrの別、マスキング済み短文）
* stdout/stderrのマスキング済み末尾（各`notification_tail_bytes`以内）
* 出力ログの相対パス、SHA-256、切捨て有無
* 引継ぎ回数、担当履歴、選択された代替AIまたは候補なしの理由
* 元メール状態、推奨する次の対応

## 影響範囲と試験

* `SPEC.md`: 設定、ログ、障害分類、通知、引継ぎ、受け入れ条件を拡張。
* `USECASE.md` / `SEQUENCE.md`: CLI実行失敗、RATE_LIMITED、候補選択、HUMAN_REQUIREDの流れを追加。
* `CLASS.md`: 出力ポンプ、アダプター証拠判定、引継ぎ履歴・担当選択のクラス責務を追加。
* `UI.md`: RATE_LIMITED、引継ぎ先、HUMAN_REQUIRED、ログ相対パスの表示を追加。
* `TESTCASE.md`: パイプ詰まり、上限、SHA-256、マスキング、アダプター判定、循環・上限・全候補不能を追加。
* `QandA.md`: CLIごとの追加レート制限文言など、実機サンプルが必要な未確定事項を記録。

実装時は、出力を大量に生成するスタブで「保存上限を超えても子プロセスが終了する」こと、stdout/stderr同時出力、チャンク境界をまたぐ秘密情報、再起動途中のHANDOFF_PENDING、候補全滅を必須試験とする。
