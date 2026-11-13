# TypeScript / JavaScript 项目的 Docker 沙箱镜像。
# 构建：docker build -f deploy/sandbox/typescript.Dockerfile -t twinfix-sandbox-typescript:22 deploy/sandbox
# 然后在 deploy/.env 设置 TWINFIX_SANDBOX_IMAGE_TYPESCRIPT=twinfix-sandbox-typescript:22
#
# Node 22+ 自带类型剥离：`.ts` 可以直接 node 执行 / node --check / node --test，
# 不要求项目预先 npm install，也不需要 tsc 或 jest。需要真实编译链的项目可派生镜像。
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git findutils tar \
    && rm -rf /var/lib/apt/lists/*

ENV NODE_ENV=development \
    NPM_CONFIG_UPDATE_NOTIFIER=false
