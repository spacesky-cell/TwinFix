# demo-ts

Project notes for TwinFix agents.

## Modules

| Module | Responsibility | Key types |
|---|---|---|
| `orders` | Create orders, price calculation, coupons, stock reservation | `OrderService`, `PriceCalculator`, `Cart`, `Inventory` |

The service is a Node HTTP server (`orders/app.ts`) with no third-party runtime dependencies.
TypeScript is executed directly by Node 22's type stripping; there is no build step and no `tsc`.
Inventory lives in memory; `Inventory.reserve` takes stock and `Inventory.release` gives it back.
Money is an integer number of cents (`number`); do not introduce floating point for amounts.

## Conventions

- Business rule violations are expected: `OutOfStock` is returned as HTTP 409 with `code=OUT_OF_STOCK`.
  A 409 is not a defect.
- Coupons: `kind` is `amount` or `percent`; percent `value` is a percentage (10 means 10%).
  An amount coupon's threshold is at least twice its face value.
- Domain logic lives in plain classes and is unit-tested without the HTTP server.
- Tests use the standard library: `node --test` (discovery from the repo root).
  Run one test: `node --test --test-name-pattern "test name" orders/tests/pricing.test.ts`.

## Do not modify

- `orders/config.ts`, `package.json` (environment/config changes need a human)
- `deploy/**`, `.github/**`
