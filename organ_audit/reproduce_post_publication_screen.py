from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

REPO_RAW = "https://raw.githubusercontent.com/mpr1255/dead_donor_replication/master"
METADATA_URL = f"{REPO_RAW}/data/full_reference_data_nocode.csv"
OUT = Path("organ_audit/post_pub_screen")

PATTERNS = {
    "voluntary_or_donation": r"自愿|捐献",
    "brain_death": r"脑死",
    "donor": r"供体|供者|供心|供肝|供肾|供肺",
    "prisoner_or_execution": r"死刑|死刑犯|犯人|囚犯|在押|羁押|服刑|司法|刑场|枪决|执行死刑",
    "death_determination": r"死亡判定|判定死亡|宣布死亡|宣告死亡|死亡标准|脑死亡判定",
    "circulatory_death": r"心脏死亡|心死亡|循环死亡|心跳停止|心搏停止|心脏停搏|心停跳",
    "airway_or_ventilation": r"气管插管|插管|人工呼吸|机械通气|人工通气|麻醉机|呼吸器",
    "consent_or_family": r"知情同意|同意书|同意捐献|家属同意|亲属同意|家属签|亲属签|书面同意|授权",
    "allocation_or_system": r"COTRS|器官分配|分配与共享|计算机系统|红十字|协调员",
    "retrieval": r"摘取|切取|获取|取供心|取供肝|取供肾|取供肺",
}


def download_text(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def excerpt(text: str, pattern: str, radius: int = 130, limit: int = 4) -> str:
    pieces: list[str] = []
    for match in re.finditer(pattern, text):
        start = max(0, match.start() - radius)
        end = min(len(text), match.end() + radius)
        piece = re.sub(r"\s+", " ", text[start:end]).strip()
        if piece and piece not in pieces:
            pieces.append(piece)
        if len(pieces) >= limit:
            break
    return " || ".join(pieces)


def year_mentions(text: str) -> str:
    values: list[str] = []
    for value in re.findall(r"(?:19|20)\d{2}年", text):
        if value not in values:
            values.append(value)
    return " | ".join(values[:20])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    metadata_path = OUT / "full_reference_data_nocode.csv"
    urllib.request.urlretrieve(METADATA_URL, metadata_path)
    metadata = pd.read_csv(metadata_path, dtype=str, low_memory=False).fillna("")
    metadata["year_num"] = pd.to_numeric(metadata["year"], errors="coerce")

    candidates = metadata[
        metadata["year_num"].gt(2015)
        & metadata["abstract_ch"].str.contains("手术", regex=False, na=False)
        & metadata["abstract_ch"].str.contains("心脏", regex=False, na=False)
        & metadata["abstract_ch"].str.contains("移植", regex=False, na=False)
    ].drop_duplicates(subset=["file_name"])

    records: list[dict[str, object]] = []
    for _, row in candidates.sort_values("file_name").iterrows():
        file_name = str(row["file_name"])
        url = f"{REPO_RAW}/data/txt/{file_name}"
        text = download_text(url)
        record: dict[str, object] = {
            "file_name": file_name,
            "title_ch": row["title_ch"],
            "author_ch": row["author_ch"],
            "journal": row["journal"],
            "year": row["year"],
            "full_text_available": text is not None,
            "full_text_url": url,
        }
        if text is None:
            for name in PATTERNS:
                record[name] = False
            record["date_mentions"] = ""
            record["relevant_excerpt"] = ""
        else:
            for name, pattern in PATTERNS.items():
                record[name] = bool(re.search(pattern, text))
            record["date_mentions"] = year_mentions(text)
            combined = "|".join(
                [
                    PATTERNS["prisoner_or_execution"],
                    PATTERNS["death_determination"],
                    PATTERNS["brain_death"],
                    PATTERNS["circulatory_death"],
                    PATTERNS["airway_or_ventilation"],
                    PATTERNS["consent_or_family"],
                    PATTERNS["allocation_or_system"],
                ]
            )
            record["relevant_excerpt"] = excerpt(text, combined, radius=140, limit=6)
        records.append(record)
        time.sleep(0.02)

    result = pd.DataFrame(records)
    result.to_csv(OUT / "all_candidate_papers.csv", index=False, encoding="utf-8-sig")

    available = result[result["full_text_available"]].copy()
    positive_48_style = available[
        available["voluntary_or_donation"] & available["brain_death"]
    ]
    no_vol_donation = available[~available["voluntary_or_donation"]]
    no_vol_no_donor = no_vol_donation[~no_vol_donation["donor"]]
    target_28_style = no_vol_donation[no_vol_donation["donor"]].copy()
    target_28_style.to_csv(
        OUT / "target_papers_without_voluntary_or_donation_terms.csv",
        index=False,
        encoding="utf-8-sig",
    )

    counts = {
        "metadata_rows": int(len(metadata)),
        "candidate_files_from_public_metadata": int(len(candidates)),
        "full_text_available": int(len(available)),
        "full_text_missing": int((~result["full_text_available"]).sum()),
        "voluntary_or_donation_and_brain_death": int(len(positive_48_style)),
        "without_voluntary_or_donation": int(len(no_vol_donation)),
        "without_voluntary_or_donation_and_without_donor": int(len(no_vol_no_donor)),
        "target_without_voluntary_or_donation_but_with_donor": int(len(target_28_style)),
        "target_keyword_counts": {
            name: int(target_28_style[name].sum()) for name in PATTERNS
        },
        "target_files_with_prisoner_or_execution_terms": target_28_style.loc[
            target_28_style["prisoner_or_execution"], "file_name"
        ].tolist(),
    }
    (OUT / "reproduction_summary.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
