## 改动内容

<!-- 为什么改、改了什么、影响哪些模块 -->

## 验证

- [ ] `cd server && uv run ruff check . && uv run ruff format --check . && uv run mypy twinfix`
- [ ] `cd server && uv run pytest -q`
- [ ] `cd web && npm run build`
- [ ] 改了 `arena/`、demo 基线或任一 Profile 时：`cd server && uv run twinfix-arena validate`

<!-- 贴关键输出或说明为什么某项不适用 -->

## 安全与合规

- [ ] 没有提交 `.env`、API Key、token、私钥或真实用户数据
- [ ] 新增依赖/资产已确认许可证兼容（见 `THIRD_PARTY_NOTICES.md`）
- [ ] 涉及权限、沙箱、合并或 Prompt 边界时，已说明对 `docs/05-safety.md` 的影响
