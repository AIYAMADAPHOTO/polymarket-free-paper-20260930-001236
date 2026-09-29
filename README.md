
## GitHub Actions 48時間・完全無料運用

Oracle/VPSを使わず48時間のPaper耐久試験を行う場合は `GITHUB_ACTIONS_SETUP_JA.md` を参照してください。
Windowsでは `SETUP_GITHUB_FREE.ps1` がrepository作成、Gemini Secret登録、初回workflow起動まで自動化します。

# Polymarket Paper Trading Bot — Phase 1 / Phase 2

## Phase 2：自動Paper Strategy Engine

`--strategy` を指定すると3種類のルールベース戦略を実行します。指定しなければ従来のPhase 1観測・手動仮想売買・TEST_MODEを使用できます。`config.py` の `TRADING_MODE = 'PAPER'` と環境変数の両方を検査し、`REAL` 等はAPI接続や状態保存の前に起動拒否します。

既存の `data/` を引き継ぎます。Phase 1の初期資金は50.00 USD、Phase 1テスト終了時は49.99567 USDでした。この損失−0.00433 USDを残し、Phase 2の成績は**49.99567 USDを基準に別集計**します。口座を50 USDに戻して損失を隠す処理はありません。Phase 1原本とテストファイルの控えは `verification/phase1_baseline/` にあります。

### 起動・停止・復元

```powershell
Set-Location 'C:\Users\chsgw\Documents\Codex\2026-09-25\oi\outputs\polymarket-paper-bot'
$env:TRADING_MODE = 'PAPER'
$env:TEST_MODE = 'false'
.\.venv\Scripts\python.exe main.py --strategy
```

停止はBot側で `Ctrl+C`。同じコマンドで再起動すると残高、保有、取引理由、シグナル履歴、消費済みシグナル、日次損失基準、benchmarkを復元します。初回のみ履歴のウォームアップが必要です。売買判断の確認は別のPowerShellで行います。

```powershell
Get-Content .\logs\bot.log -Tail 20 -Wait
```

起動中の状態ファイルは直接参照できますが、`--status` は口座ロックを取るため、Botを停止してから実行してください。

```powershell
.\.venv\Scripts\python.exe main.py --status
Get-Content .\data\performance.json
Get-Content .\verification\phase2_report.json
Import-Csv .\data\strategy_decisions.csv | Select-Object -Last 10 timestamp,market_id,outcome,action,reason,combined_score
Import-Csv .\data\trades.csv | Select-Object -Last 10 side,strategy,quantity,entry_reason,exit_reason,net_pnl
```

31分だけ実行して停止する場合（Phase 3ではありません）：

```powershell
.\.venv\Scripts\python.exe main.py --strategy --run-seconds 1860
```

### 構造

`src/strategy/` は次の責務に分割しています。

| ファイル | 責務 |
| --- | --- |
| `strategy_engine.py` | 観測履歴、判断の順序、重複防止、判断ログ、原子的なチェックポイント |
| `market_filter.py` | 状態・時刻・価格・spread・volume・liquidity・近傍の板厚を検査 |
| `signals.py` | 独立3戦略、時点制約、スコアと組合せ |
| `risk_manager.py` | 口座全体・日次・連敗・保有重複・cooldown |
| `position_sizer.py` | 費用込みの固定fractional riskによる数量計算 |
| `exit_manager.py` | 利確・損切り・時間・逆シグナル・満期接近・板異常 |

`src/execution.py` は板の複数価格段を消費するモデル、`performance.py` は成績計算、`benchmark.py` は独立したbuy-and-hold比較、`phase2_runtime.py` は周期実行です。

1サイクルでは市場データ取得→Filter→Signal→Risk→Sizing→PaperBrokerの順にエントリーを判定します。保有のExit判定は新規エントリーより先に実行し、エントリー条件を外れた保有市場も価格を再取得します。

### 3戦略とスコアの意味

すべてロングのYESまたはNOを候補にします。YESが下落する局面は、実際のNOのbidと板を使ってNO側の上昇を判断します。NOを `1−YES` で捏造しません。

