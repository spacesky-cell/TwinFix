import assert from "node:assert/strict";
import { test } from "node:test";

import { Inventory } from "../inventory.ts";
import type { Coupon, OrderLine } from "../models.ts";
import { OrderService } from "../service.ts";

function service(): OrderService {
  return new OrderService(new Inventory({ "SKU-1": 10, "SKU-2": 10 }));
}

function line(unitPrice: number): OrderLine {
  return { sku: "SKU-1", quantity: 1, unitPrice };
}

test("creates an order and reserves stock", () => {
  const inventory = new Inventory({ "SKU-1": 2 });
  const orders = new OrderService(inventory);
  const order = orders.create("u-1", [line(10000)]);
  assert.equal(order.total, 10000);
  assert.equal(inventory.available("SKU-1"), 1);
});

test("failed multi-line order releases reserved lines", () => {
  const inventory = new Inventory({ "SKU-1": 5, "SKU-2": 0 });
  const orders = new OrderService(inventory);
  assert.throws(() =>
    orders.create("u-1", [
      { sku: "SKU-1", quantity: 1, unitPrice: 10000 },
      { sku: "SKU-2", quantity: 1, unitPrice: 5000 },
    ]),
  );
  assert.equal(inventory.available("SKU-1"), 5);
});

test("does not leak a coupon into the next order", () => {
  const orders = service();
  const coupon: Coupon = { code: "SAVE20", kind: "amount", value: 2000, threshold: 10000 };
  const first = orders.create("u-1", [line(10000)], coupon);
  assert.equal(first.total, 8000);
  const second = orders.create("u-2", [line(10000)], null);
  assert.equal(second.total, 10000);
});
