import assert from "node:assert/strict";
import { test } from "node:test";

import { Cart } from "../cart.ts";

test("cart subtotal multiplies price by quantity", () => {
  const cart = new Cart("u-1");
  cart.add({ sku: "SKU-1", quantity: 2, unitPrice: 1234 });
  cart.add({ sku: "SKU-2", quantity: 3, unitPrice: 1000 });
  assert.equal(cart.subtotal(), 5468);
});
