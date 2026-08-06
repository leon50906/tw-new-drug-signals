#!/usr/bin/env python3
"""訊號一：逐張證查明細，判定 `07 新藥監視`，順便檢定「這張證真的存在嗎」。

    POST https://lmspiq.fda.gov.tw/api/public/sh/piq/1000/licSearch

## 這支程式的兩條硬規則

**一、422 不是資料。** 主機在負載下回 422「連接次數過於頻繁」，那是我方 IP 被
限流，不是「這張證不存在」，也不是「這個標記消失了」（docs/00 §C）。把 422
記錄成「無此標記」會得到一份假的空清單。所以：**任何不是「查無明細」的失敗
一律中止本輪，已完成的部分照樣存檔，下一輪從斷點續跑。**

**二、一次一筆，預設間隔 30 秒。** 這是 repo 自己的節奏（`M-44`，2026-08-05
以此節奏取 11 張未觸發 422）。每輪有預算上限（`--budget`），跑完就停，
剩下的下一輪再來——所以第一次不會一口氣打 272 次。

## 順便完成的檢定

tw_nme_titles 把 `第029081-82號` 展開成兩張證，那只是**候選**；
真正的檢定是「它存在於許可證資料裡嗎」（docs/03）。查得到明細 = 通過，
查無明細 = 這個展開造出了一張不存在的證，記成 `not_found` 並在網頁上現形。

## `07` 怎麼判

用 `tw_restraint.new_drug_monitoring()`：**取每個元素第一個空白之前那段，
比對是否等於 `07`**。不可以拿子字串「監視」比對——碼表 91 筆裡名稱含「監視」
的有 8 個（`M-04`），子字串作法會把 `24 監視期滿學名藥` 之類全部判成 true，
**而正控仍然會通過**（docs/02 陷阱 a）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "reference"))
sys.path.insert(0, str(_HERE.parent))

import tfda_lic_query as lic_api  # noqa: E402
from tw_restraint import new_drug_monitoring  # noqa: E402
from ledger import Ledger, today  # noqa: E402

#: 明細裡要留下來的欄位。加欄位前先讀 docs/00 §C 那張表——
#: `monitorDate` 名字最像答案，但它是**醫療器材**欄位，藥品為 null。
KEEP_FIELDS = (
    "certNo", "prodNameC", "prodNameE", "restraintItemsCode",
    "issueDate", "oriIssueDate", "validDate", "licStatus",
    "cancelDate", "cancelReason",
    "ingredientsDesc", "atcList", "applicantName", "makerName",
)

#: 「這個 licId 查無明細」的訊息片段。tfda_lic_query 只丟一種例外型別，
#: 所以這裡靠訊息辨識——**辨識不出來就當成傳輸失敗中止**，方向是安全的：
#: 寧可少記一筆，也不要把限流寫成「無此標記」。
_NOT_FOUND_MARK = "查無明細"


class Throttled(RuntimeError):
    """我方被限流或連不上。不是資料。"""


def query_one(lic_id: str, timeout: float = 45.0) -> dict | None:
    """回傳明細 dict；查無明細回 None；其餘一律丟 Throttled。"""
    try:
        return lic_api.query(lic_id, timeout=timeout)
    except lic_api.LicQueryError as exc:
        if _NOT_FOUND_MARK in str(exc):
            return None
        raise Throttled(str(exc)) from exc
    except Exception as exc:  # 連線逾時等
        raise Throttled(f"{type(exc).__name__}: {exc}") from exc


def _needs_query(rec: dict, refresh_days: int) -> bool:
    d = rec.get("detail")
    if not d:
        return True
    if d.get("status") == "not_found":
        return False          # 已經檢定過不存在，不重複打
    if refresh_days <= 0:
        return False
    from datetime import date
    try:
        seen = date.fromisoformat(str(d.get("queried_at"))[:10])
    except Exception:
        return True
    return (date.today() - seen).days >= refresh_days


def enrich(
    led: Ledger,
    budget: int,
    interval: float,
    refresh_days: int = 0,
    verbose: bool = True,
    checkpoint: bool = True,
) -> dict:
    """查 `budget` 張還沒查過的證。回傳統計。

    **每查完一張就存檔。** 補完全部候選證要好幾個小時，只在最後存一次的話，
    中途被砍掉就等於那幾小時的請求全部白打，而下一輪還得重打一次——
    這正是節流想避免的事。
    """
    pending = [r for r in led.permits.values() if _needs_query(r, refresh_days)]
    # 先查新的（公告日晚的優先），讓網頁上最新的那幾筆最快補齊。
    pending.sort(key=lambda r: max((e["confirmed_at"] or "" for e in r["evidence"]), default=""),
                 reverse=True)
    todo = pending[:budget]

    stats = {"queried": 0, "found": 0, "not_found": 0, "designated_07": 0,
             "pending_before": len(pending), "aborted": None}

    for i, rec in enumerate(todo):
        if i:
            time.sleep(interval)
        lic_id = rec["lic_id"]
        try:
            data = query_one(lic_id)
        except Throttled as exc:
            # 這裡**什麼都不寫**。已完成的部分在 finally 之外由呼叫端存檔。
            stats["aborted"] = str(exc)
            if verbose:
                print(f"# 中止：{exc}", file=sys.stderr)
                print("# 這不是「標記消失」，也不是「這張證不存在」。下一輪從斷點續跑。",
                      file=sys.stderr)
            break

        stats["queried"] += 1
        if data is None:
            rec["detail"] = {"status": "not_found", "queried_at": today()}
            stats["not_found"] += 1
            if verbose:
                print(f"# {rec['permit_no']} ({lic_id})：查無明細——展開造出了一張不存在的證",
                      file=sys.stderr)
            continue

        detail = {k: data.get(k) for k in KEEP_FIELDS}
        detail["status"] = "ok"
        detail["queried_at"] = today()
        rec["detail"] = detail
        stats["found"] += 1

        codes = data.get("restraintItemsCode")
        try:
            verdict = new_drug_monitoring(codes)
        except Exception as exc:
            detail["restraint_error"] = str(exc)
            verdict = None

        if verdict is not None and verdict.designated:
            stats["designated_07"] += 1
            led.add_evidence(
                rec,
                signal="restraint_07",
                # 查得日，不是「這個標記出現的日期」。本 repo 沒有逐日追蹤過任何
                # 一張證的明細，說不出標記何時出現（docs/02）。
                confirmed_at=today(),
                basis=f"licSearch licBaseId={lic_id}",
                note="查詢當下明細的 restraintItemsCode 含碼 07",
            )
        if verbose:
            # `verdict` 不可直接當布林值——tw_restraint 讓誤用當場爆炸，
            # 因為 `.designated` 的意思只有「這張證的限制項目裡有 07 這個碼」。
            mark = "07" if (verdict is not None and verdict.designated) else "--"
            print(f"# [{i+1}/{len(todo)}] {rec['permit_no']} {mark} {data.get('prodNameC')}",
                  file=sys.stderr, flush=True)
        if checkpoint:
            led.save()

    stats["pending_after"] = len([r for r in led.permits.values() if _needs_query(r, refresh_days)])
    led.log_run("enrich_details", str(stats))
    return stats


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="逐張查明細，判定 07 新藥監視（節流）")
    ap.add_argument("--ledger", default=str(_HERE.parents[1] / "data" / "ledger.json"))
    ap.add_argument("--budget", type=int, default=30, help="本輪最多查幾張（預設 %(default)s）")
    ap.add_argument("--interval", type=float, default=lic_api.DEFAULT_MIN_INTERVAL,
                    help="兩次請求的間隔秒數（預設 %(default)s，善待政府主機）")
    ap.add_argument("--refresh-days", type=int, default=0,
                    help="超過幾天就重查一次（0 = 查過就不再查）")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="只在結束時存檔（預設每查完一張就存）")
    args = ap.parse_args(argv)

    led = Ledger.load(Path(args.ledger))
    try:
        stats = enrich(led, budget=args.budget, interval=args.interval,
                       refresh_days=args.refresh_days,
                       checkpoint=not args.no_checkpoint)
    finally:
        led.save()   # 中止也要存：已完成的查詢不重跑
    print(f"# {stats}", file=sys.stderr)
    # 被限流不算失敗到需要讓 CI 紅掉——它是預期中的節流，下一輪續跑。
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
