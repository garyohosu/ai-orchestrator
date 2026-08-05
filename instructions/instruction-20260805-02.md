# ai-orchestrator正式リポジトリへのGrok・自動フェイルオーバー対応反映

## 目的

`C:\PROJECT\csv`でライブ実証に成功した、Codex CLI利用量制限からGrok CLIへの自動フェイルオーバー機能を、正式な`ai-orchestrator`リポジトリへ反映する。

このリポジトリの責務は、AI CLIの起動、Invocation-IDの生成と伝達、実行結果の分類、返信検出、終端判定、代替エージェントへの引き継ぎである。

## 対象リポジトリ

```text
C:\project\ai-orchestrator
```

参照元：

```text
C:\PROJECT\csv
```

統合環境のコードをそのまま丸ごとコピーするのではなく、差分、責務、テストを確認して正式リポジトリへ移植すること。

## 前提

`aiagent-mail`側の変更を先に完了・プッシュしてから着手する。

必要な依存バージョンまたはコミットを確認し、結果ファイルへ記録する。

## 作業開始前

```powershell
cd C:\project\ai-orchestrator
git status --short
git branch --show-current
git pull
```

未コミット変更を削除、破棄、上書きしない。

`AGENTS.md`、README、設定ファイル、アダプター、launcher、エラー分類、fallback処理、テストを先に確認する。

## ライブ実証済みの動作

統合環境では以下が成功している。

1. `codex_reviewer`を起動
2. Codex CLIが利用量制限を返す
3. 出力を`RATE_LIMITED`として分類
4. `fallback_agents`から`grok_reviewer`を選択
5. AIメールへGrok向け引き継ぎメールを作成
6. `grok_reviewer`を起動
7. Grokがメールを読んで作業
8. Grokが完了返信
9. 元ジョブが`COMPLETED`
10. 一時プロンプトファイルの残留なし

ログでは以下が確認されている。

- `handoff_count=1`
- `visited_agents=["codex_reviewer","grok_reviewer"]`
- Codex：`RATE_LIMITED`
- Grok：`COMPLETED`

## 重要な設計原則

AIメーリングシステムが作業指示の正本である。

Grokへジョブ指示全文をCLI引数として直接渡さない。

Grok CLIへ渡す内容は、Grokを起動して自分宛てメールを読ませるための、短い固定ブートストラップ指示だけとする。

実際の以下の内容は、Grok宛てのメールから取得する。

- JOB-ID
- Decision-ID
- 対象ファイル
- 作業指示
- レビュー条件
- reply protocol
- 元エージェントの失敗情報

## 実装要件

### 1. GrokCliAdapter

Grok CLIを正式なアダプターとして追加する。

最低限、以下に対応する。

- CLI存在確認
- 起動コマンド生成
- 固定ブートストラッププロンプト
- UTF-8
- stdoutとstderrの取得
- exit codeの取得
- タイムアウト
- プロセス終了
- Invocation-IDの記録
- 結果分類
- 完了返信の検出

GrokをCodex互換として無理に扱わず、独立したアダプターにする。

### 2. prompt transport

Claude CodeとCodex CLIは既存のstdin方式を維持する。

Grok CLIで必要な場合は`--prompt-file`方式を利用する。

ただし、prompt fileへ保存するのはジョブ全文ではなく、固定された最小限の起動指示だけとする。

例：

```text
You are grok_reviewer. Read and process your pending mail using the AI mail system. Follow the reply protocol in the message.
```

アダプターごとに以下のような配信方式を設定可能にする。

- stdin
- prompt_file

エージェント名の文字列による場当たり的な分岐にしない。

### 3. 一時ファイル

prompt_file方式では以下を守る。

- UTF-8で保存
- 試行ごとに一意のファイル名
- 成功時に削除
- 非ゼロ終了時に削除
- タイムアウト時に削除
- 起動失敗時に削除
- Python例外時に削除
- `finally`相当で後片付け
- 削除失敗時は本文を出さず警告
- 同時実行時に衝突しない
- 実行後に残留していないことをテストする

### 4. Codex利用量制限の分類

Codex CLIの以下のような出力を、汎用`FAILED`ではなくレート制限として分類する。

```text
You've hit your usage limit
try again at ...
```

必要に応じて以下の情報を保存する。

- classification
- provider
- agent
- retry_at
- retry_after
- original exit code
- sanitized error summary

想定分類例：

```text
codex.rate_limit.usage_limit
```

orchestratorの上位状態としては`RATE_LIMITED`として扱えること。

### 5. fallback_agentsへの接続

設定に存在するだけだった`fallback_agents`を、実際のCLI失敗経路へ接続する。

期待する経路：

```text
codex_reviewer
→ RATE_LIMITED
→ grok_reviewer
→ COMPLETED
```

以下を守る。

