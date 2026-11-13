"""日志模板聚类：掩码、合并、排序与渲染。"""

from __future__ import annotations

from twinfix.gateway.logcluster import cluster, mask, render


def _line(i: int, msg: str, level: str = "ERROR") -> tuple[str, str, str, str]:
    return (f"2026-10-21T01:{i // 60:02d}:{i % 60:02d}Z", level, msg, "order")


def test_mask_volatile_parts() -> None:
    m = mask(
        'order 1042 failed after 35ms for user "u-vip" trace 3f2a9c1e-1111-2222-3333-444455556666 from 10.0.0.7:8080'
    )
    assert m == 'order <num> failed after <num> for user "<str>" trace <uuid> from <ip>'


def test_repeated_errors_collapse_to_one_template() -> None:
    lines = [_line(i, f"payment ledger write failed for order {1000 + i}: duplicate key") for i in range(300)]
    lines += [_line(i, f"GET /api/orders/{i} took {i}ms", "INFO") for i in range(50)]
    lines += [_line(1, "stock reserve timeout sku=SKU-3", "WARN")]
    clusters = cluster(lines)
    assert [c.count for c in clusters] == [300, 1, 50]  # ERROR 优先，其次 WARN，再 INFO
    assert clusters[0].template == "payment ledger write failed for order <num>: duplicate key"
    assert clusters[0].first_ts < clusters[0].last_ts


def test_differing_token_becomes_wildcard() -> None:
    lines = [_line(0, "cache miss for sku SKU-A"), _line(1, "cache miss for sku SKU-B")]
    [c] = cluster(lines)
    assert c.template == "cache miss for sku <*>" and c.count == 2


def test_render_is_compact_and_reports_overflow() -> None:
    lines = [_line(i, f"distinct failure kind{i} here") for i in range(30)]
    lines = [_line(0, f"error variant{i} {'x' * i}") for i in range(30)]
    text = render(cluster(lines), total=30, max_templates=5)
    assert text.startswith("30 hits →") and "more templates" in text
    assert text.count("\n    e.g.") == 5
