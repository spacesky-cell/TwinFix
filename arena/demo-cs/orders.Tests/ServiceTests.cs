using Shop.Orders;

namespace Shop.Tests;

public static class ServiceTests
{
    private static OrderService Service()
    {
        return new OrderService(new Inventory());
    }

    public static void TestCreatesOrderAndReservesStock()
    {
        var inventory = new Inventory();
        var service = new OrderService(inventory);
        var order = service.Create("u-1", new[] { new OrderLine("SKU-1", 1, 10000m) });
        Check.Equal(10000m, order.Total);
        Check.Equal(499, inventory.Available("SKU-1"));
    }

    public static void TestFailedMultiLineOrderReleasesReservedLines()
    {
        var inventory = new Inventory();
        var service = new OrderService(inventory);
        Check.Throws<OutOfStockException>(() => service.Create(
            "u-1",
            new[]
            {
                new OrderLine("SKU-1", 2, 10000m),
                new OrderLine("SKU-3", 1, 10000m),
            }));
        Check.Equal(500, inventory.Available("SKU-1"));
    }

    public static void TestDoesNotLeakCouponIntoNextOrder()
    {
        var service = Service();
        var coupon = new Coupon("SAVE20", CouponKind.Amount, 2000m, 10000m);
        var first = service.Create("u-1", new[] { new OrderLine("SKU-1", 1, 10000m) }, coupon);
        Check.Equal(8000m, first.Total);
        var second = service.Create("u-2", new[] { new OrderLine("SKU-1", 1, 10000m) });
        Check.Equal(10000m, second.Total);
    }
}
