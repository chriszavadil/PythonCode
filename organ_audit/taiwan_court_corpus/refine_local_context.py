from __future__ import annotations

"""Second-pass validation of Taiwan transplant-judgment search results.

The first pass deliberately maximized recall and therefore overmatched long
judgments in which transplant, China, money, and brokerage terms occurred in
unrelated sections. This pass requires the relevant concepts to co-occur in a
small text window surrounding an actual transplant term.

A surviving row is still only a public-record lead. It is not evidence that an
organ came from a prisoner or another nonconsenting donor.
"""

import csv
import hashlib
import json
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE = "https://judgment.judicial.gov.tw"
DETAIL_URL = f"{BASE}/FJUD/data.aspx"
INPUT = Path("organ_audit/taiwan_court_corpus/output/detail_index.csv")
OUT = Path("organ_audit/taiwan_court_corpus/refined")
OUT.mkdir(parents=True, exist_ok=True)
BASELINE = "CHDM,113,金訴,657,20250724,1"
WINDOW_RADIUS = 650
REQUEST_DELAY = 0.35

ORGAN = {
    "器官移植", "肝臟移植", "肝移植", "腎臟移植", "腎移植", "心臟移植",
    "肺臟移植", "胰臟移植", "移植手術", "器官買賣", "器官來源",
}
CROSS_BORDER = {
    "中國", "大陸", "境外", "赴陸", "赴中", "青島", "長沙", "廣州", "天津",
    "武漢", "上海", "北京", "山東", "湖南",
}
HOSPITAL = {
    "青島大學附屬醫院", "青島大學", "中南大學湘雅三醫院", "湘雅三醫院",
    "天津市第一中心醫院", "天津第一中心醫院", "中山大學附屬第一醫院",
    "廣州醫科大學附屬第二醫院", "武漢協和醫院", "武漢同濟醫院",
}
BROKERAGE = {
    "仲介", "居間", "介紹費", "仲介費", "佣金", "報酬", "招攬", "媒介",
    "代辦", "器官來源費",
}
PAYMENT = {
    "匯款", "轉帳", "地下匯兌", "匯兌", "人民幣", "現金", "價金", "收款",
    "支付", "費用", "帳戶", "交易明細",
}
TRAVEL_MEDICAL = {
    "抗排斥藥", "抗排斥藥物", "免疫抑制", "入出境", "出入境", "搭機", "航班",
    "移植時間", "手術日期", "病歷", "就醫紀錄",
}
EVIDENCE = {
    "對話紀錄", "微信", "LINE", "通訊軟體", "證人", "供述", "扣案", "搜索",
    "帳冊", "銀行", "醫療紀錄",
}
CUSTODY = {
    "死刑犯", "受刑人", "囚犯", "監獄", "拘留", "羈押", "法輪功", "維吾爾",
    "供體", "捐贈者", "器官捐贈", "COTRS", "器官分配",
}

GROUPS = {
    "organ": ORGAN,
    "cross_border": CROSS_BORDER,
    "hospital": HOSPITAL,
    "brokerage": BROKERAGE,
    "payment": PAYMENT,
    "travel_medical": TRAVEL_MEDICAL,
    "evidence": EVIDENCE,
    "custody_provenance": CUSTODY,
}

# These phrases describe an actual operation, arrangement, or journey rather
# than a person's unrelated medical history.
ACTION_PATTERNS = [
    re.compile(r"(?:赴|前往|至|到|在).{0,50}(?:中國|大陸|青島|長沙|廣州|天津|武漢|上海|北京|山東|湖南).{0,120}(?:接受|進行|施作|安排|完成|實施|動).{0,40}(?:肝|腎|心|肺|胰|器官).{0,12}移植"),
    re.compile(r"(?:接受|進行|施作|安排|完成|實施).{0,50}(?:肝|腎|心|肺|胰|器官).{0,12}移植(?:手術)?"),
    re.compile(r"(?:肝|腎|心|肺|胰|器官).{0,12}移植(?:手術)?.{0,80}(?:仲介|安排|赴|前往|醫院|人民幣|匯款|費用)"),
    re.compile(r"(?:仲介|安排|居間|媒介).{0,120}(?:肝|腎|心|肺|胰|器官).{0,12}移植"),
]

INCIDENTAL_PATTERNS = [
    re.compile(r"(?:曾|因|患有|病史|術後).{0,25}(?:接受)?(?:肝|腎|心|肺|胰|器官).{0,12}移植"),
    re.compile(r"(?:醫療費用|勞動能力|精神慰撫|損害賠償).{0,100}(?:器官|肝|腎|心|肺).{0,12}移植"),
]


