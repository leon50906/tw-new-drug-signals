#!/usr/bin/env python3
"""持續更新的清單：兩層儲存 + 逐筆首次觀察。

形狀來自 docs/08：

- **每張證一列**：許可證號、真正的發證日、第一次在快照裡看到它的日期、是否已回報。
- **每個訊號一筆證據**掛在那張證底下：哪個訊號、什麼時候確認的、依據是什麼。

為什麼不是「證上的一個欄位」：三個訊號的到達時間不同（`07` 不必等公告，
新成分新藥公告中位晚 69 天，`M-14`）。一個 `marker_source` 文字欄位會讓第二個
訊號抹掉第一個，而抹掉之後沒有任何欄位會抗議。所以證據只增不改，
回報時間取**最早**那一筆。

不用尾隨時間窗。理由見 docs/08：資料本身落後 20–29 天（`M-45`）、41% 的週是零
（`M-46`）、而公告中位晚 69 天（`M-14`）——三件事各自都足以讓時間窗安靜地漏掉
每一個晚公告的品項。這裡問的是「這一筆我以前見過嗎」。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

SCHEMA = 1

TAIPEI = _dt.timezone(_dt.timedelta(hours=8))

#: 訊號代號 -> (顯示名, 它是什麼)。最後一欄逐字取自 docs/08 的身分表，
#: 因為那三個訊號**不是同一種東西**，寫成一句「都是候選產生器」會低估其中一個。
SIGNALS: dict[str, dict[str, str]] = {
    "nme_report": {
        "label": "新成分新藥公告",
        "is": "食藥署自己審查認定並公告的新成分新藥。清單上的正面事實可以直接引用。",
        "is_not": "它的沉默不可用：不在上面推不出「不是新藥」。",
    },
    "restraint_07": {
        "label": "07 新藥監視",
        "is": "一個可以直接讀到的事實：這張證的明細裡有 07 這個碼。",
        "is_not": "不是法定身分的判準。食藥署未公開收錄準則，帶 07 不等於經審查認定為藥事法第 7 條新藥。",
    },
}


def today() -> str:
    return _dt.datetime.now(TAIPEI).date().isoformat()


def now() -> str:
    return _dt.datetime.now(TAIPEI).isoformat(timespec="seconds")


class Ledger:
    """JSON 檔背後的清單。載入 -> 改 -> 存檔。"""

    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path
        self.data = data

    # ---------------------------------------------------------------- 開檔

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") != SCHEMA:
                raise SystemExit(
                    f"{path} 的 schema 是 {data.get('schema')}，本程式是 {SCHEMA}。"
                    "不自動遷移——請自己看過再改。"
                )
        else:
            data = {
                "schema": SCHEMA,
                "created_at": now(),
                "permits": {},
                "announcements": [],
                "unresolved": [],
                "runs": [],
            }
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def log_run(self, stage: str, note: str) -> None:
        runs = self.data.setdefault("runs", [])
        runs.append({"at": now(), "stage": stage, "note": note})
        del runs[:-60]

    # ------------------------------------------------------------ 每張證一列

    @property
    def permits(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("permits", {})

    def observe(self, permit_no: str, lic_id: str) -> dict[str, Any]:
        """逐筆首次觀察。沒見過就記一筆，見過就原樣回傳——**不覆寫 first_seen**。"""
        rec = self.permits.get(permit_no)
        if rec is None:
            rec = {
                "permit_no": permit_no,
                "lic_id": lic_id,
                "first_seen": today(),
                "reported": False,
                "evidence": [],
                "detail": None,
            }
            self.permits[permit_no] = rec
        return rec

    # -------------------------------------------------------- 每個訊號一筆證據

    @staticmethod
    def add_evidence(
        rec: dict[str, Any],
        signal: str,
        confirmed_at: str,
        basis: str,
        note: str = "",
    ) -> bool:
        """加一筆證據。已存在（同訊號同依據）就不動它。

        證據**只增不改**：同一張證先以 `07` 進來、幾個月後才被公告確認，
        那是同一筆的補強，不是把前一個訊號覆蓋掉。回傳是否新增。
        """
        if signal not in SIGNALS:
            raise ValueError(f"未知訊號 {signal!r}")
        for ev in rec["evidence"]:
            if ev["signal"] == signal and ev["basis"] == basis:
                return False
        rec["evidence"].append({
            "signal": signal,
            "confirmed_at": confirmed_at,
            "basis": basis,
            "note": note,
            "observed_at": today(),
        })
        rec["evidence"].sort(key=lambda e: (e["confirmed_at"] or "", e["signal"]))
        return True

    @staticmethod
    def first_signal(rec: dict[str, Any]) -> dict[str, Any] | None:
        """最早那一筆證據——「這張證最早是被哪個訊號看見的」。

        回報時取的是這個，不是最新那一筆。
        """
        if not rec["evidence"]:
            return None
        return min(rec["evidence"], key=lambda e: (e["confirmed_at"] or "9999", e["signal"]))

    @staticmethod
    def has_signal(rec: dict[str, Any], signal: str) -> bool:
        return any(e["signal"] == signal for e in rec["evidence"])

    # ------------------------------------------------------------------ 日期

    @staticmethod
    def issue_date(rec: dict[str, Any]) -> tuple[str | None, str | None]:
        """回傳（真正的發證日, 它取自哪個欄位）。

        `issueDate` **不保證是最初核發日**（docs/00 §F、docs/06、`M-21`）：
        換發會把它往後推，dupilumab 那張證的四個獨立訊號指向 2018，欄位值卻是
        2025。要精確到單張證，用 `oriIssueDate`。這裡優先取 `oriIssueDate`，
        並把用了哪個欄位一起回傳，讓下游有機會標示。
        """
        d = rec.get("detail") or {}
        for field in ("oriIssueDate", "issueDate"):
            v = d.get(field)
            if v:
                return str(v)[:10], field
        return None, None
