#!/usr/bin/env python3
"""Review duplicate Meishi leads. Dry-run unless --apply is passed.

The hosted Meishi connector does not store its runs in this SQLite file.
Pass --csv with an export (company, source_url, and optionally run_id and
status) to review those rows. CSV review never writes.

--apply merges rows in the local SQLite lead tables: one lead per canonical
URL, one company per normalized organization name. Distinct pages are kept.
Runs are moved, not deleted. Do not pass --apply until the dry-run report
has been reviewed.

Examples:
  python scripts/dedup_leads.py --csv runs.csv
  python scripts/dedup_leads.py --db data/meishi.sqlite3
  python scripts/dedup_leads.py --db data/meishi.sqlite3 --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lead_store import LeadStore, load_submission_csv, render_plan, report_submissions  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report duplicate Meishi leads. Does not write unless --apply.")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "meishi.sqlite3")
    parser.add_argument("--csv", type=Path, help="Export to review. Never modified.")
    parser.add_argument("--apply", action="store_true", help="Merge the SQLite database. Refused together with --csv.")
    args = parser.parse_args(argv)
    if args.csv and args.apply:
        print("CSVの確認は読み取り専用です。--apply は --db だけに使えます。", file=sys.stderr)
        return 2
    if args.csv:
        if not args.csv.exists():
            print(f"CSVがありません: {args.csv}", file=sys.stderr)
            return 1
        print(report_submissions(load_submission_csv(args.csv)))
        print("\nDry run. データは変更していません。")
        return 0
    if not args.db.exists():
        print(f"データベースがありません: {args.db}")
        print("Dry run. データは変更していません。")
        return 0
    if args.apply:
        store = LeadStore(args.db)
        plan = store.apply()
        print(render_plan(plan, applied=True))
        if plan.note and plan.empty:
            print("\n適用しませんでした。")
            return 0
        if plan.empty:
            print("\nまとめる重複はありません。スキーマと正規化キーを確認しました。")
            return 0
        print("\n適用しました。run は残し、重複した lead と company だけをまとめています。")
        return 0
    print(render_plan(LeadStore(args.db).plan(read_only=True)))
    print("\nDry run. データは変更していません。内容を確認してから --apply を使ってください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
