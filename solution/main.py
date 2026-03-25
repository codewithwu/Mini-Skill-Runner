import asyncio
import json
import os
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Mini Skill Runner")

# ── 工作脚本路径（不要修改）──────────────────────────────────────────────────
WORKER_SCRIPT = Path(__file__).parent.parent / "worker.py"

# ── 孤儿任务超时（秒）：超过此时间无客户端连接则自动取消 ─────────────────────
ORPHAN_TIMEOUT = 60

# ── 心跳间隔（秒）──────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL = 10


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构（可自由修改 / 扩展）
# ─────────────────────────────────────────────────────────────────────────────

class TaskInfo:
    """单个任务的运行时状态"""

    def __init__(self, task_id: str, input_data: str, timeout: int):
        self.task_id = task_id
        self.input_data = input_data
        self.timeout = timeout
        self.status = "pending"          # pending | running | done | failed | cancelled
        self.created_at = datetime.utcnow()
        self.events: list[dict] = []     # 历史事件（用于断线重连）
        self.last_connection_time = datetime.utcnow()  # 上次有客户端连接的时间
        self.queue: asyncio.Queue = asyncio.Queue()    # 事件通知队列
        self.process: Optional[asyncio.subprocess.Process] = None  # 子进程引用
        self._cancelled = False          # 是否已被取消
        # 多客户端踢除机制
        self._connection_lock = asyncio.Lock()  # 保护连接状态的锁
        self._active_conn_id: Optional[str] = None  # 当前活跃连接的 ID
        self._kicked = False             # 标记当前连接是否已被踢除

    async def add_event(self, event: dict) -> None:
        """添加事件到历史记录，并通知等待中的订阅者"""
        self.events.append(event)
        await self.queue.put(event)

    async def try_acquire_connection(self, conn_id: str) -> bool:
        """
        尝试获取连接权限。
        返回 True 表示获取成功（成为活跃连接），False 表示被踢除。
        """
        async with self._connection_lock:
            if self._active_conn_id is not None and self._active_conn_id != conn_id:
                # 已有其他活跃连接，标记旧连接将被踢除
                self._kicked = True
                return False
            self._active_conn_id = conn_id
            self._kicked = False
            return True

    def is_kicked(self) -> bool:
        """检查当前连接是否已被踢除"""
        return self._kicked

    async def try_acquire_connection(self, conn_id: str) -> bool:
        """
        尝试获取连接权限。
        返回 True 表示获取成功（成为活跃连接），False 表示被踢除。
        """
        async with self._connection_lock:
            if self._active_conn_id is not None and self._active_conn_id != conn_id:
                # 已有其他活跃连接，标记旧连接将被踢除
                self._kicked = True
                return False
            self._active_conn_id = conn_id
            self._kicked = False
            return True

    def release_connection(self, conn_id: str) -> None:
        """释放连接权限"""
        if self._active_conn_id == conn_id:
            self._active_conn_id = None


# 全局任务注册表
# key: task_id (str), value: TaskInfo
_tasks: dict[str, TaskInfo] = {}

# 保护任务注册表的并发写（加分项1：使用 asyncio.Lock）
_tasks_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# 请求 / 响应模型
# ─────────────────────────────────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    input: str
    timeout: int = 30  # 秒，超时后强制终止子进程


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    created_at: datetime
    events_count: int


# ─────────────────────────────────────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/tasks", response_model=CreateTaskResponse)
async def create_task(req: CreateTaskRequest):
    """
    创建并注册一个新任务。
    任务此时处于 pending 状态，不立即执行。
    执行在客户端第一次订阅 SSE 时触发。
    """
    task_id = str(uuid.uuid4())[:8]

    async with _tasks_lock:
        task = TaskInfo(task_id=task_id, input_data=req.input, timeout=req.timeout)
        _tasks[task_id] = task

    # 启动孤儿超时监控（后台 asyncio 任务）
    asyncio.create_task(watch_orphan(task))

    return CreateTaskResponse(task_id=task_id, status="pending")


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    """
    订阅任务进度（SSE）。

    行为：
    1. task_id 不存在 → HTTP 404
    2. 任务 pending → 触发执行，开始推送事件
    3. 任务 running → 推送历史事件（补发），然后实时推送后续事件
    4. 任务 done/failed → 推送所有历史事件后关闭连接
    5. 如果有其他连接已在监听，踢掉旧连接（发送 kicked 事件后关闭）

    SSE 事件格式：
        event: progress\ndata: {...}\n\n
        event: done\ndata: {...}\n\n
        event: heartbeat\ndata: {}\n\n
        event: error\ndata: {...}\n\n
        event: kicked\ndata: {"reason": "new connection"}\n\n
    """
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    task = _tasks[task_id]

    # 生成唯一连接 ID
    conn_id = str(uuid.uuid4())

    # 尝试获取连接权限（检查是否需要踢除旧连接）
    if not await task.try_acquire_connection(conn_id):
        # 存在活跃连接，新连接被踢除
        async def kicked_generate():
            yield sse_event("kicked", {"reason": "new connection"})
        return StreamingResponse(kicked_generate(), media_type="text/event-stream")

    # 更新最后连接时间
    task.last_connection_time = datetime.utcnow()

    async def generate() -> AsyncGenerator[str, None]:
        nonlocal task

        try:
            # 发送初始连接成功事件
            yield sse_event("connected", {"task_id": task_id})

            # 检查是否已被踢除
            if task.is_kicked():
                yield sse_event("kicked", {"reason": "new connection"})
                return

            # 如果是第一次连接，触发执行
            should_start = False
            if task.status == "pending":
                should_start = True

            # 推送历史事件（断线重连时补发已有 events）
            for event in task.events:
                # 检查是否已被踢除
                if task.is_kicked():
                    yield sse_event("kicked", {"reason": "new connection"})
                    return
                yield sse_event("progress", event)

            # 如果任务已完成，发送 done 事件后关闭
            if task.status in ("done", "failed", "cancelled"):
                yield sse_event("done", {"status": task.status})
                return

            # 触发执行（如果是 pending）
            if should_start:
                task.status = "running"
                asyncio.create_task(run_worker(task))

            # 实时等待新事件并推送
            last_heartbeat = datetime.utcnow()
            while True:
                # 检查是否已被踢除
                if task.is_kicked():
                    yield sse_event("kicked", {"reason": "new connection"})
                    return

                try:
                    # 等待新事件，最多等待心跳间隔时间
                    event = await asyncio.wait_for(
                        task.queue.get(),
                        timeout=HEARTBEAT_INTERVAL
                    )
                    task.last_connection_time = datetime.utcnow()

                    # 检查是否已被踢除（在处理事件前）
                    if task.is_kicked():
                        yield sse_event("kicked", {"reason": "new connection"})
                        return

                    yield sse_event("progress", event)

                    # 如果是 result 类型事件，任务完成
                    if event.get("type") == "result":
                        task.status = "done"
                        yield sse_event("done", {"status": "done", "output": event.get("output", "")})
                        break

                except asyncio.TimeoutError:
                    # 发送心跳
                    yield sse_event("heartbeat", {})
                    last_heartbeat = datetime.utcnow()

                # 检查任务是否已被取消或失败
                if task.status in ("failed", "cancelled"):
                    yield sse_event("done", {"status": task.status})
                    break
        finally:
            # 释放连接权限
            task.release_connection(conn_id)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/tasks/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """查询任务状态"""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    task = _tasks[task_id]
    return TaskStatusResponse(
        task_id=task_id,
        status=task.status,
        created_at=task.created_at,
        events_count=len(task.events)
    )


