from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path

import pandas as pd

SOURCE_URL = (
    "https://raw.githubusercontent.com/mpr1255/dead_donor_replication/"
    "master/data/post_2015_examination.xlsx"
)
OUT_DIR = Path("organ_audit/output")
WORKBOOK = OUT_DIR / "post_2015_examination.xlsx"


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("_") or "sheet"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(SOURCE_URL, WORKBOOK)

    raw = WORKBOOK.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    excel = pd.ExcelFile(WORKBOOK)
    summary: dict[str, object] = {
        "source_url": SOURCE_URL,
        "sha256": digest,
        "workbook_bytes": len(raw),
        "sheets": [],
    }

    for index, sheet in enumerate(excel.sheet_names, start=1):
        frame = pd.read_excel(WORKBOOK, sheet_name=sheet, dtype=str).fillna("")
        output_name = f"{index:02d}_{safe_name(sheet)}.csv"
        frame.to_csv(OUT_DIR / output_name, index=False, encoding="utf-8-sig")
        summary["sheets"].append(
            {
                "name": sheet,
                "rows": int(len(frame)),
                "columns": list(frame.columns),
                "csv": output_name,
            }
        )

    (OUT_DIR / "workbook_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
