import { Cart } from "./cart.ts";
import type { Inventory } from "./inventory.ts";
import type { Coupon, Order, OrderLine } from "./models.ts";
import { PriceCalculator } from "./pricing.ts";

export class OrderService {
  private readonly inventory: Inventory;
  private readonly calculator: PriceCalculator;
  private readonly orders = new Map<string, Order>();
  private seq = 0;

  constructor(inventory: Inventory, calculator: PriceCalculator = new PriceCalculator()) {
    this.inventory = inventory;
    this.calculator = calculator;
  }

  create(customer: string, lines: OrderLine[], coupon: Coupon | null = null): Order {
    if (lines.length === 0) {
      throw new Error("order needs at least one line");
    }
    const cart = new Cart(customer);
    try {
      for (const line of lines) {
        this.inventory.reserve(line.sku, line.quantity);
        cart.add(line);
      }
    } catch (error) {
      for (const line of cart.lines) {
        this.inventory.release(line.sku, line.quantity);
      }
      throw error;
    }
    const breakdown = this.calculator.calculate(cart.lines, coupon);
    this.seq += 1;
    const order: Order = {
      id: `o-${this.seq}`,
      customer,
      lines: [...cart.lines],
      total: breakdown.total,
    };
    this.orders.set(order.id, order);
    return order;
  }

  get(orderId: string): Order | null {
    return this.orders.get(orderId) ?? null;
  }
}
