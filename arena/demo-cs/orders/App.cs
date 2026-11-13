using System.Net;
using System.Text;
using System.Text.Json;
using Shop.Orders;

var port = Config.Port();

if (args.Contains("--healthcheck"))
{
    using var probe = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
    try
    {
        var response = await probe.GetAsync($"http://127.0.0.1:{port}/actuator/health");
        return response.IsSuccessStatusCode ? 0 : 1;
    }
    catch
    {
        return 1;
    }
}

var inventory = new Inventory();
var service = new OrderService(inventory);
_ = Config.InventoryToken(); // 启动时校验必需配置：缺失时应让进程失败

var listener = new HttpListener();
listener.Prefixes.Add($"http://*:{port}/");
listener.Start();
Console.WriteLine($"orders listening on :{port}");

while (listener.IsListening)
{
    var context = await listener.GetContextAsync();
    _ = Task.Run(() => Handle(context));
}

return 0;

async Task Handle(HttpListenerContext context)
{
    var request = context.Request;
    var response = context.Response;
    try
    {
        if (request.HttpMethod == "GET" &&
            (request.Url?.AbsolutePath == "/health" || request.Url?.AbsolutePath == "/actuator/health"))
        {
            await Respond(response, 200, new { status = "UP" });
            return;
        }

        if (request.HttpMethod == "GET" && request.Url?.AbsolutePath.StartsWith("/api/stock/", StringComparison.Ordinal) == true)
        {
            var sku = Uri.UnescapeDataString(request.Url.AbsolutePath["/api/stock/".Length..]);
            await Respond(response, 200, new { sku, available = inventory.Available(sku) });
            return;
        }

        if (request.HttpMethod == "POST" && request.Url?.AbsolutePath == "/api/orders")
        {
            using var reader = new StreamReader(request.InputStream, request.ContentEncoding ?? Encoding.UTF8);
            var text = await reader.ReadToEndAsync();
            using var document = JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text);
            var root = document.RootElement;
            var customer = root.TryGetProperty("customer", out var rawCustomer) && rawCustomer.ValueKind == JsonValueKind.String
                ? rawCustomer.GetString() ?? "anonymous"
                : "anonymous";
            var lines = root.TryGetProperty("lines", out var rawLines) && rawLines.ValueKind == JsonValueKind.Array
                ? rawLines.EnumerateArray().Select(ParseLine).ToList()
                : new List<OrderLine>();
            var coupon = root.TryGetProperty("coupon", out var rawCoupon) ? ParseCoupon(rawCoupon) : null;
            var order = service.Create(customer, lines, coupon);
            await Respond(
                response,
                201,
                new
                {
                    id = order.Id,
                    customer = order.Customer,
                    total = order.Total,
                    lines = order.Lines.Select(line => new { sku = line.Sku, quantity = line.Quantity }),
                });
            return;
        }

        await Respond(response, 404, new { error = "not found" });
    }
    catch (OutOfStockException error)
    {
        await Respond(response, 409, new { code = "OUT_OF_STOCK", message = error.Message });
    }
    catch (Exception error) when (error is JsonException or FormatException or ArgumentException or InvalidOperationException)
    {
        await Respond(response, 400, new { code = "BAD_REQUEST", message = error.Message });
    }
    catch (Exception error)
    {
        await Respond(response, 400, new { code = "BAD_REQUEST", message = error.Message });
    }
    finally
    {
        Console.WriteLine($"{request.HttpMethod} {request.Url?.AbsolutePath} -> {response.StatusCode}");
    }
}

static async Task Respond(HttpListenerResponse response, int status, object payload)
{
    var data = JsonSerializer.Serialize(payload);
    var bytes = Encoding.UTF8.GetBytes(data);
    response.StatusCode = status;
    response.ContentType = "application/json; charset=utf-8";
    response.ContentLength64 = bytes.Length;
    await response.OutputStream.WriteAsync(bytes);
    response.Close();
}

static OrderLine ParseLine(JsonElement raw)
{
    return new OrderLine(
        raw.TryGetProperty("sku", out var sku) ? sku.GetString() ?? "" : "",
        raw.TryGetProperty("quantity", out var quantity) ? quantity.GetInt32() : 0,
        raw.TryGetProperty("unitPrice", out var unitPrice) ? unitPrice.GetDecimal() : 0);
}

static Coupon? ParseCoupon(JsonElement raw)
{
    if (raw.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
    {
        return null;
    }

    var kind = raw.TryGetProperty("kind", out var rawKind) && rawKind.GetString() == "percent"
        ? CouponKind.Percent
        : CouponKind.Amount;
    decimal? threshold = raw.TryGetProperty("threshold", out var rawThreshold) && rawThreshold.ValueKind != JsonValueKind.Null
        ? rawThreshold.GetDecimal()
        : null;
    return new Coupon(
        raw.TryGetProperty("code", out var code) ? code.GetString() ?? "" : "",
        kind,
        raw.TryGetProperty("value", out var value) ? value.GetDecimal() : 0,
        threshold);
}
