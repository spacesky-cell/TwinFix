using Shop.Orders;

namespace Shop.Tests;

public static class PricingHiddenTests
{
    private static readonly PriceCalculator Calc = new();

    private static IReadOnlyList<OrderLine> Lines(decimal unitPrice, int quantity = 1)
    {
        return new[] { new OrderLine("SKU-X", quantity, unitPrice) };
    }

    public static void TestNoCouponPricedAtSubtotal()
    {
        var breakdown = Calc.Calculate(Lines(17980m));
        Check.Equal(17980m, breakdown.Total);
        Check.Equal(0m, breakdown.Discount);
        Check.Null(breakdown.AppliedCoupon);
    }

    public static void TestAmountCouponAppliesAtConfiguredThreshold()
    {
        var coupon = new Coupon("SAVE20", CouponKind.Amount, 2000m, 10000m);
        var breakdown = Calc.Calculate(Lines(10000m), coupon);
        Check.Equal(8000m, breakdown.Total);
        Check.Equal("SAVE20", breakdown.AppliedCoupon);
    }

    public static void TestAmountCouponRejectedBelowThreshold()
    {
        var coupon = new Coupon("SAVE20", CouponKind.Amount, 2000m, 10000m);
        var breakdown = Calc.Calculate(Lines(9999m), coupon);
        Check.Equal(9999m, breakdown.Total);
        Check.Null(breakdown.AppliedCoupon);
    }

    public static void TestZeroThresholdAmountCouponStillGuarded()
    {
        var coupon = new Coupon("ZERO", CouponKind.Amount, 2000m, 0m);
        Check.Null(Calc.Calculate(Lines(3999m), coupon).AppliedCoupon);
        Check.Equal(2000m, Calc.Calculate(Lines(4000m), coupon).Total);
    }

    public static void TestPercentCouponUnaffected()
    {
        var coupon = new Coupon("OFF10", CouponKind.Percent, 10m);
        var breakdown = Calc.Calculate(Lines(5000m), coupon);
        Check.Equal(4500m, breakdown.Total);
        Check.Equal("OFF10", breakdown.AppliedCoupon);
    }
}