| 戦略 | 初期ルール |
| --- | --- |
| 短期モメンタム | 最大15分の観測、6点以上・180秒以上。bidが0.015以上上昇、正の変化が3回以上かつ半数以上、累積volume増加率0.0001以上、liquidityが開始時の90%以上 |
| Mean Reversion | 過去の基準平均から0.025以上下落後、直近2区間で連続反発し、底から0.003以上回復。基準平均未満で、liquidityが消失していないこと。下落継続中はエントリーしない |
| Order Book Imbalance | 最良価格から0.03以内・各側最大5段のshare数量を使用。`(bid数量−ask数量)/(bid数量＋ask数量)` が0.65以上で180秒持続し、両側2段以上、異なる板更新時刻3個以上を要求 |

スコア0は未成立。成立の最小条件で0.70、条件を超える強さに応じて1.00まで上げます。**勝率・期待収益率・校正された確率ではありません。** 3戦略の重みは初期値1ずつ。有効な戦略の加重平均に、追加の一致戦略1つにつき0.05を加え、1.00で上限を設けます。単独戦略でも基準を満たせば候補となります。

直近履歴は `market_snapshots.csv` と同じ観測から作成し、対応する取得時刻付きでstateにも保存します。初回は古いPhase 1 CSVを後付けで取引に利用せず、新たに観測します。シグナル計算は判断時点以前の観測だけを取り出します。30秒未満の重複観測は追加せず、120秒を超える観測間隔は連続性不足として新規シグナルを止めます。単一tick変化だけでモメンタムは成立しません。

`signals_generated` はスコアが閾値を下から上へ超えた**シグナルの発生区間数**です。フィルタやRiskで取引を拒否したシグナルも含みます。単なる評価回数は `signal_evaluations`。売買したシグナルが成立し続けている間は再エントリーせず、一度不成立になってから再判定します。取引0件も正常な結果です。

### Filter / Risk / Exit初期設定

設定は `config.py` の `StrategyConfig` にまとめています。Decimal値は `Decimal('0.05')` のように文字列で変更し、Botを停止してから再起動してください。設定変更はstateに記録し、過去損失や履歴は維持します。

| 設定 | 初期値 |
| --- | --- |
| 価格範囲 | 0.05～0.95 |
| spread上限 | 0.02、かつaskの10%以内 |
| volume / liquidity下限 | 10,000 / 1,000 USD |
| 近傍板厚 | bid・askそれぞれ5 USD以上 |
| 新規エントリーに必要な残存時間 | 3,600秒 |
| データ最大経過時間 | 120秒 |
| 個別signal / combinedの閾値 | 0.70 / 0.70 |
| 1取引の最大リスク | 現在の清算評価資産の5% |
| 1市場最大投入 / 全体投入 | 5 USD / 現在資産の30% |
| 最大同時保有 | 3市場 |
| 日次損失上限 | 5 USD、UTC日付基準 |
| 連敗保護 | 完全決済した3連敗で3,600秒停止 |
| 同一市場cooldown | 900秒、損失後1,800秒 |
| 損失後の全体cooldown | 1,800秒 |
| Take Profit / Stop Loss | 費用込み原価比+5% / −3% |
| Time Stop | 3,600秒 |
| 満期接近Exit | 残り1,800秒 |
| 異常spread / liquidity消失 | spread>0.04 / エントリー時の50%未満 |
| エントリーslippage制限 | 最良askから各価格段+2%以内 |

サイズ計算は、stop価格で確実に売れると仮定せず、**購入代金＋feeの全額を最大損失として5%以内**に抑えます。50 USDなら原則2.50 USD以内です。市場上限5 USDより5%制限が先に効きます。総投入は残存取得原価で計算し、サイズは小数6桁で切り下げます。同一市場のYES/NO同時保有と買い増しを拒否します。

日次損失は当日の実現損失と、保存した当日開始資産から現在資産までの減少の両方を検査します。日次の判定基準も再起動で維持します。清算評価できない保有があれば、新規エントリーを止めます。

