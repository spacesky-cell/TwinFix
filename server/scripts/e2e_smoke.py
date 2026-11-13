"""端到端冒烟：通过 HTTP 走完整条真实链路（nginx → API → RabbitMQ → worker → PG → Redis → SSE）。

用法：
    docker compose -f deploy/compose.dev.yml up -d --build
    uv run python scripts/e2e_smoke.py [--base http://127.0.0.1:17317/api] [--scenario happy]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import httpx

TERMINAL = {"NOT_BUG", "CANNOT_REPRODUCE", "CANNOT_FIX", "ALREADY_FIXED", "CANCELLED", "CLOSED"}


def wait_state(c: httpx.Client, iid: str, targets: set[str], timeout: float = 60) -> dict:  # type: ignore[type-arg]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = c.get(f"/incidents/{iid}").json()
        if d["state"] in targets:
            return d  # type: ignore[no-any-return]
        time.sleep(0.5)
    raise SystemExit(f"timeout waiting for {targets}, last state {d['state']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:17317/api")
    ap.add_argument("--scenario", default="happy")
    args = ap.parse_args()

    # trust_env=False：本机可能配置了 HTTP_PROXY，访问 127.0.0.1 不应走代理
    c = httpx.Client(base_url=args.base, timeout=30, trust_env=False)
    print("health:", c.get("/health").json())
    if not any(p["name"] == "demo-shop" for p in c.get("/projects").json()):
        c.post("/projects", json={"name": "demo-shop", "repo": "acme/demo-shop"}).raise_for_status()

    r = c.post(
        "/incidents",
        json={
            "project": "demo-shop",
            "title": f"下单并发时库存出现负数 [{args.scenario} {int(time.time())}]",
            "service": "order",
            "scenario": args.scenario,
            "fingerprint": f"smoke-{time.time_ns()}",
        },
    )
    r.raise_for_status()
    iid = r.json()["incident_id"]
    print("incident:", iid)

    sse_frames: list[dict] = []  # type: ignore[type-arg]

    def listen() -> None:
        timeout = httpx.Timeout(10, read=120)  # SSE 每 15 秒有心跳
        with httpx.stream("GET", f"{args.base}/incidents/{iid}/stream", timeout=timeout, trust_env=False) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:") and line != "data: {}":
                    sse_frames.append(json.loads(line[5:]))
                    if sse_frames[-1]["kind"] == "finalized":
                        return

    threading.Thread(target=listen, daemon=True).start()

    d = wait_state(c, iid, {"AWAITING_REVIEW", "NEEDS_HUMAN"} | TERMINAL)
    print("reached:", d["state"])
    if d["state"] == "AWAITING_REVIEW":
        # 进入 AWAITING_REVIEW 后，等待节点在下一步才创建审批并挂起
        ap_id = None
        for _ in range(60):
            d = c.get(f"/incidents/{iid}").json()
            ap_id = next((a["id"] for a in d["approvals"] if a["action"] == "merge" and a["status"] == "pending"), None)
            if ap_id:
                break
            time.sleep(0.5)
        assert ap_id, "no pending merge approval"
        c.post(f"/approvals/{ap_id}/decide", json={"approve": True, "comment": "复测通过"}).raise_for_status()
        d = wait_state(c, iid, TERMINAL | {"NEEDS_HUMAN"})
    print("final:", d["state"], "| reason:", d["closed_reason"])
    if d["state"] in TERMINAL:
        # 终态后还有 finalize 一步（回收环境、发终态报告）
        for _ in range(60):
            evs = c.get(f"/incidents/{iid}/events", params={"limit": 2000}).json()
            if any(e["kind"] == "finalized" for e in evs):
                break
            time.sleep(0.5)
        d = c.get(f"/incidents/{iid}").json()

    events = c.get(f"/incidents/{iid}/events", params={"limit": 2000}).json()
    path = [e["to_state"] for e in events if e["kind"] == "transition"]
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("path:", " -> ".join(path))
    print("events:", len(events), kinds)
    print("reports:", [(r["type"], r["status"]) for r in d["reports"]])
    print("runs:", [(r["agent"], r["outcome"]) for r in d["runs"]])

    time.sleep(1.5)
    sse_ids = sorted({f["id"] for f in sse_frames})
    db_ids = [e["id"] for e in events]
    print(f"SSE received {len(sse_ids)}/{len(db_ids)} events")

    ok = d["state"] == "CLOSED" if args.scenario == "happy" else d["state"] in TERMINAL | {"NEEDS_HUMAN"}
    ok = ok and sse_ids == db_ids
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
