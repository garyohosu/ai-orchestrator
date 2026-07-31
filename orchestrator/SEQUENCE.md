# シーケンス図

本書は `USECASE.md` と `SPEC.md` を元に、主要フローと分岐をシーケンス図で示す。

## 1. 全体の基本フロー（初期動作確認、SPEC.md 29章の15手順に対応）

```mermaid
sequenceDiagram
    participant Human as 人間/ChatGPT
    participant Mail as mailパッケージ(SQLite)
    participant Orc as Pythonオーケストレーター
    participant Commander as 指揮AI(Codex CLI)
    participant Worker as 作業AI(Claude Code)

    Note over Human,Mail: 手順1: 指揮AIとClaude Codeをメールシステムへ登録
    Human->>Mail: register_user(指揮AI), register_user(作業AI)
    Mail-->>Human: 各AIのUIDを発行

    Note over Human,Worker: 手順2: 指揮AIからClaude Codeへ日本語の作業依頼を送信<br/>(人間/ChatGPTは起動者であり、メール上の送信者にはならない。SPEC.md 9.1/9.3/29章)
    Human->>Commander: 目的・制約・初期指示を与えて手動起動(初回のみ)
    Commander->>Commander: 依頼内容を確定し依頼IDを生成
    Commander->>Mail: send_mail(依頼ID付き, 宛先=作業AI, 送信者=指揮AI_uid)
    Note over Orc: オーケストレーターの自動処理はここから開始する

    Note over Orc,Mail: 手順3: オーケストレーターが未読を検出
    loop メール確認間隔ごと
        Orc->>Mail: check_mail(作業AI_uid)
    end
    Mail-->>Orc: 未読数 > 0

    Note over Orc,Worker: 手順4: Claude Codeを起動
    Orc->>Orc: 実行中AIの二重起動チェック
    Orc->>Worker: CLI起動(固定指示を渡す)
    Worker->>Mail: receive_mail(作業AI_uid)
    Mail-->>Worker: 未読メール一覧(既読化済み)

    Note over Worker: 手順5-6: 既存スキルを実行し成果物とQandA.mdを作成
    Worker->>Worker: 引継ぎ確認・既存スキル実行・成果物作成
    opt 不明点がある場合
        Worker->>Worker: QandA.mdへOPENで記録
    end

    Note over Worker,Mail: 手順7: Claude Codeが指揮AIへ完了メールを送信
    Worker->>Mail: send_mail(完了報告, 宛先=指揮AI, 依頼ID維持)
    Worker-->>Orc: CLI終了(終了コード0)
    Orc->>Mail: check_mail(作業AI_uid)
    Mail-->>Orc: 未読数 = 0
    Orc->>Mail: find_mails(sender_uid, recipient_uid, request_id, after_mail_id, sent_after)
    Mail-->>Orc: 返信メール(既読化しない)

    Note over Orc,Mail: 手順8: オーケストレーターが指揮AI宛てメールを検出
    Orc->>Mail: check_mail(指揮AI_uid)
    Mail-->>Orc: 未読数 > 0

    Note over Orc,Commander: 手順9: Codex CLIを起動
    Orc->>Commander: CLI起動(固定指示を渡す)
    Commander->>Mail: receive_mail(指揮AI_uid)
    Mail-->>Commander: 未読メール一覧(既読化済み)

    Note over Commander: 手順10: Codex CLIが成果物とQandA.mdを確認
    Commander->>Commander: 成果物・QandA.md(OPEN)確認

    Note over Commander,Mail: 手順11: Codex CLIが回答または修正指示をメール送信
    Commander->>Commander: QandA.mdをANSWEREDに更新(該当する場合)
    Commander->>Mail: send_mail(回答または修正指示, 宛先=作業AI, 依頼ID維持)
    Commander-->>Orc: CLI終了(終了コード0)
    Orc->>Mail: check_mail(指揮AI_uid)
    Mail-->>Orc: 未読数 = 0
    Orc->>Mail: find_mails(sender_uid, recipient_uid, request_id, after_mail_id, sent_after)
    Mail-->>Orc: 返信メール(既読化しない)

    Note over Orc,Worker: 手順12: オーケストレーターがClaude Codeを再起動
    Orc->>Mail: check_mail(作業AI_uid)
    Mail-->>Orc: 未読数 > 0
    Orc->>Worker: CLI起動(固定指示を渡す)
    Worker->>Mail: receive_mail(作業AI_uid)
    Mail-->>Worker: 未読メール一覧(既読化済み)

    Note over Worker: 手順13: Claude Codeが回答を反映
    Worker->>Worker: 回答・修正指示を反映し成果物を更新

    Note over Worker,Mail: 手順14: 完了メールを送信
    Worker->>Mail: send_mail(完了報告, 宛先=指揮AI, 依頼ID維持)
    Worker-->>Orc: CLI終了(終了コード0)
    Orc->>Mail: check_mail(作業AI_uid)
    Mail-->>Orc: 未読数 = 0

    Note over Orc,Human: 手順15: 未読メールがなくなり、全AIが終了
    Orc->>Mail: check_mail(指揮AI_uid)
    Mail-->>Orc: 未読数 = 0
    Orc->>Human: (完了条件を満たした成果物を人間が確認可能)
    Note over Orc: 追加の修正が必要な場合は、同じ依頼IDで手順9-14を繰り返す
```

