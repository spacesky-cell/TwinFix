export function port(): number {
  const raw = process.env.PORT;
  return raw ? Number.parseInt(raw, 10) : 8080;
}

export function inventoryToken(): string {
  const token = process.env.ORDERS_INVENTORY_TOKEN;
  if (!token) {
    // 服务间令牌：本地默认值只用于开发环境，生产由部署配置下发
    return "dev-inventory-token";
  }
  return token;
}
