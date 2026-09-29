# GitHub Actions 完全無料 48時間 Paper Test

この構成は、Oracle/VPSを使わず、GitHub Actionsで **48時間だけ** Polymarket Paper Botの耐久・統合試験を行うためのものです。
実取引は行いません。

## 費用

- GitHub Actions: **0円運用可能**
  - Public repository の標準GitHub-hosted runnerは無料。
  - Private repository のGitHub Freeには月2,000分が含まれます。
  - 本workflowは1回6分で強制終了、10分間隔、48時間なので、理論上の最大は scheduled 1,728分 + 初回manual 6分 = 1,734分です。
  - Privateで今月すでにActionsを多く使っている場合は途中で無料枠を使い切る可能性があります。その場合はPublicを選ぶとrunner料金は発生しません。
- Gemini API: **Free Tierのみ**。Google AI Studioで「課金を有効化していない」API keyを使用してください。
- Polymarket public API / 公開RSS: 追加料金なし。

## 重要

GitHub Actionsは今回 **ソフトウェアの48時間耐久・統合試験** として使用します。長期常設サーバー用途にはしません。
48時間完了後、workflow自身がscheduleをdisableします。
GitHubのscheduleは厳密なリアルタイムcronではなく、混雑時に遅延することがあります。

## 一番簡単な開始方法（Windows）

1. GitHubアカウントを用意する（カード不要）。
2. Google AI StudioでFree Tier API keyを作る。**Billingを有効化しない**。
3. このZIPを展開する。
4. PowerShellで展開したフォルダへ移動。
5. `SETUP_GITHUB_FREE.ps1` を実行。

```powershell
powershell -ExecutionPolicy Bypass -File .\SETUP_GITHUB_FREE.ps1
```

スクリプトはGitHub CLIが無い場合、その事実を表示して停止します。その場合は一度だけ以下を実行してください。

```powershell
winget install --id GitHub.cli -e
```

PowerShellを開き直して、もう一度 `SETUP_GITHUB_FREE.ps1` を実行してください。

## Public / Private

完全にActions分数を気にしたくない場合は **Public** を選択してください。API keyはGitHub Secretに保存され、リポジトリには書き込まれません。ただしコード・Paper取引ログは公開されます。

Paper取引ログも公開したくない場合は **Private** を選択してください。本workflowは48時間の理論最大を1,734 runner-minutesに制限していますが、GitHub Freeの月2,000分の残量を他用途で消費済みの場合は完走できないことがあります。

## 実行状態を見る

GitHubのリポジトリ → **Actions** → `Polymarket Free 48h Paper Test`。
各runのSummaryに仮想残高・PnL・取引数が表示されます。

状態は `freebot-data/free_status.json` にも保存されます。

## 48時間終了後

`free_status.json` が `COMPLETED` になると、workflowは自分自身をdisableし、それ以降のscheduled runを止めます。

## 再実験

新しい48時間実験を行う場合は、`freebot-data/` を削除してworkflowを再enableしてからmanual runしてください。過去データを残したい場合は先にZIP等で保存してください。
