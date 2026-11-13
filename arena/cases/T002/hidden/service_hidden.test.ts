import assert from "node:assert/strict";
import { test } from "node:test";

import { Inventory } from "../inventory.ts";
import type { Coupon, OrderLine } from "../models.ts";
import { OrderService } from "../service.ts";

function service(): OrderService {
  return new OrderService(new Inventory({ "SKU-1": 100, "SKU-2": 100 }));
}

function line(sku: string, unitPrice: number): OrderLine {
  return { sku, quantity: 1, unitPrice };
}

test("hidden: coupon does not leak to the next order", () => {
  const orders = service();
  const coupon: Coupon = { code: "SAVE20", kind: "amount", value: 2000, threshold: 10000 };
  const first = orders.create("u-1", [line("SKU-1", 10000)], coupon);
  assert.equal(first.total, 8000);
  const second = orders.create("u-2", [line("SKU-2", 10000)], null);
  assert.equal(second.total, 10000, "coupon leaked from previous order");
});

test("hidden: no-coupon order before coupon still pays full price", () => {
  const orders = service();
  const first = orders.create("u-1", [line("SKU-1", 10000)], null);
  assert.equal(first.total, 10000);
  const coupon: Coupon = { code: "SAVE20", kind: "amount", value: 2000, threshold: 10000 };
  const second = orders.create("u-2", [line("SKU-2", 10000)], coupon);
  assert.equal(second.total, 8000);
});

test("hidden: coupon applies to its own order only", () => {
  const orders = service();
  const firstCoupon: Coupon = { code: "SAVE20", kind: "amount", value: 2000, threshold: 10000 };
  const secondCoupon: Coupon = { code: "OFF10", kind: "percent", value: 10 };
  const first = orders.create("u-1", [line("SKU-1", 10000)], firstCoupon);
  const second = orders.create("u-2", [line("SKU-2", 10000)], secondCoupon);
  assert.equal(first.total, 8000);
  assert.equal(second.total, 9000);
});
