"""订单服务的 HTTP 入口：标准库实现，镜像不装第三方依赖，孪生构建可离线完成。"""

from __future__ import annotations

import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from orders.config import PORT
from orders.inventory import Inventory, OutOfStock
from orders.models import Coupon, Order, OrderLine
from orders.service import OrderService

INVENTORY = Inventory()
SERVICE = OrderService(INVENTORY)


def _line(raw: dict[str, Any]) -> OrderLine:
    return OrderLine(
        sku=str(raw["sku"]),
        quantity=int(raw["quantity"]),
        unit_price=Decimal(str(raw["unit_price"])),
    )


def _coupon(raw: dict[str, Any] | None) -> Coupon | None:
    if raw is None:
        return None
    return Coupon(
        code=str(raw["code"]),
        kind=str(raw["kind"]),
        value=Decimal(str(raw["value"])),
        threshold=Decimal(str(raw.get("threshold", "0"))),
    )


def _order(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "customer": order.customer,
        "total": str(order.total),
        "lines": [{"sku": line.sku, "quantity": line.quantity} for line in order.lines],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "demo-python/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 契约
        path = urlparse(self.path).path
        if path in ("/health", "/actuator/health"):
            self._respond(200, {"status": "UP"})
            return
        if path.startswith("/api/stock/"):
            sku = path.rsplit("/", 1)[-1]
            self._respond(200, {"sku": sku, "available": INVENTORY.available(sku)})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 契约
        if urlparse(self.path).path != "/api/orders":
            self._respond(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            order = SERVICE.create(
                customer=str(body.get("customer", "anonymous")),
                lines=[_line(raw) for raw in body.get("lines", [])],
                coupon=_coupon(body.get("coupon")),
            )
        except OutOfStock as exc:
            self._respond(409, {"code": "OUT_OF_STOCK", "message": str(exc)})
            return
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            self._respond(400, {"code": "BAD_REQUEST", "message": str(exc)})
            return
        self._respond(201, _order(order))

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.log_message("%s %s -> %s", self.command, self.path, status)

    def log_message(self, format: str, *args: object) -> None:
        # 孪生日志里保留一行一个请求，便于 Agent 用 twin_logs 观察
        print(f"{self.address_string()} - {format % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104 - 孪生容器内监听
    print(f"orders listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
