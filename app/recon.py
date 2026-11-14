"""渠道流水核对的核心逻辑：CSV 解析、差异计算、任务与报告的内存仓库。

本模块只依赖标准库，便于脱离数据库单独测试。

核对语义：
- 按订单号（order_no）对齐渠道 CSV 与库内 settlement_orders；
- 任一侧同号重复 -> 列为「冲突」，两侧原始行全部列出，不做任意配对；
- 一侧独有 -> 「渠道独有」/「库内独有」；
- 两侧各一行但商户、金额或手续费不同 -> 「字段不符」，并指出具体字段；
- 发布按提交序号（seq）守卫：只许更新的报告替换旧的，迟到任务不得覆盖。
"""
from __future__ import annotations

import csv
import io
import itertools
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_ROWS = 200_000        # CSV 最大数据行数
MAX_FIELD_ERRORS = 20     # 解析失败时最多回报的行错误条数
REPORT_LIST_CAP = 5_000   # 报告中每类差异最多保留的条数（超出截断并在 truncated 标记）

REQUIRED_COLUMNS = ("order_no", "merchant_id", "amount", "fee")

FINISHED_STATUSES = ("published", "stale", "failed")


class CsvError(ValueError):
    """CSV 解析失败；消息可直接展示给对账员。"""


@dataclass(frozen=True)
class FlowRow:
    """一侧的一行流水：渠道 CSV 行或库内订单行。"""

    order_no: str
    merchant_id: int
    amount: Decimal
    fee: Decimal
    source_line: int | None = None  # 渠道侧 CSV 行号，便于对账员回查文件

    def to_dict(self) -> dict:
        return {
            "order_no": self.order_no,
            "merchant_id": self.merchant_id,
            "amount": str(self.amount),
            "fee": str(self.fee),
            "line": self.source_line,
        }


def _parse_decimal(raw: str, line: int, col: str) -> Decimal:
    s = raw.strip()
    try:
        value = Decimal(s)
    except (InvalidOperation, ValueError):
        raise CsvError(f"第 {line} 行：{col} 不是合法数字：{s!r}")
    if not value.is_finite():
        raise CsvError(f"第 {line} 行：{col} 不是有限数字：{s!r}")
    return value


def parse_channel_csv(text: str) -> list[FlowRow]:
    """解析渠道流水 CSV。任何格式问题都抛 CsvError（整个任务失败，不出半份报告）。

    必需表头：order_no,merchant_id,amount,fee（大小写/空格不敏感，允许额外列）。
    同号重复不算解析错误——那是报告里的「冲突」。
    """
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header_len = 0
    col_idx: dict[str, int] = {}
    rows: list[FlowRow] = []
    errors: list[str] = []

    for raw in reader:
        line = reader.line_num
        if not raw or all(not cell.strip() for cell in raw):
            continue  # 跳过空行
        if not col_idx:
            names = [cell.strip().lower() for cell in raw]
            missing = [c for c in REQUIRED_COLUMNS if c not in names]
            if missing:
                raise CsvError(
                    "表头缺少必需列：{}（需要 {}）".format(
                        ", ".join(missing), ",".join(REQUIRED_COLUMNS)
                    )
                )
            col_idx = {name: names.index(name) for name in REQUIRED_COLUMNS}
            header_len = len(raw)
            continue

        if len(raw) != header_len:
            errors.append(f"第 {line} 行：列数 {len(raw)} 与表头 {header_len} 不一致")
        else:
            try:
                order_no = raw[col_idx["order_no"]].strip()
                if not order_no:
                    raise CsvError(f"第 {line} 行：order_no 为空")
                if len(order_no) > 64:
                    raise CsvError(f"第 {line} 行：order_no 超过 64 字符")
                merchant_raw = raw[col_idx["merchant_id"]].strip()
                try:
                    merchant_id = int(merchant_raw)
                except ValueError:
                    raise CsvError(f"第 {line} 行：merchant_id 不是整数：{merchant_raw!r}")
                rows.append(
                    FlowRow(
                        order_no=order_no,
                        merchant_id=merchant_id,
                        amount=_parse_decimal(raw[col_idx["amount"]], line, "amount"),
                        fee=_parse_decimal(raw[col_idx["fee"]], line, "fee"),
                        source_line=line,
                    )
                )
            except CsvError as exc:
                errors.append(str(exc))
        if len(errors) >= MAX_FIELD_ERRORS:
            break
        if len(rows) > MAX_ROWS:
            raise CsvError(f"数据行超过上限 {MAX_ROWS}")

    if not col_idx:
        raise CsvError("CSV 为空或缺少表头行")
    if errors:
        msg = "；".join(errors)
        raise CsvError(msg + ("；……" if len(errors) >= MAX_FIELD_ERRORS else ""))
    if not rows:
        raise CsvError("CSV 没有数据行")
    return rows