def fetch_detail(session: requests.Session, judgment_id: str) -> tuple[dict[str, str], bytes]:
    last: Exception | None = None
    for attempt in range(4):
        try:
            response = session.get(
                DETAIL_URL,
                params={"ty": "JD", "id": judgment_id, "ot": "in"},
                timeout=60,
            )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            time.sleep(REQUEST_DELAY)
            soup = BeautifulSoup(response.text, "html.parser")
            meta = soup.select_one("div#jud")
            cells = meta.select("div.col-td") if meta else []
            body = soup.select_one("div.htmlcontent")
            return (
                {
                    "title": cells[0].get_text(" ", strip=True) if len(cells) > 0 else "",
                    "date": cells[1].get_text(" ", strip=True) if len(cells) > 1 else "",
                    "case_reason": cells[2].get_text(" ", strip=True) if len(cells) > 2 else "",
                    "content": body.get_text(" ", strip=True) if body else "",
                },
                response.content,
            )
        except requests.RequestException as exc:
            last = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {judgment_id}: {last!r}")


def local_windows(content: str) -> list[str]:
    text = re.sub(r"\s+", " ", content).strip()
    positions: set[int] = set()
    for term in ORGAN:
        positions.update(match.start() for match in re.finditer(re.escape(term), text))
    windows: list[str] = []
    seen: set[str] = set()
    for pos in sorted(positions):
        start = max(0, pos - WINDOW_RADIUS)
        end = min(len(text), pos + WINDOW_RADIUS)
        window = text[start:end]
        signature = hashlib.sha256(window.encode("utf-8")).hexdigest()[:16]
        if signature not in seen:
            seen.add(signature)
            windows.append(window)
    return windows


def terms_by_group(window: str) -> dict[str, list[str]]:
    return {
        group: sorted(term for term in terms if term in window)
        for group, terms in GROUPS.items()
    }


def action_matches(window: str) -> list[str]:
    matches: list[str] = []
    for index, pattern in enumerate(ACTION_PATTERNS, start=1):
        match = pattern.search(window)
        if match:
            matches.append(f"action_{index}:{match.group(0)[:180]}")
    return matches


def incidental_matches(window: str) -> list[str]:
    matches: list[str] = []
    for index, pattern in enumerate(INCIDENTAL_PATTERNS, start=1):
        match = pattern.search(window)
        if match:
            matches.append(f"incidental_{index}:{match.group(0)[:160]}")
    return matches


def local_score(groups: dict[str, list[str]], actions: list[str]) -> int:
    score = 0
    score += 4 if groups["organ"] else 0
    score += 3 if groups["cross_border"] else 0
    score += 4 if groups["hospital"] else 0
    score += 4 if groups["brokerage"] else 0
    score += 2 if groups["payment"] else 0
    score += 2 if groups["travel_medical"] else 0
    score += 1 if groups["evidence"] else 0
    score += 1 if groups["custody_provenance"] else 0
    score += 5 if actions else 0
    if groups["brokerage"] and groups["payment"]:
        score += 2
    if groups["hospital"] and actions:
        score += 3
    if groups["cross_border"] and groups["brokerage"] and actions:
        score += 4
    return score


def classify(groups: dict[str, list[str]], actions: list[str], incidental: list[str], score: int) -> tuple[str, str]:
    anchor = bool(groups["cross_border"] or groups["hospital"])
    transactional = bool(groups["brokerage"] or groups["payment"])
    operational = bool(actions or groups["travel_medical"])

    if groups["organ"] and anchor and transactional and actions and score >= 15:
        return "strong_case_level_lead", "local operation/arrangement + cross-border/hospital + transaction"
    if groups["organ"] and anchor and transactional and operational and score >= 11:
        return "manual_review", "local transplant + cross-border/hospital + transactional or operational anchors"
    if incidental and not actions:
        return "rejected_incidental_medical_history", "transplant appears as medical history or damages context without local arrangement evidence"
    if groups["organ"] and not anchor:
        return "rejected_no_cross_border_anchor", "no China/overseas or named-hospital anchor in the transplant window"
    if groups["organ"] and anchor and not transactional:
        return "rejected_no_transaction_or_brokerage", "cross-border transplant language lacks local brokerage/payment evidence"
    return "rejected_weak_local_context", "relevant terms do not form a case-level event in one local window"


