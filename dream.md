## 2026-07-30 16:20 Dreamingタイム

### 今回やったこと
- `orchestrator/SPEC.md`（958行、既にQandA.mdで10件全てANSWERED済みの成熟した仕様）を起点に、`/spec-to-design`スキルでUSECASE.md/SEQUENCE.md/CLASS.md/UI.md/TESTCASE.mdの5点セットを新規作成
- 各成果物をCodex CLIで個別レビュー（USECASE.mdは丁寧レビュー、以降は軽量レビュー）し、指摘を都度反映してからAPPROVEDを得た

### 気づいたこと
- SPEC.mdが非常に詳細（返信確認、STALE復旧、エラー通知の再帰防止など）だったため、USECASE/SEQUENCE/CLASS/TESTCASEの各段階で「既読化済みメールの再試行禁止」「HUMAN_REQUIREDの終端状態」「エラー通知必須項目」など、細部の見落としを毎回1〜2周の指摘で拾われた
- Codexレビューは「起動失敗（未受信）」と「起動成功後の異常終了/タイムアウト（受信済み扱い）」の区別を特に重視しており、この境界線がSPEC全体を貫く最重要ルールだった

### 改善点
- 初回生成時点で「未受信 vs 既読後」の区別をテストケース・クラス設計の両方で最初から明示しておけば、レビュー往復を1回減らせた可能性がある
- QandA.mdがプロジェクトルート（orchestrator配下ではない）にある点は、SPEC.mdのフォルダ構成通りだが見落としやすいので次回も先に場所を確認する

### 次に試すとよさそうなこと
- 実装フェーズ（orchestrator.py等の実コード）に進む際は、CLASS.mdの`OutcomeClassifier`と`RetryPolicy`の境界（mail_received判定）を最初にユニットテスト化すると手戻りが少なそう
- TESTCASE.mdのT-110（初期動作確認15手順の通しシナリオ）を最初の統合テストとして実装すると、他の個別テストの前提条件（AI登録・メール送受信）を自然に満たせる
