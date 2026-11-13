# demo-python

Project notes for TwinFix agents.

## Modules

| Module | Responsibility | Key classes |
|---|---|---|
| `orders` | Create orders, price calculation, coupons, stock reservation | `OrderService`, `PriceCalculator`, `Cart`, `Inventory` |

The service is a stdlib HTTP server (`orders/app.py`) with no third-party runtime dependencies.
Inventory lives in memory; `Inventory.reserve` takes stock and `Inventory.release` gives it back.

## Conventions

- Business rule violations are expected: `OutOfStock` is returned as HTTP 409 with `code=OUT_OF_STOCK`.
  A 409 is not a defect.
- Money is `Decimal` with scale 2, rounding `HALF_UP`.
- Domain logic lives in plain classes and is unit-tested without the HTTP server.
- Tests use pytest. Run all tests: `python -m pytest orders/tests`.
  Run one test: `python -m pytest orders/tests/test_pricing.py::test_name`.

## Do not modify

- `orders/config.py`, `deploy/**` (environment/config changes need a human)
- `pyproject.toml` dependencies
