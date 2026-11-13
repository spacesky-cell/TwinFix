namespace Shop.Orders;

public enum CouponKind
{
    Amount,
    Percent,
}

public sealed record Coupon(string Code, CouponKind Kind, decimal Value, decimal? Threshold = null);

public sealed record OrderLine(string Sku, int Quantity, decimal UnitPrice);

public sealed record PriceBreakdown(decimal Subtotal, decimal Discount, string? AppliedCoupon, decimal Total);

public sealed record Order(string Id, string Customer, IReadOnlyList<OrderLine> Lines, decimal Total);
