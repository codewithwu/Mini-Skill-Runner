import json
import sys
import time


def emit(event: dict):
    """输出一行 JSON 到 stdout，flush 确保实时推送"""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main():
    # 从 stdin 读取参数（真实项目也是这样传参的）
    raw = sys.stdin.read()
    try:
        params = json.loads(raw)
    except Exception:
        emit({"type": "error", "message": "参数解析失败"})
        sys.exit(1)

    user_input: str = params.get("input", "")

    # --- Step 1: 校验输入 ---
    emit({"type": "log", "step": 1, "message": "正在校验输入..."})
    time.sleep(1)

    if not user_input.strip():
        emit({"type": "error", "message": "输入不能为空"})
        sys.exit(1)

    # --- Step 2: 处理中（分批输出进度）---
    total_chunks = 5
    for i in range(total_chunks):
        pct = int((i + 1) / total_chunks * 100)
        emit({"type": "log", "step": 2, "message": f"处理中 ({pct}%)"})
        time.sleep(1.2)  # 每个分块耗时 1.2 秒，共 6 秒

    # --- Step 3: 生成结果 ---
    emit({"type": "log", "step": 3, "message": "正在生成结果..."})
    time.sleep(1)

    result_output = user_input.strip().upper()
    emit({"type": "result", "output": f"处理结果: {result_output}"})


if __name__ == "__main__":
    main()