def compute_diff(channel_rows: list[FlowRow], db_rows: list[FlowRow]) -> dict:
    """按订单号对齐两侧，产出报告主体（汇总 + 四类差异清单）。

    同号重复（任一侧）一律进 conflicts，不参与独有/不符的判定，绝不任意配对。
    """
    ch_map: dict[str, list[FlowRow]] = {}
    for r in channel_rows:
        ch_map.setdefault(r.order_no, []).append(r)
    db_map: dict[str, list[FlowRow]] = {}
    for r in db_rows:
        db_map.setdefault(r.order_no, []).append(r)

    channel_only: list[dict] = []
    db_only: list[dict] = []
    mismatches: list[dict] = []
    conflicts: list[dict] = []
    matched = 0

    for order_no in sorted(set(ch_map) | set(db_map)):
        ch = ch_map.get(order_no, [])
        db = db_map.get(order_no, [])
        if len(ch) > 1 or len(db) > 1:
            conflicts.append(
                {
                    "order_no": order_no,
                    "channel_rows": [r.to_dict() for r in ch],
                    "db_rows": [r.to_dict() for r in db],
                }
            )
            continue
        if not db:
            channel_only.append(ch[0].to_dict())
            continue
        if not ch:
            db_only.append(db[0].to_dict())
            continue
        c, d = ch[0], db[0]
        fields = [
            name
            for name, a, b in (
                ("merchant_id", c.merchant_id, d.merchant_id),
                ("amount", c.amount, d.amount),
                ("fee", c.fee, d.fee),
            )
            if a != b
        ]
        if fields:
            mismatches.append(
                {
                    "order_no": order_no,
                    "fields": fields,
                    "channel": c.to_dict(),
                    "db": d.to_dict(),
                }
            )
        else:
            matched += 1

    summary = {
        "channel_rows": len(channel_rows),
        "db_rows": len(db_rows),
        "matched": matched,
        "channel_only": len(channel_only),
        "db_only": len(db_only),
        "mismatch": len(mismatches),
        "conflict": len(conflicts),
    }

    truncated: dict[str, bool] = {}

    def cap(items: list, key: str) -> list:
        if len(items) > REPORT_LIST_CAP:
            truncated[key] = True
            return items[:REPORT_LIST_CAP]
        return items

    return {
        "summary": summary,
        "truncated": truncated,
        "channel_only": cap(channel_only, "channel_only"),
        "db_only": cap(db_only, "db_only"),
        "mismatches": cap(mismatches, "mismatch"),
        "conflicts": cap(conflicts, "conflict"),
    }


@dataclass
class ReconTask:
    """一次上传核对任务。状态：running -> published | stale | failed。"""

    settle_date: str
    filename: str
    seq: int  # 提交序号，单调递增；发布守卫依据
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"
    error: str | None = None      # 失败原因 / 未覆盖说明（网页展示）
    summary: dict | None = None   # 完成后的差异统计
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "settle_date": self.settle_date,
            "filename": self.filename,
            "status": self.status,
            "error": self.error,
            "summary": self.summary,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


@dataclass
class ReconReport:
    """某个结算日当前已发布的核对报告。"""

    settle_date: str
    task_id: str
    seq: int
    published_at: float
    summary: dict
    truncated: dict
    channel_only: list
    db_only: list
    mismatches: list
    conflicts: list

    def to_dict(self) -> dict:
        return {
            "settle_date": self.settle_date,
            "task_id": self.task_id,
            "seq": self.seq,
            "published_at": self.published_at,
            "summary": self.summary,
            "truncated": self.truncated,
            "channel_only": self.channel_only,
            "db_only": self.db_only,
            "mismatches": self.mismatches,
            "conflicts": self.conflicts,
        }


class ReconStore:
    """内存仓库：核对任务（限量保留）+ 每个结算日一份已发布报告。"""

    def __init__(self, task_capacity: int = 100):
        self._seq = itertools.count(1)
        self._tasks: dict[str, ReconTask] = {}
        self._order: list[str] = []
        self._reports: dict[str, ReconReport] = {}
        self._capacity = task_capacity

    def next_seq(self) -> int:
        return next(self._seq)

    def add_task(self, task: ReconTask) -> None:
        self._tasks[task.id] = task
        self._order.append(task.id)
        while len(self._order) > self._capacity:
            oldest = self._order[0]
            if self._tasks[oldest].status in FINISHED_STATUSES:
                self._order.pop(0)
                del self._tasks[oldest]
            else:
                break  # 最旧的还在跑，不再淘汰

    def get_task(self, task_id: str) -> ReconTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ReconTask]:
        return [self._tasks[i] for i in reversed(self._order) if i in self._tasks]

    def latest_task_for(self, settle_date: str) -> ReconTask | None:
        for task_id in reversed(self._order):
            task = self._tasks.get(task_id)
            if task is not None and task.settle_date == settle_date:
                return task
        return None

    def publish(self, task: ReconTask, report: ReconReport) -> str:
        """发布守卫：同一结算日只许序号更大的报告替换旧的。

        较晚提交的任务已发布后，先提交的迟到任务到达时 seq 更小，
        返回 "stale"，旧（更新）报告保持不变。事件循环内检查与替换之间
        没有 await，判定-替换是原子的。
        """
        current = self._reports.get(task.settle_date)
        if current is not None and current.seq >= task.seq:
            return "stale"
        self._reports[task.settle_date] = report
        return "published"

    def get_report(self, settle_date: str) -> ReconReport | None:
        return self._reports.get(settle_date)

    def list_reports(self) -> list[ReconReport]:
        return [self._reports[d] for d in sorted(self._reports, reverse=True)]
