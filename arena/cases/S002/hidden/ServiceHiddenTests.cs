using Shop.Orders;

namespace Shop.Tests;

public static class ServiceHiddenTests
{
    private static OrderService Service()
    {
        return new OrderService(new Inventory());
    }

    private static OrderLine Line(decimal unitPrice)
    {
        return new OrderLine("SKU-1", 1, unitPrice);
    }

    public static void TestSecondOrderWithoutCouponUsesSubtotal()
    {
        var service = Service();
        var coupon = new Coupon("SAVE20", CouponKind.Amount, 2000m, 10000m);
        var first = service.Create("u-1", new[] { Line(10000m) }, coupon);
        Check.Equal(8000m, first.Total);
        var second = service.Create("u-2", new[] { Line(10000m) });
        Check.Equal(10000m, second.Total);
        var third = service.Create("u-3", new[] { Line(10000m) });
        Check.Equal(10000m, third.Total);
    }

    public static void TestFirstOrderWithoutCouponUnaffected()
    {
        var service = Service();
        var order = service.Create("u-1", new[] { Line(10000m) });
        Check.Equal(10000m, order.Total);
    }

    public static void TestPercentCouponStillAppliesAfterAmountCoupon()
    {
        var service = Service();
        var amount = new Coupon("SAVE20", CouponKind.Amount, 2000m, 10000m);
        Check.Equal(8000m, service.Create("u-1", new[] { Line(10000m) }, amount).Total);
        var percent = new Coupon("OFF10", CouponKind.Percent, 10m);
        Check.Equal(9000m, service.Create("u-2", new[] { Line(10000m) }, percent).Total);
    }
}