def redact(text: str, limit: int = 1000) -> str:
    excerpt = re.sub(r"\s+", " ", text).strip()[:limit]
    excerpt = re.sub(
        r"(病患|患者|被告|證人|告訴人|告發人|醫師|仲介人|家屬)([：:\s]*)([\u4e00-\u9fff○ＯA-Z]{2,5})",
        r"\1\2[REDACTED]",
        excerpt,
    )
    excerpt = re.sub(r"[A-Z]\d{8,10}", "[ID REDACTED]", excerpt)
    excerpt = re.sub(r"\b09\d{8}\b", "[PHONE REDACTED]", excerpt)
    return excerpt


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        index_rows = list(csv.DictReader(handle))

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; local-context-court-audit/1.0; +https://github.com/chriszavadil/PythonCode)",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
        }
    )

    outputs: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    classifications: Counter[str] = Counter()

    for seed in index_rows:
        judgment_id = seed["judgment_id"]
        try:
            detail, raw = fetch_detail(session, judgment_id)
            windows = local_windows(detail["content"])
            evaluated: list[tuple[int, str, str, dict[str, list[str]], list[str], list[str]]] = []
            for window in windows:
                groups = terms_by_group(window)
                actions = action_matches(window)
                incidental = incidental_matches(window)
                score = local_score(groups, actions)
                classification, reason = classify(groups, actions, incidental, score)
                evaluated.append((score, classification, reason, groups, actions, incidental, window))

            if evaluated:
                priority = {"strong_case_level_lead": 3, "manual_review": 2}
                evaluated.sort(key=lambda item: (priority.get(item[1], 0), item[0]), reverse=True)
                score, classification, reason, groups, actions, incidental, best_window = evaluated[0]
            else:
                score, classification, reason = 0, "rejected_no_local_window", "no transplant-term window found"
                groups = {group: [] for group in GROUPS}
                actions, incidental, best_window = [], [], ""

            classifications[classification] += 1
            outputs.append(
                {
                    "judgment_id": judgment_id,
                    "title": detail["title"] or seed.get("title", ""),
                    "roc_date": detail["date"] or seed.get("roc_date", ""),
                    "case_reason": detail["case_reason"] or seed.get("case_reason", ""),
                    "classification": classification,
                    "classification_reason": reason,
                    "local_score": score,
                    "organ_windows_examined": len(windows),
                    "matched_groups": " | ".join(group for group, terms in groups.items() if terms),
                    "matched_terms": " | ".join(sorted({term for terms in groups.values() for term in terms})),
                    "action_matches": " | ".join(actions),
                    "incidental_matches": " | ".join(incidental),
                    "best_window_redacted": redact(best_window),
                    "source_queries": seed.get("source_queries", ""),
                    "public_url": f"{DETAIL_URL}?ty=JD&id={quote(judgment_id, safe=',')}&ot=in",
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        except Exception as exc:
            errors.append({"judgment_id": judgment_id, "error": repr(exc)})

    outputs.sort(
        key=lambda row: (
            0 if row["classification"] == "strong_case_level_lead" else 1 if row["classification"] == "manual_review" else 2,
            -int(row["local_score"]),
            str(row["judgment_id"]),
        )
    )

    fields = [
        "judgment_id", "title", "roc_date", "case_reason", "classification",
        "classification_reason", "local_score", "organ_windows_examined",
        "matched_groups", "matched_terms", "action_matches", "incidental_matches",
        "best_window_redacted", "source_queries", "public_url", "content_sha256",
    ]
    write_csv(OUT / "all_local_classifications.csv", outputs, fields)
    write_csv(
        OUT / "surviving_leads.csv",
        [row for row in outputs if row["classification"] in {"strong_case_level_lead", "manual_review"}],
        fields,
    )
    write_csv(
        OUT / "rejected_false_positives.csv",
        [row for row in outputs if row["classification"].startswith("rejected_")],
        fields,
    )
    write_csv(OUT / "errors.csv", errors, ["judgment_id", "error"])

    baseline_row = next((row for row in outputs if row["judgment_id"] == BASELINE), None)
    if not baseline_row or baseline_row["classification"] != "strong_case_level_lead":
        raise RuntimeError(f"baseline failed local-context control: {baseline_row!r}")

    summary = {
        "generated_on": date.today().isoformat(),
        "input_judgments": len(index_rows),
        "judgments_retrieved": len(outputs),
        "errors": len(errors),
        "classifications": dict(classifications),
        "surviving_leads": sum(1 for row in outputs if row["classification"] in {"strong_case_level_lead", "manual_review"}),
        "strong_case_level_leads": sum(1 for row in outputs if row["classification"] == "strong_case_level_lead"),
        "manual_review": sum(1 for row in outputs if row["classification"] == "manual_review"),
        "baseline_control": BASELINE,
        "evidence_limit": "Survival means only that a public judgment contains a locally coherent cross-border transplant event or arrangement. It does not identify the donor or establish prisoner sourcing.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    (OUT / "REPORT.md").write_text(
        "\n".join(
            [
                "# Local-context validation of Taiwan judgment leads",
                "",
                f"Generated: {summary['generated_on']}",
                "",
                "The first-pass corpus scan counted terms across entire judgments and was intentionally over-inclusive. This second pass examines only small windows centered on transplant terms and requires local cross-border/hospital and transactional or operational anchors.",
                "",
                f"- Input judgments: {summary['input_judgments']}",
                f"- Retrieved: {summary['judgments_retrieved']}",
                f"- Strong case-level leads: {summary['strong_case_level_leads']}",
                f"- Manual-review leads: {summary['manual_review']}",
                f"- Total surviving: {summary['surviving_leads']}",
                f"- Errors: {summary['errors']}",
                "",
                "A surviving judgment is evidence of a cross-border transplant transaction or arrangement only. It is not evidence that the donor was a prisoner or that the organ was nonconsensually sourced.",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