- 元のJOB-IDを維持
- Invocation-IDを試行ごとに生成
- attempt IDを記録
- handoff countを記録
- visited agentsを記録
- 同じエージェントへ無限再試行しない
- fallback候補間で循環しない
- 最大引き継ぎ回数を守る
- 完了後に再引き継ぎしない
- 全候補失敗時のみ上位へHUMAN_REQUIREDを返す
- 途中の失敗だけで元ジョブを終端しない
- 最終通知を重複送信しない

### 6. 引き継ぎメール

fallback先を選択したら、AIメーリングシステムを利用して新しい宛先へ委任メールを作る。

元のジョブ指示をCLIへ直接送り直さない。

引き継ぎメールに以下を含める。

- 元の依頼件名
- 元の依頼本文
- 元mail_id
- 親mail_id
- JOB-ID
- Decision-ID
- 元エージェント
- fallback先
- failure classification
- handoff count
- visited agents
- reply protocol

### 7. availability check

候補エージェントの利用可能性を確認できる構造にする。

例：

- CLIがPATHに存在する
- 必須オプションを利用できる
- 必要な設定が存在する
- アダプターが現在のOSに対応する

Antigravityは現在この環境でCLI未導入である。

旧Gemini CLIを現行fallback候補へ戻さない。

Antigravity未導入時は、他の利用可能な候補へのfallbackを継続する。

### 8. 設定

正式な設定ファイルへ以下を反映する。

- `grok_reviewer`
- 使用CLI
- 起動方式
- prompt transport
- 固定ブートストラップ
- fallback候補
- 最大handoff回数
- タイムアウト
- availability設定

ローカルの認証情報や秘密情報を設定例へ含めない。

## テスト要件

最低限、以下をテストする。

### 既存動作

- Claude Codeのstdin起動
- Codex CLIのstdin起動
- Invocation-IDの生成と伝達
- 返信検出
- 終端判定
- 制御通知除外
- 既存テスト全件成功

### Grok

- Grokコマンド生成
- 固定ブートストラップのみを渡す
- `--prompt-file`の正しい使用
- UTF-8の取り扱い
- 正常終了
- 非ゼロ終了
- タイムアウト
- CLI未導入
- 不正応答
- 一時ファイルの確実な削除
- プロンプト本文をログへ出さない

### レート制限

- Codex利用量制限メッセージの分類
- 再試行時刻の抽出
- 汎用FAILEDと区別される
- レート制限がfallback対象になる

### fallback

- CodexからGrokへ引き継ぐ
- 元のメール内容をGrok向けメールへ継承する
- Grok成功時に元処理を成功扱いにできる
- Grok失敗時に次候補へ進める
- 全候補失敗時のみ上位へ通知する
- 同一候補へ無限再試行しない
- 候補間で循環しない
- 最大handoff回数を守る
- 最終結果を一度だけ通知する
- terminal記録と実状態が矛盾しない

可能であれば、実CLIを呼ばないモックテストと、明示的に実行する統合テストを分離する。

## ライブ確認

単体・統合テスト成功後、正式リポジトリ環境で安全に確認可能なら、Codex利用量制限からGrokへのフェイルオーバーを再確認する。

本番データを破壊しないこと。

ライブ確認できない場合は、統合環境`C:\PROJECT\csv`で確認済みのログ、mail_id、ジョブ状態と、正式リポジトリのテスト結果を区別して報告する。

確認済みの統合環境実績を、正式リポジトリで実行したかのように記述しないこと。

## Git管理対象

コミット対象：

- アダプター
- launcher変更
- 結果分類
- fallback処理
- 設定例
- テスト
- ドキュメント

コミットしないもの：

- 実行ログ
- runtime
- PIDファイル
- stop.request
- 一時プロンプト
- メールDB
- 認証情報
- APIキー
- キャッシュ
- 実運用のローカルconfig

## 完了条件

- Grokアダプターが正式リポジトリへ追加された
- Codexレート制限を分類できる
- fallback_agentsが実失敗経路へ接続された
- メール経由でGrokへ引き継げる
- 一時ファイルが残留しない
- 既存テストを破壊していない
- 新規テストが全件成功する
- 結果報告を作成した
- コミットした
- プッシュした

## 結果報告

リポジトリ直下へ以下を作成する。

```text
result-20260805-02.md
```

次を記載する。

1. 結論
2. 変更ファイル
3. Grokアダプターの構造
4. prompt transportの設計
5. 固定ブートストラップの内容
6. Codexレート制限の分類方法
7. fallback処理
8. 設定変更
9. テストコマンド
10. テスト結果
11. ライブ確認の有無
12. 残る制約
13. コミットハッシュ
14. プッシュ先ブランチ

## コミットとプッシュ

推奨コミットメッセージ：

```text
feat: add Grok fallback for rate-limited AI agents
```

今回の変更だけを選択してコミットする。

```powershell
git status --short
git diff --check
git add <今回の対象ファイル>
git commit -m "feat: add Grok fallback for rate-limited AI agents"
git push
```

実行データを含めていないことを確認し、最後に`git status --short`を結果ファイルへ記録すること。