namespace Shop.Orders;

public sealed class OutOfStockException : Exception
{
    public OutOfStockException(string sku, int requested, int available)
        : base($"insufficient stock for {sku}: requested {requested}, available {available}")
    {
        Sku = sku;
        Requested = requested;
        Available = available;
    }

    public string Sku { get; }

    public int Requested { get; }

    public int Available { get; }
}

public sealed class Inventory
{
    private readonly Dictionary<string, int> _stock;

    public Inventory(IReadOnlyDictionary<string, int>? stock = null)
    {
        _stock = stock is null
            ? new Dictionary<string, int> { ["SKU-1"] = 500, ["SKU-2"] = 300, ["SKU-3"] = 0 }
            : new Dictionary<string, int>(stock);
    }

    public int Available(string sku)
    {
        return _stock.TryGetValue(sku, out var available) ? available : 0;
    }

    public void Reserve(string sku, int quantity)
    {
        if (quantity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(quantity), "quantity must be positive");
        }

        var available = Available(sku);
        if (available < quantity)
        {
            throw new OutOfStockException(sku, quantity, available);
        }

        _stock[sku] = available - quantity;
    }

    public void Release(string sku, int quantity)
    {
        _stock[sku] = Available(sku) + quantity;
    }
}
