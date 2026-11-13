# 评测判分（控制台发起的全量评测也在 API 容器里跑）需要 JDK + Maven + Go + Node + .NET；
# demo-shop 编译目标是 Java 21，Go/TypeScript/C# 用例分别用 go test / node --test / dotnet run。
# 直接借官方镜像的工具链，避免 Debian 默认版本不够。
FROM maven:3.9.9-eclipse-temurin-21 AS maven
FROM golang:1.25-bookworm AS go
FROM node:22-bookworm-slim AS node
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS dotnet

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    JAVA_HOME=/opt/java/openjdk \
    GOTOOLCHAIN=local \
    GOCACHE=/tmp/gocache \
    GOPATH=/tmp/gopath \
    DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_HOME=/tmp/dotnet-home \
    NUGET_PACKAGES=/tmp/nuget \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    PATH="/usr/local/go/bin:/opt/java/openjdk/bin:/usr/share/maven/bin:/usr/share/dotnet:/app/.venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
# 三个镜像同为 Debian bookworm，直接拷 JDK、Maven 与 Go，省掉再装一套 apt 包
COPY --from=maven /opt/java/openjdk /opt/java/openjdk
COPY --from=maven /usr/share/maven /usr/share/maven
COPY --from=go /usr/local/go /usr/local/go
# TypeScript 判分用：Node 22 自带类型剥离，node --test 不需要 npm install
COPY --from=node /usr/local/bin/node /usr/local/bin/node
# C# 判分用：拷贝 .NET SDK（Ubuntu 24.04 构建，Debian trixie 的 glibc 兼容）；
# InvariantGlobalization 避免 python:slim 缺 ICU，零 NuGet 依赖项目可直接离线 build。
COPY --from=dotnet /usr/share/dotnet /usr/share/dotnet
RUN ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet
WORKDIR /app

# 先只装依赖，代码变更不使依赖层失效
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project --extra eval

# local 代码托管与 Arena 注入通过 git 子进程操作裸仓库；docker CLI + compose
# 供 worker 为每个事故拉起孪生环境（通过挂载的 /var/run/docker.sock 连接宿主机 daemon）
ARG DOCKER_CLI_VERSION=28.5.1
ARG COMPOSE_VERSION=2.40.3
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY twinfix ./twinfix
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra eval

RUN useradd --system --uid 10001 --home /app twinfix \
    && mkdir -p /data /app/.m2 && chown twinfix /data /app/.m2
USER twinfix
EXPOSE 8000
CMD ["uvicorn", "twinfix.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
