from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

SOURCE = Path("organ_audit/output/01_Sheet1.csv")
OUT = Path("organ_audit/analysis")

KEYWORDS = {
    "execution_or_custody": r"死刑|死刑犯|犯人|囚犯|在押|羁押|司法|枪决|刑场|执行死刑",
    "brain_death": r"脑死亡|脑死",
    "death_determination": r"死亡判定|判定死亡|宣布死亡|宣告死亡|死亡标准",
    "cardiorespiratory_arrest": r"心脏停搏|心跳停止|呼吸停止|心肺停止|循环停止",
    "airway_or_ventilation": r"气管插管|插管|机械通气|人工通气|呼吸器|麻醉机",
    "consent_or_family": r"同意|家属|亲属|签字|签署|知情|授权|自愿",
    "donation_system": r"捐献|COTRS|器官分配|红十字",
    "donor_language": r"供体|供者|供肺|供心|供肝|供肾",
}


def uniq_join(series: pd.Series, limit: int = 20) -> str:
    values: list[str] = []
    for value in series.astype(str):
        value = value.strip()
        if value and value not in values:
            values.append(value)
    if len(values) > limit:
        return " | ".join(values[:limit]) + f" | … (+{len(values) - limit})"
    return " | ".join(values)


def first_contexts(series: pd.Series, limit: int = 5) -> str:
    values: list[str] = []
    for value in series.astype(str):
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return " || ".join(values)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SOURCE, dtype=str).fillna("")
    df["string_distance_num"] = pd.to_numeric(df["string_distance"], errors="coerce")
    df["combined_text"] = (
        df["target_string"].astype(str)
        + " "
        + df["matching_string"].astype(str)
        + " "
        + df["context"].astype(str)
        + " "
        + df["notes"].astype(str)
    )

    for name, pattern in KEYWORDS.items():
        df[name] = df["combined_text"].str.contains(pattern, regex=True, na=False)

    group_columns = ["file_name", "title_ch", "journal_year"]
    rows: list[dict[str, object]] = []
    for keys, group in df.groupby(group_columns, dropna=False, sort=True):
        file_name, title, year = keys
        row: dict[str, object] = {
            "file_name": file_name,
            "title_ch": title,
            "journal_year": year,
            "candidate_passages": int(len(group)),
            "minimum_string_distance": (
                float(group["string_distance_num"].min())
                if group["string_distance_num"].notna().any()
                else None
            ),
            "notes": uniq_join(group["notes"], limit=50),
            "target_strings": uniq_join(group["target_string"], limit=50),
            "matching_strings": uniq_join(group["matching_string"], limit=30),
            "context_examples": first_contexts(group["context"], limit=5),
        }
        for name in KEYWORDS:
            row[f"{name}_passages"] = int(group[name].sum())
        rows.append(row)

    papers = pd.DataFrame(rows).sort_values(
        ["journal_year", "file_name"], kind="stable"
    )
    papers.to_csv(OUT / "paper_candidate_summary.csv", index=False, encoding="utf-8-sig")

    note_rows = df[df["notes"].astype(str).str.strip().ne("")].copy()
    note_rows[
        [
            "file_name",
            "title_ch",
            "journal_year",
            "target_string",
            "matching_string",
            "string_distance",
            "context",
            "notes",
        ]
    ].to_csv(OUT / "reviewer_notes.csv", index=False, encoding="utf-8-sig")

    targeted = df[
        df["execution_or_custody"]
        | df["death_determination"]
        | (
            df["brain_death"]
            & df["airway_or_ventilation"]
        )
        | (
            df["cardiorespiratory_arrest"]
            & df["airway_or_ventilation"]
        )
    ].copy()
    targeted[
        [
            "file_name",
            "title_ch",
            "journal_year",
            "target_string",
            "matching_string",
            "string_distance",
            "context",
            "notes",
            *KEYWORDS.keys(),
        ]
    ].to_csv(OUT / "targeted_passages.csv", index=False, encoding="utf-8-sig")

    report = {
        "rows": int(len(df)),
        "unique_files": int(df["file_name"].nunique()),
        "unique_titles": int(df["title_ch"].nunique()),
        "years": {
            str(k): int(v)
            for k, v in df.groupby("journal_year")["file_name"].nunique().sort_index().items()
        },
        "rows_with_reviewer_notes": int(len(note_rows)),
        "files_with_reviewer_notes": int(note_rows["file_name"].nunique()),
        "targeted_passages": int(len(targeted)),
        "targeted_files": int(targeted["file_name"].nunique()),
        "keyword_passage_counts": {
            name: int(df[name].sum()) for name in KEYWORDS
        },
        "note_values": {
            str(k): int(v)
            for k, v in note_rows["notes"].value_counts(dropna=False).items()
        },
    }
    (OUT / "analysis_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
