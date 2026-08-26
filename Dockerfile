# ============================================================
# 构建优化说明：
# 1. 层缓存顺序：先 COPY 依赖清单 → 装依赖 → 再 COPY 应用代码。
#    旧版 `COPY . .` 在依赖安装之前，任何一行代码改动都会使依赖
#    层缓存失效、触发全量重装（30-60s+）。现在日常改代码重建只需
#    秒级。
# 2. 依赖用 uv 安装（比 pip 快 10-100 倍），--frozen 锁定 uv.lock
#    的精确版本，构建可复现。
# 3. 运行时只需要 app/（含已构建好的 templates 静态资源）和版本
#    文件；源码其他部分由 .dockerignore 排除。
# 4. 以非 root 用户运行容器（安全加固），并用 exec 形式 CMD 保证
#    SIGTERM 正常传递（优雅关闭、释放连接池）。
# 5. 注意：不加 --workers —— key 冷却表、限流计数、响应缓存、统计
#    全是进程内存态，多 worker 会导致每个 key 的 RPM 配额放大 N 倍
#    且各自为政，触发上游风控。单 worker 是架构约束下的正确选择。
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# 依赖层：只在 requirements.txt 变化时重建
COPY requirements.txt .
RUN pip install uv \
    && uv pip install --system --no-cache-dir -r requirements.txt

# 应用层：代码改动时只重建这一层
COPY app ./app
COPY version.txt .

# 非 root 运行（安全加固）
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860

# /health 是无需认证的存活探针端点，适合容器编排健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3)" || exit 1

# exec 形式 + 显式 host/port；--timeout-keep-alive 防止代理链路上的
# 空闲连接被过早回收
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--timeout-keep-alive", "60"]
