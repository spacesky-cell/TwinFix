from __future__ import annotations


class OutOfStock(Exception):
    def __init__(self, sku: str, requested: int, available: int) -> None:
        self.sku = sku
        self.requested = requested
        self.available = available
        super().__init__(f"insufficient stock for {sku}: requested {requested}, available {available}")


class Inventory:
    def __init__(self, stock: dict[str, int] | None = None) -> None:
        self._stock = dict(stock or {"SKU-1": 500, "SKU-2": 300, "SKU-3": 0})

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        available = self.available(sku)
        if available < quantity:
            raise OutOfStock(sku, quantity, available)
        self._stock[sku] = available - quantity

    def release(self, sku: str, quantity: int) -> None:
        self._stock[sku] = self.available(sku) + quantity
