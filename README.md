# Mini-Skill-Runner
![演示页面](demo.png)
一个基于 FastAPI 的实时任务执行系统，通过 SSE (Server-Sent Events) 流式推送任务执行进度，支持子进程管理、超时控制、断线重连等功能。

## 功能特性

- **实时进度推送** - 通过 SSE 流式推送任务执行状态
- **断线重连** - 重连后自动补发历史事件
- **超时控制** - 任务超时后自动 Kill 子进程
- **孤儿任务取消** - 60 秒无客户端连接自动取消任务
- **多客户端踢除** - 新连接自动踢除旧连接
- **心跳保活** - 10 秒心跳机制维持连接
- **并发安全** - 使用 asyncio.Lock 保护共享状态

## 项目结构

```
Mini-Skill-Runner/
├── solution/
│   ├── main.py          # FastAPI 应用主程序
│   └── static/
│       └── index.html   # 演示前端页面
├── worker.py            # 示例 Worker 脚本
├── requirements.txt    # Python 依赖
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动服务

```bash
cd solution
uvicorn main:app --reload --port 8000
```

### 3. 访问演示页面

打开浏览器访问 http://localhost:8000/

## API 文档

启动服务后访问 http://localhost:8000/docs 查看交互式 API 文档。

### 主要接口

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/tasks` | 创建新任务 |
| GET | `/tasks/{task_id}/stream` | SSE 订阅任务进度 |
| GET | `/tasks/{task_id}/status` | 查询任务状态 |

### 创建任务

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"input": "hello world", "timeout": 30}'
```

响应：
```json
{"task_id": "a1b2c3d4", "status": "pending"}
```

### SSE 事件格式

```
event: progress
data: {"type": "log", "step": 1, "message": "正在校验输入..."}

event: done
data: {"status": "done", "output": "处理结果: HELLO WORLD"}
```

事件类型：`progress` | `done` | `heartbeat` | `error` | `kicked`

## 工作原理

1. 客户端调用 `POST /tasks` 创建任务（此时为 pending 状态）
2. 客户端调用 `GET /tasks/{task_id}/stream` 订阅 SSE
3. 订阅时触发执行，启动 `worker.py` 子进程
4. 子进程通过 stdout 输出 JSON Lines 格式的事件
5. 服务端解析事件并通过 SSE 推送给客户端
6. 任务完成、超时或被取消时连接关闭

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ORPHAN_TIMEOUT` | 60 秒 | 孤儿任务超时时间 |
| `HEARTBEAT_INTERVAL` | 10 秒 | 心跳间隔 |
| 任务 `timeout` | 30 秒 | 单个任务超时时间 |

## 开发自定义 Worker

参考 `worker.py` 的格式，通过 stdout 输出 JSON 事件：

```python
import json
import sys

def emit(event: dict):
    print(json.dumps(event, ensure_ascii=False), flush=True)

# 输出进度
emit({"type": "log", "step": 1, "message": "步骤1"})

# 输出结果
emit({"type": "result", "output": "处理结果"})
```
