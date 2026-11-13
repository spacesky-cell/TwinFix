# C# / .NET 项目的 Docker 沙箱镜像。
# 构建：docker build -f deploy/sandbox/csharp.Dockerfile -t twinfix-sandbox-csharp:10.0 deploy/sandbox
# 然后在 deploy/.env 设置 TWINFIX_SANDBOX_IMAGE_CSHARP=twinfix-sandbox-csharp:10.0
#
# 只带 .NET SDK：demo-cs 这类零 NuGet 依赖的项目可直接 dotnet build / dotnet run 测试工程。
# 需要 xUnit/NUnit 等包的项目可派生镜像并预热 NuGet 缓存。
FROM mcr.microsoft.com/dotnet/sdk:10.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git findutils tar \
    && rm -rf /var/lib/apt/lists/*

# 沙箱以非 root 用户运行，HOME 指向可写的 /tmp/home（由 DockerSandbox 注入）；
# NuGet 缓存也放 /tmp，避免写 /root/.nuget。
ENV DOTNET_CLI_HOME=/tmp/dotnet-home \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_PACKAGES=/tmp/nuget
