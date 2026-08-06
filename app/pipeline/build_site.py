#!/usr/bin/env python3
"""ledger.json -> site/data.json（網頁讀的那一份）。

網頁要標示的東西，逐條取自 docs/08〈一份清單該長什麼樣〉：

- 每一筆是**哪個訊號**判定的，以及那個訊號的意思。
- 日期是**發證日**，不是核准日，也不是我們觀察到的日期。
- 「本清單沒有」不等於「不是新藥」。
- 成分資料缺漏的筆數要現形（`M-31` 的同一條規則）。
- 每一個對外的數字都帶量測日期與母體。

以及 docs/08 的第 2 條：**改了判定演算法，就要把最後一份輸出跑出來看。**
這支程式是管線的最後一個介面，所以它自己也把控制跑一次，結果寫進 data.json，
讓網頁上那段「本清單如何判定新藥」的敘述有東西可以對照——那段敘述也是一個讀者。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "reference"))
sys.path.insert(0, str(_HERE.parent))

from tw_moiety import moieties  # noqa: E402
from tw_permit_id import permit_page_url  # noqa: E402
from tw_restraint import MONITORING_FAMILY, new_drug_monitoring  # noqa: E402
from ledger import SIGNALS, Ledger, now  # noqa: E402

NME_LIST_URL = "https://www.fda.gov.tw/TC/siteList.aspx?sid=2712"
LIC_API_URL = "https://lmspiq.fda.gov.tw/api/public/sh/piq/1000/licSearch"


def _controls() -> list[dict]:
    """離線控制。每個工具兩個：一個必須亮的正控，一個必須不亮的負控。

    只有正控的工具對「抓太多」完全沒有偵測能力——一個把所有東西都判成 true
    的比對器，正控會完美通過（fixtures/controls.json、docs/07 §1）。
    """
    out = []

    def check(name: str, kind: str, got, expect, why: str = ""):
        out.append({"name": name, "kind": kind, "got": repr(got),
                    "expect": repr(expect), "pass": got == expect, "why": why})

    check("EXACT-07 比對器・正控", "positive",
          new_drug_monitoring(["02 輸 入", "07 新藥監視"]).designated, True,
          "衛部菌疫輸字第001328號 的真實 restraintItemsCode 形狀")
    check("EXACT-07 比對器・負控", "negative",
          new_drug_monitoring(["01 國 產"]).designated, False,
          "衛署藥製字第033862號，METRONIDAZOLE 學名藥")
    check("沒有退化成子字串「監視」比對", "negative",
          new_drug_monitoring(["25 監視中學名藥", "24 監視期滿學名藥",
                               "26 監視期滿新藥"]).designated, False,
          f"碼表裡名稱含「監視」的有 {len(MONITORING_FAMILY)} 個；子字串作法這三個都會判 true，"
          "而正控仍然會通過")

    from tw_nme_titles import extract_permits
    check("多證公告沒漏（範圍展開）", "positive",
          extract_permits("台灣優時比貿易有限公司 衛部藥輸字第027714~027719號 「必治癲膜衣錠」"),
          ["衛部藥輸字第%06d號" % n for n in range(27714, 27720)],
          "只取每則第一張證，273 張裡會少 67 張（M-19）")
    check("短尾展開對", "positive",
          extract_permits("美商惠氏藥廠 衛部菌疫輸字第001196、97號「能增樂預填充注射筆」"),
          ["衛部菌疫輸字第001196號", "衛部菌疫輸字第001197號"],
          "錯誤展開會得到 000097")
    check("字別沒吃到公司名", "positive",
          extract_permits("台灣武田藥品工業股份有限公司衛部藥輸字第027623號「福星定膜衣錠20毫克」"),
          ["衛部藥輸字第027623號"],
          "用貪婪的中文字元類抓字別，會連「…有限公司衛部藥輸」一起吃進去"
          "（M-22：206 則裡 20 則）。正解是拿 61 筆的 Lic.Type 碼表當封閉集合，"
          "從右往左取最長匹配")
    check("產品名沒被當序號", "negative",
          extract_permits("台灣拜耳股份有限公司 衛部藥輸字第028325、26號 「可申達10、20毫克膜衣錠」"),
          ["衛部藥輸字第028325號", "衛部藥輸字第028326號"],
          "允許序號酬載含空白，產品名會被整段吃成序號")
    check("來源錯字沒被猜", "negative",
          extract_permits("台灣武田藥品工業股份有限公司 衛部藥字第027731號「癌能畢 膜衣錠90毫克」"),
          [], "衛部藥字不是合法字別。參考實作回報無法解析而不是猜一個——那是刻意的拒絕")

    from tw_permit_id import PermitIdError, lic_id, normalize_serial
    check("R 序號不補零", "positive", lic_id("衛部藥輸字第R00108號"), "52R00108",
          "補成 R000108 會得到 9 字元的 licId，正常是 8")
    try:
        got = normalize_serial("0271234")
    except PermitIdError:
        got = "PermitIdError"
    check("超長序號不截斷", "negative", got, "PermitIdError",
          "lpad 會截斷成 027123，指向另一張真實存在的許可證")

    return out


def _live_canary(led: Ledger) -> dict:
    """活體 canary：60001328（IBALIZUMAB）必須帶 07。

    只讀 ledger 裡已經查到的那一筆，**不另外發請求**——網頁重建不該打政府主機。
    如果它有一天不再帶 07，代表上游語意變了，本清單的結論就過期了（docs/02）。
    """
    rec = led.permits.get("衛部菌疫輸字第001328號")
    d = (rec or {}).get("detail") or {}
    codes = d.get("restraintItemsCode")
    if not rec or d.get("status") != "ok":
        return {"state": "unknown", "note": "這張證還沒查到明細，canary 無法判讀。"}
    ok = new_drug_monitoring(codes).designated
    return {
        "state": "ok" if ok else "stale",
        "queried_at": d.get("queried_at"),
        "codes": codes,
        "note": "上游語意未變。" if ok else
                "60001328 不再帶 07——上游語意可能變了，本清單的結論已過期，先別引用。",
    }


def build(led: Ledger) -> dict:
    rows = []
    n_pending = n_not_found = n_07 = n_moiety_missing = 0
    lags: list[int] = []

    for permit_no, rec in led.permits.items():
        detail = rec.get("detail") or {}
        status = detail.get("status") or "pending"
        issue_date, issue_field = Ledger.issue_date(rec)
        # `issueDate` 是**現行證**的發證日，換發會把它往後推（`M-21`：四個獨立
        # 訊號指向 2018，欄位值卻是 2025）。兩個欄位不一致的列要標出來，
        # 因為拿 `issueDate` 當排序鍵已經讓本 repo 把一張證判反過。
        raw_issue = str(detail.get("issueDate") or "")[:10] or None
        reissue = bool(issue_field == "oriIssueDate" and raw_issue and raw_issue != issue_date)

        ann = [e for e in rec["evidence"] if e["signal"] == "nme_report"]
        announced_on = min((e["confirmed_at"] for e in ann), default=None)
        pdf = ann[0]["basis"] if ann else None
        title = ann[0]["note"] if ann else ""

        lag = None
        if announced_on and issue_date:
            try:
                lag = (date.fromisoformat(announced_on) - date.fromisoformat(issue_date)).days
                lags.append(lag)
            except ValueError:
                lag = None

        ing = detail.get("ingredientsDesc") or []
        if isinstance(ing, str):
            ing = [ing]
        mset = moieties(";;".join(str(x) for x in ing if x)) if ing else moieties(None)

        codes = detail.get("restraintItemsCode") or []
        family = []
        if status == "ok":
            try:
                v = new_drug_monitoring(codes)
                family = list(v.monitoring_family_present)
            except Exception:
                family = []

        if status == "pending":
            n_pending += 1
        if status == "not_found":
            n_not_found += 1
        if Ledger.has_signal(rec, "restraint_07"):
            n_07 += 1
        if status == "ok" and mset.missing:
            n_moiety_missing += 1

        first = Ledger.first_signal(rec)
        rows.append({
            "permit_no": permit_no,
            "lic_id": rec["lic_id"],
            "product": detail.get("prodNameC"),
            "product_en": detail.get("prodNameE"),
            "applicant": detail.get("applicantName"),
            "issue_date": issue_date,
            "issue_date_field": issue_field,
            "_reissue": reissue,
            "_issueDate": raw_issue,
            "announced_on": announced_on,
            "announcement_title": title,
            "lag_days": lag,
            "pdf": pdf,
            "signals": sorted({e["signal"] for e in rec["evidence"]}),
            "first_signal": first["signal"] if first else None,
            "first_signal_at": first["confirmed_at"] if first else None,
            "first_seen": rec["first_seen"],
            "detail_status": status,
            "detail_queried_at": detail.get("queried_at"),
            "restraint_codes": codes,
            "monitoring_family": family,
            # 成分欄是**現況值，不是核發當時的快照**：它會隨變更登記改寫
            # （docs/05）。原始字串與正規化結果都留著，讓讀者看得到正規化
            # 動了什麼——剝鹽別讓 DASATINIB MONOHYDRATE 對回 DASATINIB，
            # 同一個動作也讓 leuprolide mesylate 與 acetate 塌成同一個字串。
            "ingredients_raw": [str(x) for x in ing],
            "moieties": sorted(mset.moieties),
            "dropped_excipients": list(mset.dropped_excipients),
            "moiety_missing": mset.missing if status == "ok" else None,
            "is_combination": mset.is_combination,
            "atc": detail.get("atcList") or [],
            "cancel_date": (str(detail.get("cancelDate"))[:10]
                            if detail.get("cancelDate") else None),
            "cancel_reason": detail.get("cancelReason"),
            "permit_url": permit_page_url(rec["lic_id"]),
        })

    rows.sort(key=lambda r: (r["announced_on"] or "", r["permit_no"]), reverse=True)

    n_ok = sum(1 for r in rows if r["detail_status"] == "ok")
    return {
        "generated_at": now(),
        "sources": {
            "nme_list": NME_LIST_URL,
            "lic_api": LIC_API_URL,
            "method_repo": "https://github.com/leon50906/tw-new-drug-signals",
        },
        "signals": SIGNALS,
        "stats": {
            "announcements": len(led.data.get("announcements") or []),
            "unresolved": len(led.data.get("unresolved") or []),
            "permits": len(rows),
            "detail_ok": n_ok,
            "detail_pending": n_pending,
            "detail_not_found": n_not_found,
            "designated_07": n_07,
            "moiety_missing": n_moiety_missing,
            "lag_median": (int(statistics.median(lags)) if lags else None),
            "lag_min": (min(lags) if lags else None),
            "lag_max": (max(lags) if lags else None),
            "lag_n": len(lags),
            "reissue": sum(1 for r in rows if r["_reissue"]),
        },
        "controls": _controls(),
        "canary": _live_canary(led),
        "unresolved": led.data.get("unresolved") or [],
        "rows": rows,
    }


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="ledger.json -> site/data.json")
    ap.add_argument("--ledger", default=str(_HERE.parents[1] / "data" / "ledger.json"))
    ap.add_argument("--out", default=str(_HERE.parents[1] / "site" / "data.json"))
    ap.add_argument("--html", default=str(_HERE.parents[1] / "site" / "index.html"))
    # 模板放在 pipeline/ 而不是 site/：site/ 整個目錄會被發布，
    # 一個還帶著 __DATA__ 佔位符的檔案不該有公開網址。
    ap.add_argument("--template", default=str(_HERE.parent / "template.html"))
    ap.add_argument("--fragment", default=None,
                    help="另外輸出一份去掉 html/head/body 外殼的片段（給會自己包骨架的宿主）")
    args = ap.parse_args(argv)

    led = Ledger.load(Path(args.ledger))
    payload = build(led)

    # 控制沒過就**不產生輸出**。一個比對器壞掉的頁面比沒有頁面更糟：
    # 它照樣長得像一份清單，而且掛在真名底下。
    failed = [c for c in payload["controls"] if not c["pass"]]
    if failed:
        for c in failed:
            print(f"# 控制失敗：{c['name']} got={c['got']} expect={c['expect']}", file=sys.stderr)
        print("# 不寫出任何檔案。舊的網頁維持原樣，直到這裡恢復。", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False, indent=1)
    out.write_text(blob, encoding="utf-8")

    # 資料直接內嵌進 HTML：單一檔就是完整的網頁，用 file:// 打開也能看，
    # 不需要伺服器、也不會因為 fetch 被擋而變成一頁空表。
    tpl = Path(args.template).read_text(encoding="utf-8")
    if "__DATA__" not in tpl:
        print(f"ERROR: {args.template} 裡沒有 __DATA__ 佔位符", file=sys.stderr)
        return 2
    inline = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("__DATA__", inline)
    Path(args.html).write_text(page, encoding="utf-8")

    if args.fragment:
        # 同一份頁面，去掉外層骨架（doctype / html / head / body），
        # 給那些會自己包骨架的宿主用。<style> 與 <script> 原樣留著。
        frag = re.sub(r"(?is)^.*?<head[^>]*>", "", page)
        frag = frag.replace("</head>", "").replace("</html>", "")
        frag = re.sub(r"(?is)<title>.*?</title>", "", frag)
        frag = re.sub(r"(?is)<meta[^>]*>", "", frag)
        frag = re.sub(r"(?is)</?body[^>]*>", "", frag)
        Path(args.fragment).write_text(frag.strip() + "\n", encoding="utf-8")
        print(f"# {args.fragment}", file=sys.stderr)

    s = payload["stats"]
    print(f"# {args.html}", file=sys.stderr)
    print(f"# {args.out}", file=sys.stderr)
    print(f"# 公告 {s['announcements']} 則、候選證 {s['permits']} 張、"
          f"已查明細 {s['detail_ok']}（待查 {s['detail_pending']}、查無 {s['detail_not_found']}）、"
          f"帶 07 {s['designated_07']} 張、成分缺漏 {s['moiety_missing']} 張、"
          f"無法解析 {s['unresolved']} 則", file=sys.stderr)
    print(f"# canary: {payload['canary']['state']}", file=sys.stderr)
    print(f"# 控制 {len(payload['controls'])} 項全過", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
