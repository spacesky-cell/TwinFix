namespace Shop.Orders;

public sealed class OrderService
{
    private readonly Inventory _inventory;
    private readonly PriceCalculator _calculator;
    private readonly Dictionary<string, Order> _orders = new();
    private int _seq;

    public OrderService(Inventory inventory, PriceCalculator? calculator = null)
    {
        _inventory = inventory;
        _calculator = calculator ?? new PriceCalculator();
    }

    public Order Create(string customer, IReadOnlyList<OrderLine> lines, Coupon? coupon = null)
    {
        if (lines.Count == 0)
        {
            throw new ArgumentException("order needs at least one line", nameof(lines));
        }

        var cart = new Cart(customer);
        try
        {
            foreach (var line in lines)
            {
                _inventory.Reserve(line.Sku, line.Quantity);
                cart.Add(line);
            }
        }
        catch
        {
            foreach (var line in cart.Lines)
            {
                _inventory.Release(line.Sku, line.Quantity);
            }

            throw;
        }

        var breakdown = _calculator.Calculate(cart.Lines, coupon);
        _seq += 1;
        var order = new Order($"o-{_seq}", customer, cart.Lines.ToList(), breakdown.Total);
        _orders[order.Id] = order;
        return order;
    }

    public Order? Get(string orderId)
    {
        return _orders.TryGetValue(orderId, out var order) ? order : null;
    }
}
