#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--data', type=Path, required=True); a=p.parse_args()
path=a.data/'free_status.json'
if not path.exists(): sys.exit(1)
try: data=json.loads(path.read_text(encoding='utf-8'))
except Exception: sys.exit(1)
sys.exit(0 if data.get('status')=='COMPLETED' else 1)
