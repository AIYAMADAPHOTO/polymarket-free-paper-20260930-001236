#!/usr/bin/env python3
"""Write a compact GitHub Actions summary without exposing secrets."""
from __future__ import annotations
import argparse, json, os
from pathlib import Path

p=argparse.ArgumentParser(); p.add_argument('--data', type=Path, required=True); a=p.parse_args()
status_path=a.data/'free_status.json'
if not status_path.exists():
    text='## Polymarket paper test\n\nNo status file exists yet.\n'
else:
    try:
        s=json.loads(status_path.read_text(encoding='utf-8'))
    except Exception as exc:
        text=f'## Polymarket paper test\n\nStatus file unreadable: `{type(exc).__name__}`\n'
    else:
        c=s.get('counters') or {}
        text='\n'.join([
            '## Polymarket paper test', '',
            f"- Status: **{s.get('status')}**",
            f"- Start: `{s.get('start_time')}`",
            f"- Scheduled end: `{s.get('scheduled_end_time')}`",
            f"- Cash: **{s.get('cash')} USD**",
            f"- Equity (mark-to-bid): **{s.get('equity_mark_to_bid_before_exit_fee')} USD**",
            f"- Net PnL (mark-to-bid): **{s.get('net_pnl_mark_to_bid')} USD**",
            f"- Open positions: **{len(s.get('open_positions') or {})}**",
            f"- Trades: **{s.get('trade_count')}**",
            f"- Paid fallback used: **{s.get('paid_api_fallback_used')}**",
            f"- Real-order code used: **{s.get('real_order_code_used')}**",
            '',
        ])
summary=os.getenv('GITHUB_STEP_SUMMARY')
if summary:
    with open(summary,'a',encoding='utf-8') as f:f.write(text)
print(text)
