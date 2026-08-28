from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pandas as pd

SOURCE = (
    "https://raw.githubusercontent.com/mpr1255/dead_donor_replication/"
    "master/data/full_reference_data_nocode.csv"
)
OUT_DIR = Path("organ_audit/source_metadata")
LOCAL = OUT_DIR / "full_reference_data_nocode.csv"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(SOURCE, LOCAL)

    try:
        df = pd.read_csv(LOCAL, dtype=str, low_memory=False).fillna("")
        encoding = "utf-8"
    except UnicodeDecodeError:
        df = pd.read_csv(
            LOCAL, dtype=str, low_memory=False, encoding="utf-8-sig"
        ).fillna("")
        encoding = "utf-8-sig"

    schema = {
        "source": SOURCE,
        "bytes": LOCAL.stat().st_size,
        "encoding": encoding,
        "rows": int(len(df)),
        "columns": list(df.columns),
        "nonempty_counts": {
            column: int(df[column].astype(str).str.strip().ne("").sum())
            for column in df.columns
        },
        "example_rows": df.head(3).to_dict(orient="records"),
    }
    (OUT_DIR / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
