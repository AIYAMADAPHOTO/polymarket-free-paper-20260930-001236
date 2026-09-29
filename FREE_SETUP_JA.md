# 完全無料で48時間動かす Polymarket Paper Bot

## 今回の推奨方式

**VPSは使いません。GitHub Actionsで約10分ごとにPaper処理を1回だけ実行し、状態をGitHubへ保存して48時間継続します。**

元X投稿の発想に寄せた検証用Botです。

1. Polymarket公開市場を最大1,000件走査
2. 出来高・流動性などで候補を絞る
3. 無料公開フィードから現在情報を取得
4. Gemini無料枠でfair value（YES確率）を推定
5. 実行可能価格・fee・板深度を考慮して8%以上のedgeが残る場合だけPaper Entry
6. Kelly式で資金配分し、1ポジション最大6%
7. 仮想50 USDで48時間記録

**実資金は動きません。ウォレット・秘密鍵・USDC・実注文コードはありません。政治・選挙・法案等の市場は対象外です。**

## 完全無料にする条件

### GitHub

最も確実にGitHub Actionsの追加料金を避けるなら、セットアップ時に **Public repository** を選びます。標準GitHub-hosted runnerはPublic repositoryでは無料です。

注意：PublicにするとソースコードとPaper取引ログは公開されます。Gemini APIキーはGitHub Secretへ保存されるためリポジトリには書き込みません。

Privateを選ぶこともできますが、GitHub Freeの月間Actions枠を消費します。今回のワークフローは1回最大6分、10分間隔・48時間で理論上最大約1,734分ですが、その月に別のActionsを使っていれば残量が減っています。

### Gemini

- Google AI Studioで、**課金をリンクしていないプロジェクト**のAPIキーを使います。
- 既定モデル：`gemini-3.5-flash-lite`
- fallback：なし（primaryが利用不可ならその周期を安全にスキップ）
- 1日150回のローカルハード上限を設定しています。
- 429 / quota超過時はその周期をスキップし、有料モデルへ切り替えません。
- Google Search groundingは使いません。

## 一番簡単なセットアップ

Windowsで、このフォルダ内の次を実行します。

```powershell
powershell -ExecutionPolicy Bypass -File .\SETUP_GITHUB_FREE.ps1
```

スクリプトが順番に処理します。

1. `git` / GitHub CLI (`gh`) の確認
2. GitHubに未ログインならブラウザ認証
3. Public / Private repositoryの選択（既定はPublic）
4. Gemini APIキーを画面非表示で入力
5. GitHub repository作成・push
6. `GEMINI_API_KEY` をGitHub Secretとして登録
7. GitHub Actions workflowを有効化
8. 最初の1回を手動起動

`gh` が入っていない場合は先に：

```powershell
winget install --id GitHub.cli -e
```

その後PowerShellを開き直して `SETUP_GITHUB_FREE.ps1` を再実行してください。

詳細は `GITHUB_ACTIONS_SETUP_JA.md` を参照してください。

## GitHub Actionsでの動作

- cron：毎時 `03,13,23,33,43,53` 分
- 各runは `freebot.py once` を1周期だけ実行
- `freebot-data/` をcommitして次回へ状態継承
- `TRADING_MODE=PAPER`
- 初期資産 `50.00 USD`
- 48時間終了後 `COMPLETED` になったらworkflow自身を無効化

GitHubのscheduled workflowは混雑等で遅れることがあります。**厳密な10分リアルタイム実行ではありません。**

## 主な結果ファイル

`freebot-data/` に以下が残ります。

- `free_experiment_manifest.json`
- `free_status.json`
- `free_last_cycle.json`
- `free_scan_summary.csv`
- `free_fair_values.csv`
- `free_rejections.csv`
- `free_ai_failures.csv`
- `free_heartbeat.csv`
- `trades.csv`
- `state.json`
- `gemini_free_quota.json`

## 重要

48時間で増えても将来利益の保証にはなりません。元投稿の `$50 -> $2,980` を保証・再現するものではありません。

## ローカルテスト

```bash
python -m unittest discover -s tests -v
```

旧Phase 1〜3のテストを維持し、GitHub Actions無料運用用テストも追加しています。

## 旧VPS方式

Docker/VPS関連ファイルは既存資産として残していますが、**今回の推奨経路では使用しません**。Oracle Cloud登録も不要です。