利確・損切りは、取得原価に対する**その時点で板を売却した後のfee控除後損益**で判定します。指定の+5%/−3%は「エントリー価格が5%動いた」という意味ではありません。spreadとfeeが大きい場合は小さな値動きでも損切り条件になるため、利益を保証する設定ではありません。0～1の価格範囲を守り、価格0や1で架空約定しません。価格飛びや流動性不足では損切り閾値での約定を保証できません。

### 複数段約定とfee

Gamma / CLOBの仕様を2026-09-25に再確認しました。[公式Fee説明](https://docs.polymarket.com/trading/fees)と[市場別fee設定](https://docs.polymarket.com/market-data/market-details)に基づき、取得した料率を使います。exponent=1以外、欠損、取得不能はunknownとして仮想約定を拒否します。

BUYは安いaskから、SELLは高いbidから、APIで実在した各価格段の数量だけを使います。CSVの `simulated_fill_price` はその加重平均（VWAP）で、各実在価格と数量は `fill_legs`、正確な売買代金は `fill_notional` に保存します。平均価格の丸め値を再乗算して現金を計算しません。feeは各価格段の約定数量に公式曲線を適用して合計します。実取引所の個別注文単位の丸めを完全再現するものではありません。

エントリーは全量を満たせなければ拒否。Exitは取得可能な数量までpartial fillを許可し、残りは保有として残します。部分約定は `fill_status=partial`、元の依頼数量は `requested_quantity`。片側の板が空、古い板、unknown feeなら残高を変えずに拒否理由を保存します。

`slippage_cost` は同数量を最良価格で約定できたと仮定した代金との差で、実際の約定代金に既に含まれています。PnLから二重に控除しません。spreadもbid/askに内包します。板の待ち行列、隠れた注文、ネットワーク遅延中の板変化、見せ板は再現しません。

### 記録・Performance・Benchmark

`trades.csv` は従来列を保持して追加列を末尾に増やします。過去の取引に存在しなかった情報は空欄とし、旧取引の理由・価格を補完しません。`market_snapshots.csv` はYES/NOの複数段・板hashも保存し、旧ヘッダーの拡張時に `.phase1.bak` を作ります。stateの既存取引オブジェクトは書き換えません。

`strategy_decisions.csv` にFilter除外、3戦略の個別スコアと特徴量、合成スコア、Risk結果、エントリー・Exit理由、拒否理由を保存します。エントリー・Exitのチェックポイントを仮想取引と同じatomic state保存で確定するため、保存直後の再起動でもBUYを重複しません。

`data/performance.json` はPhase 2開始以降の結果です。`total_trades` はBUY/SELL約定数、勝敗は部分SELLを合算した完全決済ポジション単位です。平均損失は負数、profit factorの分母が0ならnullと理由を返します。勝率は0～1、戦略別勝率は%です。

- `current_equity` / `net_pnl`：全保有を現在の板で清算してExit feeを引いた評価額／Phase 2開始評価額との差。
- `gross_pnl` / `realized_net_pnl`：決済した数量の手数料前／取得・売却手数料込み損益。保有中の未実現分は含みません。
- `fees`：Phase 2で実際にシミュレートしたBUY/SELLに計上済みのfee。未決済の推定Exit feeは現在資産評価に含めますが、支払い済みfeeに加えません。
- `maximum_drawdown_*`：周期的に取得した清算評価曲線のピークからの低下。観測間の値動きや評価不能区間の最大値を保証する数値ではありません。
- `lifetime_*`：Phase 1を含む口座全体の基準・実現損益。Phase 2と混同しません。

benchmarkは同じPhase 2開始時の資産から、最初にeligibleとなった市場のうちmarket ID・outcome順で最初の1つを最大1 USDだけBUYし、保有を続ける独立した紙上口座です。BUYにも複数段・feeを適用し、清算評価にはbid側の板と売却feeを使います。戦略本体の現金・ポジションには混ぜません。市場選択を後から変更せず、1回のエントリーが拒否された場合も都合のよい市場に選び直しません。最初のeligible市場が出るまでの待機時間、買付に使用した板の取得時刻、選択結果を保存します。

benchmarkと本体は保有額・エントリー時刻が異なるため、この小標本だけで優位性を断定できません。利益が出ても負けても記録を残します。

### Phase 2検証

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\audit_read_only.py
.\.venv\Scripts\python.exe scripts\verify_synthetic.py
```

実API受入試験の証跡は `verification/phase2_report.json`、`phase2_live_segment_1.log`、`phase2_live_segment_2.log`、`phase2_restart_1.json`、`phase2_restart_2.json` に保存します。途中と終了後に別プロセスの `--status` でstate全体の完全一致を検査します。実API結果と、テストファイルのsynthetic fixturesは分離しています。

`verification/phase2_synthetic_scenarios.json` は架空の市場入力を明示した3シナリオです。各戦略のBUYから実際のPaperBrokerによるSELL、理由ログ、PnL、再読込まで確認します。このファイルの利益・損失は実API受入試験の成績に加算しません。

新しい独立口座で30～60分の受入試験を再実施する場合は、未使用のフォルダを指定します。既存の検証レポートは先にコピーしてください。実取引履歴を消すコマンドは不要です。

```powershell
.\.venv\Scripts\python.exe scripts\verify_phase2.py --minutes 31 --data-dir .\data-phase2-new-test
```

既存口座で継続する場合は通常の `main.py --strategy` を使います。受入試験スクリプトは完了済みの戦略口座を初期化せず、再試験用の新規口座を要求します。Phase 3の48時間運転は今回実行していません。

---

## Phase 1操作（互換機能）

Windows / Python 3.11以上。公開APIのGETで実際の市場と板を読み、**仮想50.00 USD**で紙上売買を記録します。実注文、ウォレット、秘密情報、送金機能はありません。通常起動は市場の観測のみです。

## Windows PowerShellで起動

以下はこのPCで使用したフォルダです。別の場所へ移した場合は最初のパスを変更してください。venvの有効化は不要です。

```powershell
Set-Location 'C:\Users\chsgw\Documents\Codex\2026-09-25\oi\outputs\polymarket-paper-bot'
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index -r requirements.txt
.\.venv\Scripts\python.exe main.py --once
```

実行時依存はPython標準ライブラリのみなので、requirementsのインストールは追加パッケージなしで完了します。APIアクセスにはインターネットが必要です。

45秒の待機間隔で継続観測する場合：

```powershell
.\.venv\Scripts\python.exe main.py
```

1サイクルの通信が終わった後に45秒待ちます。障害時はAPI単位の再試行に加え、次のサイクルまでの待ち時間も延ばします。最大20市場、最大5ページ×50件を探索する設定です。**全世界の市場総数ではなく、探索範囲内のYES/NO市場一覧**です。`config.py`でページ上限、`--max-markets 40`で採用上限を変更できます。

## TEST_MODE

配布フォルダの `data/` は実検証の仮想売買結果を保持しています。結果の確認：

```powershell
.\.venv\Scripts\python.exe main.py --status
Get-Content .\data\trades.csv
Get-Content .\verification\live_report.json
```

新しい50 USD口座で自分でもテストする場合は、**新しいデータフォルダ名**を指定します。

```powershell
$env:TEST_MODE = 'true'
.\.venv\Scripts\python.exe main.py --once --data-dir .\data-my-test
$env:TEST_MODE = 'false'
```

同じ意味のCLI指定は `--test-mode` です。TEST_MODEが有効なときだけ自動の1 share BUY→SELLが動作します。価格は0〜1なのでBUY元本は1 USD未満です。取引所の最低注文金額は適用しない、小額の紙上動作テストです。約定条件の完全な取引所再現ではありません。

途中にプロセス再起動を挟む例（別の未使用フォルダを使用）：

```powershell
.\.venv\Scripts\python.exe main.py --once --test-mode --test-leg buy --data-dir .\data-restart-test
.\.venv\Scripts\python.exe main.py --status --data-dir .\data-restart-test
.\.venv\Scripts\python.exe main.py --once --test-mode --test-leg sell --data-dir .\data-restart-test
.\.venv\Scripts\python.exe main.py --status --data-dir .\data-restart-test
```

BUY後の停止では次回TEST_MODEがSELLを再開します。完了済みの口座では再実行しても取引を増やしません。継続起動時は完了後も市場観測を続けます。SELLに必要な板が取得できない場合は保有を維持し、次回再試行します。TEST_MODEを無効にした通常再起動では自動SELLしません。

## ログ・停止・再起動・保存状態

別のPowerShellでログを確認：

```powershell
Get-Content .\logs\bot.log -Tail 40 -Wait
```

BotのPowerShellで `Ctrl+C` を押して停止します。ログ追尾側も `Ctrl+C` で終了します。再起動と保存確認：

```powershell
.\.venv\Scripts\python.exe main.py --status
Get-Content .\data\state.json
Import-Csv .\data\trades.csv | Format-Table side,quantity,simulated_fill_price,fee,cash_after,realized_pnl
Import-Csv .\data\portfolio_history.csv | Select-Object -Last 5
Import-Csv .\data\market_snapshots.csv | Select-Object -First 20 market_id,title,yes_bid,yes_ask,no_bid,no_ask,fee_status
.\.venv\Scripts\python.exe main.py
```

`--data-dir .\data-my-test` のログは `data-my-test-logs/bot.log` に保存します。ログはUTC時刻で最大5 MB×4ファイル。CSVはUTF-8です。Excelで開く場合は「データ→テキストまたはCSVから」でUTF-8と文字列型を指定し、長いIDや小数の自動変換を避けてください。

## 手動の仮想注文

`market_snapshots.csv` にある `market_id` を入力すると、最新の市場状態・板を再取得して実行します。

```powershell
$marketId = Read-Host 'market_snapshots.csvのmarket_id'
.\.venv\Scripts\python.exe main.py --buy $marketId --outcome YES --quantity 1
.\.venv\Scripts\python.exe main.py --sell $marketId --outcome YES --quantity 0.5
.\.venv\Scripts\python.exe main.py --close $marketId --outcome YES
```

手動注文はTEST_MODEをfalseにしてください。`--price 0.45` はBUYの上限／SELLの下限で、条件を満たさなければ約定しません。APIが失敗した注文は通常サイクルと異なり、コマンドをエラー終了します。保存状態を確認してから再実行してください。

## 計算・欠損値の扱い

- BUYは最良ask、SELLは最良bid。板の配列順に依存せず最大bid・最小askを選択します。表示されている最良価格の数量を超える注文は拒否します。板の消費・複数段約定・遅延モデルは未実装です。
- `BestQuoteFill` が将来のslippageモデル差し替え箇所。現在slippageは0、spreadの不利は実際のbid/askで反映します。
- 市場ID、YES/NOラベル、token IDを対応づけます。YES/NO以外のラベルの市場、閉鎖・終了・清算提案済み・異常データは理由をログに出して除外します。
- 金額・価格はDecimal、JSONでは小数文字列。保有原価は購入手数料込みの加重平均法です。一部SELLの原価配賦のみ小数18桁に丸め、最後のSELLで残り原価を全額配賦します。
- realized PnLは売却収入－売却手数料－配賦取得原価。unrealized PnLは保有数量×現在bid－残存取得原価。equityはcash＋bid評価額。未実現評価は将来の売却手数料控除前です。
- 板の取得時刻・APIの板時刻とも120秒以内が仮想約定の条件。無効・古い・不足した板を使って価格を補完しません。保有に新鮮なbidがない場合、equityとunrealized PnLはnullです。
- 手数料はGamma `feesEnabled` と `feeSchedule` を参照します。未取得時に公開CLOB `fd` を確認します。現行の `quantity × rate × price × (1-price)` に対応するexponent=1をサポートし、0.00001 USD未満は0、それ以外は小数5桁・ROUND_HALF_UPで紙上計算します。公式説明に端数同率時の丸め規則はないため、この規則はシミュレーション上の選択です。
- カテゴリ別の料率はハードコードしません。無料が明記されていれば0、設定が不明・未対応なら `fee_status=unknown`、CSVとログに記録して仮想約定を拒否します。unknownを無料扱いしません。
- 取得不能のCSV値は空欄です。GammaのYES/NO価格とCLOBのbid/askは取得元・時刻が異なり、一致は保証されません。市場スナップショットに両方と板時刻を保存します。

## 永続化

`state.json` が正本で、全取引、残高、保有、PnL、開始時刻、開始残高、TEST_MODEチェックポイントを含みます。同一ディレクトリ内の一時ファイルをfsync後にatomic replaceします。前回のstateは `state.json.bak`。同一口座への多重起動はOSロックで拒否します。

復元時はchecksumと全取引の再計算で残高・保有・PnLを検証します。破損時は終了コード2で停止し、初期化しません。バックアップを自動採用すると直近取引を失う可能性があるため、原本・バックアップを別途保管して比較してください。新規開始には別のデータフォルダを指定してください。

`trades.csv` はstateからatomic再生成するため、state保存直後の停止やCSVだけの破損は再起動で復旧できます。CSVの出力失敗後も取引がstateに記録済みの場合があります。必ず再起動して `--status` を確認してください。CSVやstateをExcel等で開いてロックすると保存エラーになる場合があります。snapshotとportfolio_historyは追記ファイルで、強制電源断時の末尾欠損を自動復旧する対象ではありません。

## テスト

このPCで実施した自動テストと実API検証は `verification/` に保存しています。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\verify_unit.py
.\.venv\Scripts\python.exe scripts\verify_live.py .\data-new-acceptance
```

後者は未使用のフォルダを指定してください。依存確認→API/市場/snapshot→BUY→保有中の別プロセス復元→SELL→CSV/state照合→別プロセス復元→重複取引防止を検証します。仮想売買でも最新価格によってPnLは変わります。

## 公式API・確認日

仕様確認・実測日：2026-09-25。公開RESTのGETのみを許可したクライアントです。

| 用途 | API | 公式資料 |
| --- | --- | --- |
| 市場発見・ページ送り | `https://gamma-api.polymarket.com/markets/keyset` | [Keyset markets](https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination) |
| 売買前の市場再確認 | `https://gamma-api.polymarket.com/markets/{id}` | [Market details](https://docs.polymarket.com/market-data/market-details) |
| YES/NOそれぞれの板 | `https://clob.polymarket.com/book?token_id=...` | [Order book](https://docs.polymarket.com/api-reference/market-data/get-order-book) |
| 手数料情報の補助取得 | `https://clob.polymarket.com/clob-markets/{condition_id}` | [CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info) |
| 手数料計算根拠 | Gamma `feeSchedule` / CLOB `fd` | [Fees](https://docs.polymarket.com/trading/fees) |

実測差分：旧 `/markets` はHTTP 200でも廃止警告と `/markets/keyset` の案内があり、採用していません。板の順序が公式例と逆だったため並び順に依存しません。初期のPython標準User-Agentで403が出ましたが、アプリ識別User-Agentを設定した公式公開APIへの通常GETで取得できました。地域制限回避は行っていません。

## ファイル構成・Phase 1の限界

`main.py` 起動・観測・テスト、`config.py` 設定、`src/polymarket_client.py` 公開GET、`market_scanner.py` 正規化・除外、`paper_broker.py` 仮想約定、`portfolio.py` 会計・評価、`storage.py` 保存、`models.py` 型、`logger.py` ログ。

この節はPhase 1の操作説明です。Phase 2では上記の3戦略と板消費モデルを追加しました。市場清算・償還、AI選定、バックテスト、実取引は引き続き未実装です。終了した保有市場は自動清算せず、評価不能として残します。非YES/NO市場とexponent≠1の手数料は対象外です。

## Phase 3：独立50 USD・48時間固定実験

Phase 1/2の `data/`、`logs/`、`verification/` は読み取り専用として扱います。新規実験は `experiments/phase3_YYYYMMDD_HHMMSS/` に保存され、仮想50.00 USD・保有0から開始します。既存戦略・リスク・手数料・板約定コードは変更しません。開始前に全テスト、合成板テスト、別プロセス復元、実Gamma/CLOB接続、APIのHTTPS DateとPC時計の差（30秒以内）、既存成果物のSHA-256一致を確認し、一つでも失敗したら開始しません。

PowerShellでプロジェクトに移動して実行します。`start` は毎回新しい実験を作るため、再開には使わないでください。

```powershell
cd C:\Users\chsgw\Documents\Codex\2026-09-25\oi\outputs\polymarket-paper-bot
.\.venv\Scripts\python.exe phase3.py start
# 以降、実際に表示されたIDに置換する
$experiment = '.\experiments\phase3_YYYYMMDD_HHMMSS'
.\.venv\Scripts\python.exe phase3.py status $experiment
Get-Content (Join-Path $experiment 'runtime.log') -Tail 30
.\.venv\Scripts\python.exe phase3.py stop $experiment
# status が INTERRUPTED になり監視プロセスが終了してから再開
.\.venv\Scripts\python.exe phase3.py resume $experiment
```

任意で `powershell -ExecutionPolicy Bypass -File .\run_phase3.ps1 -Action status -Experiment $experiment` も使用できます。ExecutionPolicyの恒久変更は不要です。

### Windows長時間稼働

- AC電源・安定したインターネット接続を確保してください。Windowsの「設定 → システム → 電源とバッテリー → 画面、スリープ、休止状態のタイムアウト」で、電源接続時のスリープを「なし」にする場合はユーザー自身で設定し、実験後に戻してください。自動再起動・休止状態・蓋を閉じる動作にも注意してください。
- 実装は稼働中のスレッドだけに `SetThreadExecutionState` で一時的なスリープ抑制を要求し、終了時に解除します。OS設定は恒久変更しません。手動スリープ、再起動、電源断を防げる保証はありません。
- 起動確認が終わるまでターミナルを閉じないでください。通常の `start` は非表示の独立監視プロセスへ引き継ぐため、その後はターミナルを閉じても継続する設計です。内部 `_worker` の直接起動は避けてください。
- `processes.json` のworkerとsupervisorのPID、`status.json`、更新中の `telemetry.json` と `runtime.log` を併せて確認してください。保存済みRUNNING表示だけでは現在の稼働を証明できません。
- worker異常終了時は監視プロセスが同一state・ID・元の期限で復元します。PC再起動後の自動起動はOS設定を変更しないため未実装です。PC復帰後は `resume` が必要です。中断時間は隠さず記録され、再開しても48時間は延長しません。

### 固定・証跡・終了

`runtime/` は開始時コードのコピー、`config_snapshot.json` は全設定とソースhash、`manifest.json` と `time_origin.json` はUTC/ローカルの開始・終了予定時刻です。開始後はこれらを編集しないでください。重要な変更・破損は `INVALID` にして停止し、同じ実験を修正再開しません。原因修正・全テスト後、別IDの新規実験を作ります。

`state.json` が残高と取引の正本です。`evidence.sqlite3` は追記専用の取引・シグナル証跡です。`signals.csv` は3戦略の全評価（閾値未満も含む）、候補、拒否、約定結果を区別して保存します。同じ評価イベントは重複させません。`rejected_signals.csv` は見送った評価を保存します。シグナルCSVは復元時に証跡から再生成可能です。ローカル証跡全体を意図的に書き換える行為に対する第三者署名保証ではありません。

約5分ごとの `heartbeat.csv`、5秒程度ごとの稼働pulse、毎時 `summaries/`、各周期 `valuations.csv`・`live_summary.json` を保存します。15秒超のpulse間隔は通信待ちを含む観測上の稼働空白として保守的に集計し、360秒超のheartbeat間隔も報告します。`signals_generated` はPhase 2と同じシグナル発生区間数、`signal_evaluation_rows` は候補・拒否・約定を含む記録行数で、別の指標です。strategy別集計は複合戦略名単位とし、同一取引を3戦略へ重複配賦しません。

期限到達後は売買せず、未決済ポジションを保持したまま `final_report.json` と `verification/phase3_integrity_report.json` を自動作成します。未実現PnLは新鮮なbid板を全数量消費したVWAP（将来の売却手数料控除前）。別欄のhypothetical liquidationだけに仮想売却手数料を反映し、実際のSELLとして記録しません。板不足・古い価格・終了市場の評価はnullです。公式市場精算は未対応で `unsupported_features` に記載し、確認不能な保有はUNRESOLVEDとして残します。benchmarkはPhase 2と同じ独立仮想口座で実行され、本体資産に混ぜません。

実験ファイルをExcel等でロックしないでください。損失や取引の削除、設定変更、短縮実験を48時間と呼ぶことはできません。短時間の終了処理テストはunit test内の合成時計だけで行い、本番CLIには実験期間を短縮するオプションはありません。48時間継続の成否は終了時の実際の証跡で判断します。

## Linux VPS / Docker版

移行手順は [deploy_to_vps.md](deploy_to_vps.md)。旧ローカル実験は `ABORTED_BY_USER_MIGRATION` として保持し、VPSでは独立した `phase3_vps_...` ID・50.00 USD・保有0で開始します。移行中止記録は元の実験ファイルを上書きしないよう `migration_disposition.json` に分離されています。

推奨最小：Ubuntu 22.04/24.04 LTS、1 vCPU、1 GB RAM、10 GB程度のdisk、Docker Engine＋Compose plugin。GPU・GUI・ブラウザ・Web UIは不要。Python 3.13.5を固定したnon-rootコンテナを使用し、公開ポート、privileged、Docker socket mount、host networkは使用しません。既存101テストは削除・変更していません。Docker/VPS関連テストは `tests/test_vps.py` に追加しています。

市場監視は現在 **1回あたり最大20市場のbounded sample** です。全市場監視ではありません。コードの `config.py: MAX_MARKETS` は将来の別実験で変更可能ですが、今回のVPS移行では20を維持し、実験中は変更禁止です。3戦略・filter・risk・exit・PaperBroker・fee・slippage本体も変更しません。

`.env.example` を `.env` へコピーします。秘密情報は不要です。PAPER、50.00 USD、48時間は固定検証。scan間隔は開始前に設定可能（既定45秒）、timezone/LOG_LEVELと共に凍結snapshotへ記録し、再開時の変更を拒否します。

以下はLinuxプロジェクトフォルダでのコマンドです。Docker権限がない場合は先頭に `sudo` を付けてください。

```bash
docker compose up -d
docker compose ps
docker compose logs -f
# 現金・現在position・experiment ID・終了予定・heartbeat/API更新時刻
docker compose exec -T paperbot python vps.py status
docker compose exec -T paperbot python vps.py health
# ホスト上のheartbeat
sudo tail -n 5 vps-data/experiments/phase3_vps_*/heartbeat.csv
docker compose stop
docker compose start
docker compose down
```

`./vps-data` はbind mountです。down/recreate後も実験データはホスト側に残ります。`active.json` で同じIDを復元し、元のscheduled endを維持します。明示的にstopしていなければ `restart: unless-stopped` とDocker daemonの自動起動でVPS再起動後も復旧する設計です。SSH切断はコンテナを止めません。SIGTERM/SIGINT時は正常保存し、停止理由と時刻を新規ファイルに追記します。health failureだけではrestartしません。完了・重大失敗時は待機し、無限再作成を防ぎます。

Docker stdoutは10m×5本に制限。runtime.logは10 MiBずつ分割し、監査用segmentを削除しません。空き容量が少ない場合は証跡を削除する代わりに停止します。CSVは追記・ストリーミングで、snapshot全件をRAMへ読み込みません。memory limitの既定は768m、tmpfsは96m、queueは不使用です。48時間の取引正本・有限の戦略履歴はRAMにも保持するため、実際のメモリ使用量はVPSで確認してください。

Docker DesktopがないこのPCでは **ローカルDocker build/run検証未実施**。実VPS接続・SSH切断テスト・ホスト再起動テストも未実施です。Python自動テストの成功と混同しないでください。原期限を維持するため、停止時間を取り戻すための48時間延長は行いません。