# ─────────────────────────────────────────────────────────────────────────────
# 核心逻辑（需要实现）
# ─────────────────────────────────────────────────────────────────────────────

async def run_worker(task: TaskInfo) -> None:
    """
    启动 worker.py 子进程，逐行读取 stdout，解析 JSON Lines，
    将事件追加到 task.events 并通知所有等待的订阅者。

    关键要求：
    - 使用 asyncio.create_subprocess_exec 启动子进程
    - 通过 stdin 传入 {"input": task.input_data}
    - 逐行读取 stdout（不要用 communicate()，那样无法流式推送）
    - 超时（task.timeout 秒）后必须彻底 kill 子进程（包括子进程组）
    - 子进程正常退出 → task.status = "done"
    - 子进程异常退出 → task.status = "failed"
    - 超时 → task.status = "cancelled"
    - 任何情况下最后都要 await proc.wait()，避免僵尸进程
    """
    proc = None
    start_time = datetime.utcnow()

    def is_timedout():
        return (datetime.utcnow() - start_time).total_seconds() >= task.timeout

    def kill_process():
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task.process = proc

        # 发送参数
        input_data = json.dumps({"input": task.input_data})
        proc.stdin.write(input_data.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # 读取输出，循环检查超时
        while not is_timedout():
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=0.5
                )
                if not line:
                    break  # EOF
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    await task.add_event(event)
                    if event.get("type") in ("result", "error"):
                        break  # 完成
                except json.JSONDecodeError:
                    pass
            except asyncio.TimeoutError:
                continue

        # 检查是否超时
        if is_timedout():
            task.status = "cancelled"
            kill_process()
            await proc.wait()
            return

        # 等待进程结束
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            kill_process()
            await proc.wait()

        # 设置状态
        if proc.returncode == 0:
            task.status = "done"
        else:
            task.status = "failed"

    except Exception as e:
        task.status = "failed"
        try:
            await task.add_event({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if task.process is not None:
            try:
                await task.process.wait()
            except Exception:
                pass
            task.process = None


async def watch_orphan(task: TaskInfo) -> None:
    """
    孤儿任务监控：每隔一段时间检查任务是否仍有活跃连接。
    如果任务在 ORPHAN_TIMEOUT 秒内没有任何连接且仍在运行，自动取消。
    """
    while True:
        await asyncio.sleep(5)  # 每 5 秒检查一次

        if task._cancelled or task.status in ("done", "failed", "cancelled"):
            break

        # 检查是否超过孤儿超时
        elapsed = (datetime.utcnow() - task.last_connection_time).total_seconds()
        if elapsed >= ORPHAN_TIMEOUT and task.status == "running":
            task._cancelled = True
            task.status = "cancelled"

            # 终止子进程
            if task.process is not None:
                try:
                    os.killpg(os.getpgid(task.process.pid), signal.SIGKILL)
                    await task.process.wait()
                except Exception:
                    pass
                task.process = None

            # 添加取消事件
            await task.add_event({"type": "error", "message": "任务因超时无响应已被取消"})


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数（可选，建议实现以提高代码可读性）
# ─────────────────────────────────────────────────────────────────────────────

def sse_event(event_type: str, data: dict) -> str:
    """
    格式化一条 SSE 消息。

    示例输出：
        event: progress
        data: {"type": "log", "step": 1, "message": "正在校验输入..."}

        （注意末尾有两个换行）
    """
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# 静态文件服务（用于演示前端页面）
# ─────────────────────────────────────────────────────────────────────────────

from fastapi.staticfiles import StaticFiles
from pathlib import Path

# 提供静态文件访问
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """返回演示页面"""
    from fastapi.responses import FileResponse
    static_dir = Path(__file__).parent / "static"
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "Mini Skill Runner API", "docs": "/docs"}