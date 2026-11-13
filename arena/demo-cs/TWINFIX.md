# demo-cs

Project notes for TwinFix agents.

## Modules

| Module | Responsibility | Key types |
|---|---|---|
| `orders` | Create orders, price calculation, coupons, stock reservation | `OrderService`, `PriceCalculator`, `Cart`, `Inventory` |
| `orders.Tests` | Minimal reflection test harness (`Test*` static methods) | `TestHarness`, `Check` |

The service is a .NET console app using `System.Net.HttpListener` (`orders/App.cs`) with no third-party
runtime dependencies. Inventory lives in memory; `Inventory.Reserve` takes stock and
`Inventory.Release` gives it back. Money is a decimal number of cents; do not introduce
floating point for amounts.

## Conventions

- Business rule violations are expected: `OutOfStockException` is returned as HTTP 409 with
  `code=OUT_OF_STOCK`. A 409 is not a defect.
- Coupons: `Kind` is `Amount` or `Percent`; percent `Value` is a percentage (10 means 10%).
  An amount coupon's threshold is at least twice its face value.
- Domain logic lives in plain classes and is unit-tested without the HTTP server.
- Tests use the in-repo harness: add `public static void Test*` methods to `orders.Tests`,
  run all with `dotnet run --project orders.Tests`, or one with
  `dotnet run --project orders.Tests -- PricingTests::TestPercentCouponUnaffected`.
  Assertions use `Check.Equal` / `Check.True` / `Check.Throws<T>`.
- Build: `dotnet build orders/Orders.csproj`.

## Do not modify

- `orders/Config.cs`, `orders/Orders.csproj` (environment/config changes need a human)
- `deploy/**`, `.github/**`
