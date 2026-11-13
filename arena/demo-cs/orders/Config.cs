namespace Shop.Orders;

public static class Config
{
    public static int Port()
    {
        var raw = Environment.GetEnvironmentVariable("PORT");
        return int.TryParse(raw, out var port) ? port : 8080;
    }

    public static string InventoryToken()
    {
        var token = Environment.GetEnvironmentVariable("ORDERS_INVENTORY_TOKEN");
        if (string.IsNullOrEmpty(token))
        {
            // 服务间令牌：本地默认值只用于开发环境，生产由部署配置下发
            return "dev-inventory-token";
        }

        return token;
    }
}
