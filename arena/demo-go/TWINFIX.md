# demo-go

Project notes for TwinFix agents.

## Modules

| Module | Responsibility | Key types |
|---|---|---|
| `orders` | Create orders, price calculation, coupons, stock reservation | `OrderService`, `PriceCalculator`, `Cart`, `Inventory` |

The service is a standard-library HTTP server (`orders/app.go`); `cmd/orders` is the process entry point.
Inventory lives in memory; `Inventory.Reserve` takes stock and `Inventory.Release` gives it back.
Money is an integer number of cents (`Money int64`); do not introduce floating point for amounts.

## Conventions

- Business rule violations are expected: `OutOfStock` is returned as HTTP 409 with `code=OUT_OF_STOCK`.
  A 409 is not a defect.
- Coupons: `kind` is `amount` or `percent`; percent `value` is a percentage (10 means 10%).
  An amount coupon's threshold is at least twice its face value.
- Domain logic lives in plain types and is unit-tested without the HTTP server.
- Tests use the standard library: `go test ./...`. Run one test: `go test ./orders -run TestName`.

## Do not modify

- `orders/config.go`, `go.mod` (environment/config changes need a human)
- `deploy/**`, `.github/**`
