#!/usr/bin/env python3
"""訊號二：抓〈新成分新藥核准審查報告摘要〉全部公告，解析標題成許可證號。

    https://www.fda.gov.tw/TC/siteList.aspx?sid=2712        （第 1 頁）
    https://www.fda.gov.tw/TC/siteList.aspx?sid=2712&pn=N   （第 N 頁）

這是食藥署唯一以「新成分新藥」命名的逐案清單，**它列出來的每一筆都是權威**
（docs/03）。它的沉默不是——不在上面推不出「不是新藥」。

## 三個不肯安靜失敗的地方

1. **HTTP 錯誤一律中止**，不寫檔。抓網頁抓到錯誤頁再存成資料，會得到一份
   看起來很正常的短清單（docs/00 §C 的 422 同一個形態）。
2. **筆數要對得起來**。頁面自己印「共 N 筆資料」，解析出來的列數必須等於 N。
   少抓沒有徵兆——你得到一份比較短的清單，不會得到錯誤（tw_nme_titles 的
   模組 docstring 就是在講這件事）。對不上就中止。
3. **無法解析的標題必須現形**，寫進 ledger 的 `unresolved`，不靜默丟棄。
   已知一例是來源錯字「衛部藥字第027731號」（字別不合法），參考實作依設計
   回報無法解析而不是猜一個——那是刻意的拒絕，不是失敗（docs/03 陷阱 d）。

展開出來的許可證號是**候選**。「它存在於許可證資料裡嗎」才是檢定，
而那個檢定在 enrich_details.py（docs/03〈展開是假設，「存在」才是檢定〉）。
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "reference"))
sys.path.insert(0, str(_HERE.parent))

from tw_nme_titles import parse_title  # noqa: E402
from tw_permit_id import PermitIdError, parse_permit_number  # noqa: E402
from ledger import Ledger, today  # noqa: E402

BASE = "https://www.fda.gov.tw/TC/siteList.aspx?sid=2712"
SITE = "https://www.fda.gov.tw"
_UA = "tw-new-drug-signals-ledger/1.0 (+https://github.com/leon50906/tw-new-drug-signals)"

#: 一列 = 序號 + 標題（含 PDF 連結）+ 發布日期。來源的 <tr> 沒有收尾標籤，
#: 所以錨定在三個 <td> 的形狀上，不靠 </tr>。
_ROW_RE = re.compile(
    r"<td class='alignCenter' >(\d+)</td>"
    r"\s*<td[^>]*>\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>\s*</td>"
    r"\s*<td class='alignCenter' >(\d{4}-\d{2}-\d{2})</td>",
    re.S,
)
_TOTAL_RE = re.compile(r"共<span class=\"pageHighlight\">(\d+)</span>筆資料")
_PAGES_RE = re.compile(r"第<span class=\"pageHighlight\">\d+</span>/\s*<span class=\"pageHighlight\">(\d+)</span>頁")
_TAG_RE = re.compile(r"<[^>]+>")


class FetchError(RuntimeError):
    pass


def _get(url: str, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status}：{url}")
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FetchError(
            f"HTTP {exc.code} {exc.reason}：{url}。"
            "中止，不寫檔——把錯誤頁存成資料會得到一份假的短清單。"
        ) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"連線失敗：{url}（{exc.reason}）") from exc


def _clean_title(inner_html: str) -> str:
    return html.unescape(_TAG_RE.sub("", inner_html)).strip()


def _parse_page(body: str) -> tuple[list[dict], int | None, int | None]:
    rows = []
    for seq, href, inner, date in _ROW_RE.findall(body):
        rows.append({
            "seq": int(seq),
            "title": _clean_title(inner),
            "announced_on": date,
            "pdf": SITE + html.unescape(href) if href.startswith("/") else html.unescape(href),
        })
    total = _TOTAL_RE.search(body)
    pages = _PAGES_RE.search(body)
    return rows, (int(total.group(1)) if total else None), (int(pages.group(1)) if pages else None)


def fetch_all(interval: float = 3.0, verbose: bool = True) -> list[dict]:
    """抓完全部頁面。筆數對不上就中止。"""
    body = _get(BASE)
    rows, total, pages = _parse_page(body)
    if total is None or pages is None:
        raise FetchError(
            "頁面上找不到「共 N 筆資料 / 第 x/y 頁」。版面可能改了——"
            "在確認新版面之前中止，不要用一份可能少抓的清單覆蓋 ledger。"
        )
    if verbose:
        print(f"# 共 {total} 筆、{pages} 頁；第 1 頁取到 {len(rows)} 列", file=sys.stderr)

    for pn in range(2, pages + 1):
        time.sleep(interval)
        page_rows, _, _ = _parse_page(_get(f"{BASE}&pn={pn}"))
        if not page_rows:
            raise FetchError(f"第 {pn} 頁解析到 0 列。中止。")
        rows.extend(page_rows)
        if verbose:
            print(f"# 第 {pn}/{pages} 頁：{len(page_rows)} 列（累計 {len(rows)}）", file=sys.stderr)

    # 正控：解析出來的列數必須等於頁面自己宣告的筆數。
    if len(rows) != total:
        raise FetchError(
            f"解析到 {len(rows)} 列，頁面宣告 {total} 筆。少抓沒有徵兆，所以這裡中止。"
        )
    # 序號應該是 1..total 的全域連號；不連號代表分頁抓漏或重複。
    seqs = sorted(r["seq"] for r in rows)
    if seqs != list(range(1, total + 1)):
        missing = sorted(set(range(1, total + 1)) - set(seqs))
        raise FetchError(f"序號不連續，缺 {missing[:10]}…。中止。")
    return rows


def update_ledger(led: Ledger, rows: list[dict], verbose: bool = True) -> dict:
    """把公告寫進 ledger：每張候選證一列，每則公告一筆證據。"""
    led.data["announcements"] = rows
    led.data["unresolved"] = []
    new_permits = 0
    new_evidence = 0
    candidates = 0

    for row in rows:
        parsed = parse_title(row["title"])
        for note in parsed.unresolved:
            led.data["unresolved"].append({
                "title": row["title"],
                "announced_on": row["announced_on"],
                "pdf": row["pdf"],
                "note": note,
            })
        for permit_no in parsed.permits:
            candidates += 1
            try:
                lic = parse_permit_number(permit_no)
            except PermitIdError as exc:
                led.data["unresolved"].append({
                    "title": row["title"],
                    "announced_on": row["announced_on"],
                    "pdf": row["pdf"],
                    "note": f"{permit_no}：{exc}",
                })
                continue
            before = permit_no in led.permits
            rec = led.observe(lic.canonical, lic.lic_id)
            if not before:
                new_permits += 1
            if led.add_evidence(
                rec,
                signal="nme_report",
                confirmed_at=row["announced_on"],   # 公告日，不是我們的觀察日
                basis=row["pdf"],
                note=row["title"],
            ):
                new_evidence += 1

    stats = {
        "announcements": len(rows),
        "candidate_permits": candidates,
        "distinct_permits": len(led.permits),
        "new_permits": new_permits,
        "new_evidence": new_evidence,
        "unresolved": len(led.data["unresolved"]),
    }
    if verbose:
        print(f"# {stats}", file=sys.stderr)
    led.log_run("fetch_nme_list", str(stats))
    return stats


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="抓新成分新藥公告清單並更新 ledger")
    ap.add_argument("--ledger", default=str(_HERE.parents[1] / "data" / "ledger.json"))
    ap.add_argument("--interval", type=float, default=3.0, help="翻頁間隔秒數（預設 %(default)s）")
    args = ap.parse_args(argv)

    try:
        rows = fetch_all(interval=args.interval)
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    led = Ledger.load(Path(args.ledger))
    update_ledger(led, rows)
    led.save()
    print(f"# 已寫入 {args.ledger}（{today()}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
