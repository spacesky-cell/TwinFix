export class OutOfStock extends Error {
  readonly sku: string;
  readonly requested: number;
  readonly available: number;

  constructor(sku: string, requested: number, available: number) {
    super(`insufficient stock for ${sku}: requested ${requested}, available ${available}`);
    this.name = "OutOfStock";
    this.sku = sku;
    this.requested = requested;
    this.available = available;
  }
}

export class Inventory {
  private readonly stock: Map<string, number>;

  constructor(stock: Record<string, number> = { "SKU-1": 500, "SKU-2": 300, "SKU-3": 0 }) {
    this.stock = new Map(Object.entries(stock));
  }

  available(sku: string): number {
    return this.stock.get(sku) ?? 0;
  }

  reserve(sku: string, quantity: number): void {
    if (quantity <= 0) {
      throw new Error("quantity must be positive");
    }
    const available = this.available(sku);
    if (available < quantity) {
      throw new OutOfStock(sku, quantity, available);
    }
    this.stock.set(sku, available - quantity);
  }

  release(sku: string, quantity: number): void {
    this.stock.set(sku, this.available(sku) + quantity);
  }
}
