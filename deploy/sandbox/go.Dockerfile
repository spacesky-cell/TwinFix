# Go 项目的 Docker 沙箱镜像。
# 构建：docker build -f deploy/sandbox/go.Dockerfile -t twinfix-sandbox-go:1.25 deploy/sandbox
# 然后在 deploy/.env 设置 TWINFIX_SANDBOX_IMAGE_GO=twinfix-sandbox-go:1.25
FROM golang:1.25-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git findutils tar \
    && rm -rf /var/lib/apt/lists/*

# 只使用镜像内的 Go 工具链：go.mod 声明更新版本时不要尝试联网下载 toolchain。
# 官方镜像默认 GOPATH=/go 属主是 root，沙箱以 10001 运行会写不进去；缓存改到 /tmp 下。
ENV GOTOOLCHAIN=local \
    GOPATH=/tmp/gopath \
    GOCACHE=/tmp/gocache
