"""FastAPI 入口：网页 + 渠道流水核对 API + 对照批次 API + 补记 API。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import asyncpg
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from plans import structure_lines
from recon import ReconStore, ReconTask
from reconrun import ReconRunner
from runner import BatchRunner
from sqlcheck import validate_select_sql
from store import Batch, BatchStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/settlement"
)
MAX_CSV_BYTES = 10 * 1024 * 1024  # 上传 CSV 大小上限 10MB

store = BatchStore(capacity=50)
runner = BatchRunner(DATABASE_URL, store)
recon_store = ReconStore(task_capacity=100)
recon_runner = ReconRunner(DATABASE_URL, recon_store)
pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    last_exc: Exception | None = None
    for _ in range(60):  # 等数据库就绪（compose 里还有 healthcheck 兜底）
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            break
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(1)
    if pool is None:
        raise RuntimeError(f"无法连接数据库: {last_exc}")
    yield
    await pool.close()


app = FastAPI(title="结算对账与计划对照台", lifespan=lifespan)


# ---------------- 渠道流水每日核对 ----------------

@app.post("/api/recon/tasks", status_code=201)
async def create_recon_task(
    settle_date: str = Form(...), file: UploadFile = File(...)
):
    """上传某结算日的渠道流水 CSV，后台核对并（按提交序号）发布报告。"""
    try:
        day = date.fromisoformat(settle_date.strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="结算日格式应为 YYYY-MM-DD")
    data = await file.read(MAX_CSV_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > MAX_CSV_BYTES:
        raise HTTPException(status_code=400, detail="CSV 超过 10MB 上限")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件需为 UTF-8 编码")
    task = ReconTask(
        settle_date=day.isoformat(),
        filename=file.filename or "upload.csv",
        seq=recon_store.next_seq(),
    )
    recon_store.add_task(task)
    recon_runner.start(task, text)
    return task.to_dict()


@app.get("/api/recon/tasks")
async def list_recon_tasks():
    return {"tasks": [t.to_dict() for t in recon_store.list_tasks()]}


@app.get("/api/recon/reports")
async def list_recon_reports():
    """每个结算日的当前已发布报告（摘要）+ 该日最新任务状态（用于提示未发布原因）。"""
    reports = []
    for rep in recon_store.list_reports():
        latest = recon_store.latest_task_for(rep.settle_date)
        reports.append(
            {
                "settle_date": rep.settle_date,
                "task_id": rep.task_id,
                "seq": rep.seq,
                "published_at": rep.published_at,
                "summary": rep.summary,
                "latest_task": latest.to_dict() if latest else None,
            }
        )
    return {"reports": reports}


@app.get("/api/recon/reports/{settle_date}")
async def get_recon_report(settle_date: str):
    report = recon_store.get_report(settle_date)
    if report is None:
        raise HTTPException(status_code=404, detail="该结算日暂无已发布报告")
    return report.to_dict()


class OrderIn(BaseModel):
    order_no: str = Field(min_length=1, max_length=64)
    merchant_id: int
    amount: Decimal
    fee: Decimal
    settle_date: date


@app.post("/api/orders", status_code=201)
async def create_order(body: OrderIn):
    """补记一笔结算订单；进行中的核对事务使用同一快照，看不到这笔新单。"""
    if pool is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
                VALUES ($1, $2, $3, $4, 'SETTLED', $5)
                RETURNING order_id, order_no, merchant_id, amount, fee, status, settle_date
                """,
                body.order_no.strip(),
                body.merchant_id,
                body.amount,
                body.fee,
                body.settle_date,
            )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(status_code=400, detail=f"商户 {body.merchant_id} 不存在")
    return {"ok": True, "row": _jsonable(dict(row))}


# ---------------- SQL 计划对照（保留原功能） ----------------

class BatchIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


@app.post("/api/batches")
async def create_batch(body: BatchIn):
    try:
        sql = validate_select_sql(body.sql)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    batch = Batch(sql=sql)
    if not runner.try_start(batch):
        return JSONResponse(
            status_code=409,
            content={"detail": "已有对照批次在运行，当前繁忙，请稍后再试", "busy": True},
        )
    store.add(batch)
    return JSONResponse(status_code=201, content=batch.to_dict())


@app.get("/api/batches")
async def list_batches():
    return {
        "busy": runner.busy,
        "current_batch_id": runner.current_batch_id,
        "batches": [b.to_dict() for b in store.list()],
    }


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    data = batch.to_dict(include_plans=True)
    for ex, ex_dict in zip(batch.executions, data["executions"]):
        if ex.plan_structure is not None:
            ex_dict["structure_lines"] = structure_lines(ex.plan_structure)
    return data


@app.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    found, pid = runner.request_cancel(batch_id)
    if not found:
        return {"ok": False, "detail": "批次未在运行，无法取消"}
    if pid is not None and pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT pg_cancel_backend($1)", pid)
        except Exception:
            pass  # 取消请求失败不阻断，worker 侧还有标记位和兜底超时
    return {"ok": True}


@app.post("/api/sample-row")
async def add_sample_row():
    """补记一行结算样表数据；对照运行期间也可写入（对照事务用同一快照，看不到新行）。"""
    if pool is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH m AS (
                SELECT 1 + (random() * 4999)::int              AS merchant_id,
                       round((1 + random() * 499)::numeric, 2) AS amount
            )
            INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
            SELECT 'SO-M-' || upper(substr(md5(random()::text), 1, 10)),
                   merchant_id, amount, round(amount * 0.006, 2), 'SETTLED', CURRENT_DATE
            FROM m
            RETURNING order_id, order_no, merchant_id, amount, fee, status, settle_date
            """
        )
    return {"ok": True, "row": _jsonable(dict(row))}


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    return value


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
