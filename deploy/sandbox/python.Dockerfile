# Python 项目的 Docker 沙箱镜像。
# 构建：docker build -f deploy/sandbox/python.Dockerfile -t twinfix-sandbox-python:3.12 deploy/sandbox
# 然后在 deploy/.env 设置 TWINFIX_SANDBOX_IMAGE=twinfix-sandbox-python:3.12
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git findutils tar \
    && rm -rf /var/lib/apt/lists/*

# 固定版本便于复现。项目自己的依赖建议预装进派生镜像（或在允许沙箱出网时自行安装），
# TwinFix 默认只执行 compileall / pytest，不会替项目改依赖。
RUN pip install --no-cache-dir pytest==9.1.1
