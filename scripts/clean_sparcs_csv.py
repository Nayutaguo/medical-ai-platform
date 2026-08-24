#!/usr/bin/env python
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from medical_ai.data import (
    CANONICAL_COLUMNS,
    RAW_TO_CANONICAL,
    DataQualityProfile,
    clean_sparcs_row,
    format_cleaned_row_for_csv,
)

DEFAULT_RAW_CSV = (
    Path("C:\\Users\\唐嘉佑\\Desktop\\生产实习\\Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv\\Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv")
)
DEFAULT_OUTPUT = ROOT / "data" / "processed" / "inpatient_sparcs_2021_clean.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean SPARCS inpatient discharge CSV into canonical project columns.")
    parser.add_argument("--input", type=Path, default=DEFAULT_RAW_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--profile-output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Optional row limit for quick development runs.")
    parser.add_argument("--progress-every", type=int, default=100000)
    parser.add_argument("--on-error", choices=["fail", "skip"], default="fail")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    profile_output = args.profile_output or args.output.with_suffix(".profile.json")
    profile_output.parent.mkdir(parents=True, exist_ok=True)
    profile = DataQualityProfile()
    count = 0

    with args.input.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = sorted(set(RAW_TO_CANONICAL) - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"Input CSV is missing expected columns: {', '.join(missing)}")

        with args.output.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=CANONICAL_COLUMNS, lineterminator="\n")
            writer.writeheader()

            for row_number, row in enumerate(reader, start=2):
                try:
                    cleaned = clean_sparcs_row(row)
                except Exception as exc:
                    profile.record_invalid_row(row_number, exc)
                    if args.on_error == "fail":
                        raise
                    continue
                profile.observe(cleaned)
                writer.writerow(format_cleaned_row_for_csv(cleaned))
                count += 1
                if args.limit is not None and count >= args.limit:
                    break
                if args.progress_every and count % args.progress_every == 0:
                    print(f"cleaned {count} rows", file=sys.stderr)

    profile_output.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Cleaned {count} rows into {args.output}")
    print(f"Wrote profile to {profile_output}")


if __name__ == "__main__":
    main()
