import assert from "node:assert/strict";
import { test } from "node:test";

import type { Coupon, OrderLine } from "../models.ts";
import { PriceCalculator } from "../pricing.ts";

const CALC = new PriceCalculator();

function lines(unitPrice: number, quantity = 1): OrderLine[] {
  return [{ sku: "SKU-X", quantity, unitPrice }];
}

test("prices an order without a coupon at subtotal", () => {
  const breakdown = CALC.calculate(lines(17980));
  assert.equal(breakdown.total, 17980);
  assert.equal(breakdown.discount, 0);
  assert.equal(breakdown.appliedCoupon, null);
});

test("amount coupon applies at configured threshold", () => {
  const coupon: Coupon = { code: "SAVE20", kind: "amount", value: 2000, threshold: 10000 };
  const breakdown = CALC.calculate(lines(10000), coupon);
  assert.equal(breakdown.total, 8000);
  assert.equal(breakdown.appliedCoupon, "SAVE20");
});

test("amount coupon is rejected below threshold", () => {
  const coupon: Coupon = { code: "SAVE20", kind: "amount", value: 2000, threshold: 10000 };
  const breakdown = CALC.calculate(lines(9999), coupon);
  assert.equal(breakdown.total, 9999);
  assert.equal(breakdown.appliedCoupon, null);
});

test("zero-threshold amount coupon is still guarded", () => {
  const coupon: Coupon = { code: "ZERO", kind: "amount", value: 2000, threshold: 0 };
  assert.equal(CALC.calculate(lines(3999), coupon).appliedCoupon, null);
  assert.equal(CALC.calculate(lines(4000), coupon).total, 2000);
});

test("percent coupon rounds half up", () => {
  const coupon: Coupon = { code: "OFF10", kind: "percent", value: 10 };
  const breakdown = CALC.calculate(lines(5050), coupon);
  assert.equal(breakdown.discount, 505);
  assert.equal(breakdown.total, 4545);
});
