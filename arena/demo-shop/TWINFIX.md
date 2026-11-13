# demo-shop

Project notes for TwinFix agents.

## Modules

| Module | Responsibility | Key classes |
|---|---|---|
| order | Create orders, price calculation, coupons | `OrderService`, `PriceCalculator`, `Coupon` |
| inventory | Stock lookup and atomic reservation | `StockService` |

`order` calls `inventory` over HTTP (`HttpInventoryGateway`). Both use one PostgreSQL database `shop`;
each module owns its own tables (`schema.sql` / `data.sql` in its resources).

## Conventions

- Business rule violations throw `BizException` and return HTTP 409. They are expected, not bugs.
- Stock is reserved per line and must be given back if the order does not go through:
  `InventoryGateway.reserve` takes stock, `InventoryGateway.release` returns it
  (inventory side: `POST /api/stock/{sku}/release` → `StockService.release` → `StockRepository.release`).
  A multi-line order that fails halfway releases the lines it already reserved; compensation failures
  are logged, never allowed to mask the original exception.
- Money is `BigDecimal` with scale 2, rounding `HALF_UP`.
- Domain logic lives in plain classes (`domain/`) and is unit-tested without Spring or a database.
  Repositories and HTTP clients are interfaces in `infra/` so services can be tested with in-memory fakes.
- Tests use JUnit 5 + AssertJ. Run one module's tests: `mvn -pl order test`.

## Do not modify

- `**/application*.yml`, `**/schema.sql`, `**/data.sql` (schema/config changes need a human)
- `pom.xml` dependencies
