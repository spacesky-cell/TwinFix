import http from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";

import { port, inventoryToken } from "./config.ts";
import { Inventory, OutOfStock } from "./inventory.ts";
import type { Coupon, Order, OrderLine } from "./models.ts";
import { OrderService } from "./service.ts";

const INVENTORY = new Inventory();
const SERVICE = new OrderService(INVENTORY);
// 启动时校验必需配置：缺失时应让进程失败，而不是带着空令牌继续服务
const TOKEN = inventoryToken();
void TOKEN;

function line(raw: Record<string, unknown>): OrderLine {
  return {
    sku: String(raw.sku),
    quantity: Number(raw.quantity),
    unitPrice: Number(raw.unitPrice),
  };
}

function coupon(raw: Record<string, unknown> | null | undefined): Coupon | null {
  if (raw === null || raw === undefined) {
    return null;
  }
  return {
    code: String(raw.code),
    kind: raw.kind === "percent" ? "percent" : "amount",
    value: Number(raw.value),
    threshold: raw.threshold === undefined ? 0 : Number(raw.threshold),
  };
}

function orderPayload(order: Order): Record<string, unknown> {
  return {
    id: order.id,
    customer: order.customer,
    total: order.total,
    lines: order.lines.map((item) => ({ sku: item.sku, quantity: item.quantity })),
  };
}

async function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const text = Buffer.concat(chunks).toString("utf-8");
  return text ? (JSON.parse(text) as Record<string, unknown>) : {};
}

function respond(res: ServerResponse, status: number, payload: Record<string, unknown>): void {
  const data = JSON.stringify(payload);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
  res.end(data);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  try {
    if (req.method === "GET" && (url.pathname === "/health" || url.pathname === "/actuator/health")) {
      respond(res, 200, { status: "UP" });
      return;
    }
    if (req.method === "GET" && url.pathname.startsWith("/api/stock/")) {
      const sku = decodeURIComponent(url.pathname.slice("/api/stock/".length));
      respond(res, 200, { sku, available: INVENTORY.available(sku) });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/orders") {
      const body = await readBody(req);
      const order = SERVICE.create(
        String(body.customer ?? "anonymous"),
        (Array.isArray(body.lines) ? body.lines : []).map((item) => line(item as Record<string, unknown>)),
        coupon(body.coupon as Record<string, unknown> | null | undefined),
      );
      respond(res, 201, orderPayload(order));
      return;
    }
    respond(res, 404, { error: "not found" });
  } catch (error) {
    if (error instanceof OutOfStock) {
      respond(res, 409, { code: "OUT_OF_STOCK", message: error.message });
      return;
    }
    if (error instanceof SyntaxError || error instanceof TypeError || error instanceof RangeError) {
      respond(res, 400, { code: "BAD_REQUEST", message: error.message });
      return;
    }
    const message = error instanceof Error ? error.message : String(error);
    respond(res, 400, { code: "BAD_REQUEST", message });
  } finally {
    console.log(`${req.method} ${url.pathname} -> ${res.statusCode}`);
  }
});

server.listen(port(), "0.0.0.0", () => {
  console.log(`orders listening on :${port()}`);
});
