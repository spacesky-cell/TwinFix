import type { OrderLine } from "./models.ts";

export class Cart {
  readonly customer: string;
  readonly lines: OrderLine[];

  constructor(customer: string, lines: OrderLine[] = []) {
    this.customer = customer;
    this.lines = [...lines];
  }

  add(line: OrderLine): void {
    this.lines.push(line);
  }

  subtotal(): number {
    return this.lines.reduce((sum, line) => sum + line.unitPrice * line.quantity, 0);
  }
}