## 2. CLI異常時の送信元通知フロー（SPEC.md 30章対応）

```mermaid
sequenceDiagram
    participant Sender as 元メール送信者
    participant Mail as mailパッケージ(SQLite)
    participant Orc as Pythonオーケストレーター
    participant CLI as 対象AI CLI

    Sender->>Mail: send_mail(依頼ID, 宛先=対象AI)
    Orc->>Mail: check_mail(対象AI_uid)
    Mail-->>Orc: 未読数 > 0

    alt CLIが見つからない/起動できない
        Orc->>Orc: 判定=DELIVERY_FAILED
        Orc->>Orc: 対象AIが未受信のため再試行(最大再試行回数まで)
    else CLI起動成功
        Orc->>CLI: CLI起動
        alt CLI実行タイムアウト超過
            Orc->>CLI: プロセス終了
            Orc->>Orc: 判定=TIMEOUT(既読化済みのため再試行しない)
        else 0以外の終了コード/予期せぬ終了
            Orc->>Orc: 判定=FAILED(既読化済みのため再試行しない)
        else 終了コード0
            Orc->>Mail: 返信確認タイムアウトまで find_mails でポーリング(既読化しない)
            alt 返信あり(送受信者UID一致・依頼ID一致・メールID>元メールID・起動後作成)
                Mail-->>Orc: 返信メール確認
                Orc->>Orc: 正常完了として記録
            else 返信なし
                Orc->>Orc: 判定=NO_REPLY
            end
        end
    end

    opt 判定がDELIVERY_FAILED/FAILED/TIMEOUT/NO_REPLYで再試行上限到達、または認証切れ等で判定不能
        Orc->>Orc: 判定不能な場合はHUMAN_REQUIREDとする
        Orc->>Mail: send_mail(エラー通知, 宛先=元メール送信者UID, 依頼ID維持, 状態をタグ付け)
        Orc->>Orc: 対象依頼を終端状態として記録(ログ・引継ぎ)
    end

    opt エラー通知の送信自体が失敗、または元送信者UIDが無効/orchestrator自身
        Orc->>Orc: 再帰通知を作らずHUMAN_REQUIREDとして記録
    end
```

## 2.5 RATE_LIMITED時の代替AI引継ぎフロー

