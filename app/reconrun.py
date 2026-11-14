"""核对任务执行器：解析 CSV -> 同一快照取库内订单 -> 计算差异 -> 按序号守卫发布。

快照语义：取数在一条专用连接的「REPEATABLE READ, READ ONLY」事务里完成，
本次核对的全部查询共享同一快照；核对期间其他连接补记的订单不会混入报告。

发布语义：只有全部步骤成功才替换该结算日的报告；解析、查询或发布任一
环节失败，已发布的旧报告原样保留，任务标记失败并记录原因。
"""
from __future__ import annotations

import asyncio
import time
from datetime import date

import asyncpg

from recon import (
    CsvError,
    FlowRow,
    ReconReport,
    ReconStore,
    ReconTask,
    compute_diff,
    parse_channel_csv,
)


class ReconRunner:
    def __init__(self, dsn: str, store: ReconStore):
        self._dsn = dsn
        self._store = store

    def start(self, task: ReconTask, csv_text: str) -> None:
        """后台启动一个核对任务；任务之间可并发，发布由序号守卫保证有序。"""
        asyncio.create_task(self._run(task, csv_text))

    async def _run(self, task: ReconTask, csv_text: str) -> None:
        try:
            channel_rows = parse_channel_csv(csv_text)
            db_rows = await self._fetch_orders(task.settle_date)
            body = compute_diff(channel_rows, db_rows)
            report = ReconReport(
                settle_date=task.settle_date,
                task_id=task.id,
                seq=task.seq,
                published_at=time.time(),
                **body,
            )
            outcome = self._store.publish(task, report)
            task.summary = body["summary"]
            if outcome == "published":
                task.status = "published"
            else:
                task.status = "stale"
                task.error = "已有更晚提交的报告发布，本任务结果未覆盖（迟到任务）"
        except CsvError as exc:
            task.status = "failed"
            task.error = f"CSV 解析失败：{exc}"
        except Exception as exc:  # 查询/发布等失败：旧报告保留，原因上网页
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished_at = time.time()

    async def _fetch_orders(self, settle_date: str) -> list[FlowRow]:
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(self._dsn)
            # 同一可重复读只读事务：本任务所有查询共享同一快照
            await conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = await conn.fetch(
                """
                SELECT order_no, merchant_id, amount, fee
                FROM settlement_orders
                WHERE settle_date = $1
                ORDER BY order_no
                """,
                date.fromisoformat(settle_date),
            )
            await conn.execute("ROLLBACK")  # 只读事务，回滚即结束且无副作用
            return [
                FlowRow(
                    order_no=r["order_no"],
                    merchant_id=r["merchant_id"],
                    amount=r["amount"],
                    fee=r["fee"],
                )
                for r in rows
            ]
        finally:
            if conn is not None:
                try:
                    await asyncio.wait_for(conn.close(), timeout=5)
                except Exception:
                    try:
                        conn.terminate()
                    except Exception:
                        pass
