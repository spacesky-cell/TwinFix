namespace Shop.Orders;

public sealed class Cart
{
    private readonly List<OrderLine> _lines = new();

    public Cart(string customer, IEnumerable<OrderLine>? lines = null)
    {
        Customer = customer;
        if (lines is not null)
        {
            _lines.AddRange(lines);
        }
    }

    public string Customer { get; }

    public IReadOnlyList<OrderLine> Lines => _lines;

    public void Add(OrderLine line)
    {
        _lines.Add(line);
    }

    public decimal Subtotal()
    {
        return _lines.Sum(line => line.UnitPrice * line.Quantity);
    }
}
