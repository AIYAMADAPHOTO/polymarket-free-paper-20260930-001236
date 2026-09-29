# Zero-cost architecture / audit notes

## Reused from the existing project

- Gamma/CLOB public-market client
- market normalization
- CLOB order-book parsing
- depth-aware Paper fills
- fee model
- Decimal-based portfolio/accounting
- atomic state persistence and CSV logs
- restart-safe PaperBroker
- legacy Phase 1/2/3 tests

## Current zero-cost path: GitHub Actions

`GitHub Actions schedule -> freebot.py once -> BroadMarketScanner -> GeminiFreeClient -> FreeFairValueKellyStrategy -> PaperBroker -> commit freebot-data`

### Why GitHub Actions

- No VPS is required for the 48-hour software endurance/integration test.
- The workflow runs one short Paper cycle roughly every 10 minutes and exits.
- State is committed back to the repository so the next run resumes the same 48-hour experiment.
- Scheduled workflows can be delayed by GitHub, so the cadence is approximate rather than real-time.
- The workflow disables itself once the experiment status becomes `COMPLETED`.

### Market discovery

- Gamma keyset pagination, max 100 rows/page.
- Up to 10 pages = up to 1,000 markets/cycle.
- Metadata first; CLOB books are fetched only when needed.

### AI fair value

- Default: `gemini-3.5-flash-lite` on the Gemini API free tier.
- Fallback: none. If the primary free model is unavailable, that cycle is skipped instead of switching models.
- Current evidence is gathered before the model call from no-key/no-charge public feeds (Google News RSS; Kraken public ticker for supported crypto). Evidence is cached.
- Candidates are batched so a cycle normally needs at most one Gemini request.
- Local hard cap: 150 model calls/day.
- Quota/rate errors skip the cycle; there is no paid fallback.
- Unallowlisted model identifiers are blocked.
- Billing-disabled confirmation is mandatory before model calls.

### Signal and sizing

- Requires confidence threshold and >= 0.08 executable edge.
- Edge is rechecked after order-book depth and fee.
- Binary Kelly: `(p - price)/(1-price)`.
- Position fraction hard-capped at 0.06 of current equity.
- Portfolio exposure and position-count guards remain.

### Exclusions and safety

- Political/electoral/legislative markets are explicitly excluded from AI probability assessment and trading.
- Real trading, wallet connection, private keys, USDC, approvals and exchange write endpoints are not part of this path.
- `TRADING_MODE=PAPER` is mandatory.

### Zero-cost caveats

- Public GitHub repositories can use standard GitHub-hosted runners without Actions minute charges, but the repository contents and Paper logs are public.
- GitHub Free private repositories include a monthly Actions allowance; prior usage can reduce what remains.
- Gemini free-tier quotas are account/project dependent and can change. If quota is exhausted, this bot skips cycles instead of switching to paid service.
- A Gemini API key cannot by itself prove the Google project has no billing linkage; use a project with billing disabled.
- Provider terms and free-tier limits can change.