```mermaid
sequenceDiagram
    participant Orc as オーケストレーター
    participant CLI as 現担当CLI
    participant Adapter as CLIアダプター
    participant Log as logs/
    participant Mail as メール
    participant Alt as 代替AI

    Orc->>CLI: stdout/stderrを個別PIPEで読み取り
    CLI-->>Orc: 終了（出力は逐次保存・上限後も読み捨て）
    Orc->>Adapter: 有限長末尾と終了情報を判定
    Adapter-->>Orc: RATE_LIMITED + rule_id + evidence
    Orc->>Log: 出力相対パス・SHA-256・根拠・担当履歴をJSONL記録
    alt 未訪問の代替AIあり、引継ぎ上限未到達
        Orc->>Orc: HANDOFF_PENDINGを永続化
        Orc->>Mail: 同じ依頼IDのHANDOFFメールを送信
        Orc->>Orc: HANDOFF_SENTと担当履歴を永続化
        Mail-->>Alt: 未読の引継ぎメール
        Alt->>Mail: receive_mailで取得・処理
    else 候補なし／循環／上限／送信・保存失敗
        Orc->>Orc: HUMAN_REQUIREDを永続化
        Orc->>Mail: 元送信者へマスキング済み通知
    end
```

判定根拠はCLIアダプターが返し、共通分類器は終了コードだけでRATE_LIMITEDを作らない。RATE_LIMITEDでは同じAIの通常再試行を行わない。

## 3. 常時監視モードの停止フロー（SPEC.md 25章対応）

```mermaid
sequenceDiagram
    participant Human as 人間
    participant Orc as Pythonオーケストレーター
    participant CLI as 実行中AI CLI
    participant Mail as mailパッケージ(SQLite)

    Human->>Orc: Ctrl+C または stop.request作成
    Orc->>Orc: 新規AI起動を停止
    alt 実行中AIなし
        Orc->>Orc: 直ちに終了
    else 実行中AIあり
        Orc->>CLI: 終了を待機(CLI実行タイムアウトまで)
        alt タイムアウト以内に終了
            Orc->>Orc: ログ・引継ぎへ記録
        else タイムアウト超過
            Orc->>CLI: プロセスを終了
            Orc->>Orc: 中断をログ・引継ぎへ記録
            Orc->>Mail: 必要に応じて元メール送信者へ通知
        end
        opt AI実行中に再度Ctrl+C
            Human->>Orc: 強制停止要求
            Orc->>CLI: 強制終了
            Orc->>Orc: 強制停止の事実をログ・引継ぎへ記録
            Orc->>Mail: 元メール送信者へ通知
        end
    end
    Orc->>Orc: 処理済みstop.requestを削除
    Orc->>Orc: オーケストレーター終了
```

## 4. STALE実行情報の復旧フロー（SPEC.md 24章対応）

```mermaid
sequenceDiagram
    participant Orc as Pythonオーケストレーター(起動時)
    participant Runtime as runtime/実行情報
    participant OS as Windows(プロセス確認)
    participant Mail as mailパッケージ(SQLite)

    Orc->>Runtime: 残存する実行情報を読み込む
    loop 各実行情報
        Orc->>OS: 保存PIDのプロセス開始時刻を照会
        alt PID一致かつ開始時刻一致
            Orc->>Orc: 実行中とみなす(何もしない)
        else PID不一致/開始時刻不一致/確認不可
            Orc->>Orc: STALE候補として記録
            Orc->>Mail: find_mails(元メールの既読状態を確認)
            Orc->>Mail: find_mails(sender_uid, recipient_uid, request_id,<br/>after_mail_id=元メールID, sent_after=記録された起動日時)
            Note over Orc,Mail: 返信の判定条件は30章と同一(依頼IDだけで判定しない)<br/>receive_mailは使用しない(元メールを奪って既読化し判定を破壊するため)
            alt 元メールが未読
                Orc->>Orc: 再処理候補として次回起動サイクルへ
            else 既読かつ返信なし
                Orc->>Mail: 元メール送信者へ異常終了通知
            else 既読かつ返信あり
                Orc->>Orc: 完了済みとして整理
            end
        end
    end
```
