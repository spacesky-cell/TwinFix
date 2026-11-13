from __future__ import annotations

import os

PORT = int(os.environ.get("PORT", "8080"))
# 服务间令牌：本地默认值只用于开发环境，生产由部署配置下发
INVENTORY_TOKEN = os.environ.get("ORDERS_INVENTORY_TOKEN", "dev-inventory-token")
