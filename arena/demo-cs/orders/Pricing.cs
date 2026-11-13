namespace Shop.Orders;

public interface IPricing
{
    decimal DiscountFor(decimal subtotal, Coupon? coupon);
}

public sealed class PriceCalculator : IPricing
{
    public PriceBreakdown Calculate(IReadOnlyList<OrderLine> lines, Coupon? coupon = null)
    {
        var subtotal = lines.Sum(line => line.UnitPrice * line.Quantity);
        var discount = DiscountFor(subtotal, coupon);
        return new PriceBreakdown(
            subtotal,
            discount,
            discount > 0 && coupon is not null ? coupon.Code : null,
            subtotal - discount);
    }

    public decimal DiscountFor(decimal subtotal, Coupon? coupon)
    {
        if (coupon is null)
        {
            return 0;
        }

        var threshold = ThresholdOf(coupon);
        if (subtotal < threshold)
        {
            return 0;
        }

        return coupon.Kind == CouponKind.Amount
            ? Math.Min(coupon.Value, subtotal)
            : Math.Round(subtotal * coupon.Value / 100, MidpointRounding.AwayFromZero);
    }

    private decimal ThresholdOf(Coupon coupon)
    {
        // 满减券门槛按面值的 2 倍兜底，防止运营把门槛配成 0 导致无门槛立减
        return coupon.Kind == CouponKind.Amount
            ? Math.Max(coupon.Threshold ?? 0, coupon.Value * 2)
            : coupon.Threshold ?? 0;
    }
}
