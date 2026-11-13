"""CSharpProfile、C# 判分与代码索引。"""

from __future__ import annotations

from pathlib import Path

from twinfix.arena.hidden import CSharp, install_csharp_hidden_tests, summarize_csharp_output
from twinfix.codeintel.csharp_index import build_csharp_index
from twinfix.profiles.base import TestSelector
from twinfix.profiles.csharp import CSharpProfile
from twinfix.profiles.factory import get_profile

FILES = {
    "orders/Models.cs": """\
namespace Shop.Orders;

public enum CouponKind { Amount, Percent }

public sealed record Coupon(string Code, CouponKind Kind, decimal Value, decimal? Threshold = null);

public sealed record OrderLine(string Sku, int Quantity, decimal UnitPrice);
""",
    "orders/Pricing.cs": """\
namespace Shop.Orders;

public interface IPricing
{
    decimal DiscountFor(decimal subtotal, Coupon? coupon);
}

public sealed class PriceCalculator : IPricing
{
    public decimal DiscountFor(decimal subtotal, Coupon? coupon)
    {
        if (coupon is null)
        {
            return 0;
        }
        var threshold = ThresholdOf(coupon);
        return subtotal >= threshold ? coupon.Value : 0;
    }

    private decimal ThresholdOf(Coupon coupon) => coupon.Threshold ?? 0;
}
""",
    "orders/Service.cs": """\
namespace Shop.Orders;

public sealed class OrderService
{
    private readonly PriceCalculator _calculator = new();

    public decimal Create(Coupon? coupon)
    {
        var line = new OrderLine("SKU-1", 1, 100m);
        var calc = new PriceCalculator();
        return calc.DiscountFor(100m, coupon) + this.Helper();
    }

    private decimal Helper() => 0;
}
""",
}


def test_detect_and_commands() -> None:
    profile = CSharpProfile()
    assert profile.detect(["Orders.sln"])
    assert profile.detect(["orders/Orders.csproj"])
    assert profile.detect(["global.json"])
    assert not profile.detect(["pom.xml"])
    build = profile.build_command(None)
    assert build[:3] == ["dotnet", "build", "."]
    assert "--artifacts-path" in build
    selected = profile.test_command(TestSelector(classes=["Shop.Tests.PricingTests::TestNoCoupon"]))
    assert selected[:2] == ["sh", "-c"]
    script = selected[2]
    assert "dotnet run" in script and ".Tests" in script
    assert "--junit" in script and "Shop.Tests.PricingTests.TestNoCoupon" in script
    assert profile.test_paths() == ["**/*Tests.cs", "**/*Test.cs", "**/*.Tests/**", "**/tests/**"]
    assert profile.source_suffixes() == (".cs",)
    assert profile.runtime_probe() is None
    assert get_profile("cs").name == "csharp"
    assert get_profile("C#").name == "csharp"
    assert get_profile("dotnet").name == "csharp"


def test_selector_matching() -> None:
    profile = CSharpProfile()
    assert profile.case_matches("Shop.Tests.PricingTests::TestNoCoupon", "Shop.Tests.PricingTests#TestNoCoupon")
    assert profile.case_matches("PricingTests::TestNoCoupon", "Shop.Tests.PricingTests#TestNoCoupon")
    assert profile.case_matches("TestNoCoupon", "Shop.Tests.PricingTests#TestNoCoupon")
    assert profile.case_matches("PricingTests", "Shop.Tests.PricingTests#TestNoCoupon")
    assert not profile.case_matches("Shop.Tests.PricingTests::Other", "Shop.Tests.PricingTests#TestNoCoupon")


def test_parse_stacktrace_and_frame_location() -> None:
    profile = CSharpProfile(("Shop.Orders",))
    text = (
        "System.NullReferenceException: Object reference not set to an instance of an object.\n"
        "   at Shop.Orders.PriceCalculator.ThresholdOf(Coupon coupon) in /app/orders/Pricing.cs:line 37\n"
        "   at Shop.Orders.PriceCalculator.DiscountFor(Decimal subtotal, Coupon coupon) "
        "in /app/orders/Pricing.cs:line 23\n"
        "   at Shop.Orders.OrderService.Create(String customer, IReadOnlyList`1 lines, Coupon coupon)\n"
        "   at System.Environment.get_StackTrace()\n"
        "   at Program.<Main>$(String[] args) in /app/orders/App.cs:line 24\n"
    )
    frames = profile.parse_stacktrace(text)
    assert [frame.symbol for frame in frames] == [
        "Shop.Orders.PriceCalculator.ThresholdOf",
        "Shop.Orders.PriceCalculator.DiscountFor",
        "Shop.Orders.OrderService.Create",
        "System.Environment.get_StackTrace",
        "Program.<Main>$",
    ]
    assert [frame.is_project for frame in frames] == [True, True, True, False, True]
    assert frames[2].line is None
    location = profile.frame_to_location(frames[0], ["orders/Pricing.cs", "orders/Service.cs"])
    assert location is not None and location.path == "orders/Pricing.cs" and location.line == 37


def test_csharp_index_symbols_calls_and_inheritance() -> None:
    index = build_csharp_index(FILES)
    ids = set(index.symbols)
    assert {
        "Shop.Orders.Coupon",
        "Shop.Orders.Coupon#field:Code",
        "Shop.Orders.CouponKind#field:Amount",
        "Shop.Orders.OrderLine#field:Sku",
        "Shop.Orders.IPricing",
        "Shop.Orders.IPricing#DiscountFor/2",
        "Shop.Orders.PriceCalculator",
        "Shop.Orders.PriceCalculator#DiscountFor/2",
        "Shop.Orders.PriceCalculator#ThresholdOf/1",
        "Shop.Orders.OrderService#Create/1",
        "Shop.Orders.OrderService#field:_calculator",
    } <= ids

    calculator = index.symbols["Shop.Orders.PriceCalculator"]
    assert any(
        edge.kind == "implements" and edge.src == calculator.id and edge.dst == "Shop.Orders.IPricing"
        for edge in index.edges
    )
    discount = index.symbols["Shop.Orders.PriceCalculator#DiscountFor/2"]
    assert {edge.name for edge in index.edges if edge.src == discount.id and edge.kind == "calls"} == {"ThresholdOf"}
    create = index.symbols["Shop.Orders.OrderService#Create/1"]
    assert {
        (edge.kind, edge.dst) for edge in index.edges if edge.src == create.id and edge.kind in ("calls", "constructs")
    } >= {
        ("constructs", "Shop.Orders.OrderLine"),
        ("constructs", "Shop.Orders.PriceCalculator"),
        ("calls", "Shop.Orders.PriceCalculator#DiscountFor/2"),
        ("calls", "Shop.Orders.OrderService#Helper/0"),
    }
    assert {ref.edge.src for ref in index.find_references(index.symbols["Shop.Orders.Coupon"], "types")} >= {
        "Shop.Orders.PriceCalculator#DiscountFor/2",
        "Shop.Orders.PriceCalculator#ThresholdOf/1",
        "Shop.Orders.OrderService#Create/1",
    }
    outline = index.outline("orders/Pricing.cs")
    assert [symbol.name for symbol in outline][:2] == ["IPricing", "DiscountFor"]
    assert [symbol.id for symbol in index.search("PriceCalculator", "class")] == ["Shop.Orders.PriceCalculator"]


def test_csharp_index_using_alias() -> None:
    files = {
        "models.cs": "namespace Shop.Models;\npublic class Coupon { }\n",
        "service.cs": """\
using Models = Shop.Models;

namespace Shop.Orders;

public class Service
{
    public void Use(Models.Coupon coupon) { }
}
""",
    }
    index = build_csharp_index(files)
    assert any(
        edge.kind == "type_ref" and edge.src == "Shop.Orders.Service#Use/1" and edge.dst == "Shop.Models.Coupon"
        for edge in index.edges
    )


def test_csharp_grading_command_and_hidden_install(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "orders.Tests").mkdir(parents=True)
    (repo / "orders.Tests" / "Orders.Tests.csproj").write_text("<Project/>\n", encoding="utf-8")
    hidden = tmp_path / "PricingHiddenTests.cs"
    hidden.write_text("namespace Shop.Tests;\npublic static class PricingHiddenTests { }\n", encoding="utf-8")
    names = install_csharp_hidden_tests(repo, "orders", [hidden])
    assert names == ["PricingHiddenTests"]
    assert (repo / "orders.Tests" / "PricingHiddenTests.cs").is_file()

    local = CSharp(executable="dotnet").command(repo, "orders", names)
    assert Path(local[0]).stem.lower() == "dotnet"
    assert local[1:3] == ["run", "--artifacts-path"]
    assert str(repo / "orders.Tests" / "Orders.Tests.csproj") in local
    assert local[-1] == "PricingHiddenTests"

    image = CSharp(image="twinfix-sandbox-csharp:10.0").command(repo, "orders", None)
    assert image[:3] == ["docker", "run", "--rm"]
    assert f"{repo.resolve()}:/w" in image
    assert "twinfix-sandbox-csharp:10.0" in image
    assert "/w/.twinfix/csharp-tests.xml" in image

    summary = summarize_csharp_output(
        "Determining projects to restore...\n"
        "/w/orders/Pricing.cs(23,37): error CS8604: Possible null reference\n"
        "FAIL Shop.Tests.PricingTests.TestNoCoupon\n"
    )
    assert "error CS8604" in summary
    assert "FAIL Shop.Tests.PricingTests.TestNoCoupon" in summary
    assert "Determining projects" not in summary


def test_orchestrator_selects_csharp_profile_and_image() -> None:
    from types import SimpleNamespace

    from twinfix.domain.models import Incident, Project
    from twinfix.orchestrator.steps import Steps
    from twinfix.profiles.java import JavaProfile

    steps = object.__new__(Steps)
    steps.adapters = SimpleNamespace(profile=JavaProfile())
    steps.settings = SimpleNamespace(
        sandbox_image_python="py:3.12",
        sandbox_image_go="go:1.25",
        sandbox_image_typescript="ts:22",
        sandbox_image_csharp="cs:10.0",
    )
    assert steps._profile_of(Project(name="t", repo="acme/cs", language="csharp")).name == "csharp"
    assert steps._profile_for_inc(Incident(meta={"language": "csharp"})).name == "csharp"
    assert steps._sandbox_image(Project(name="t", repo="acme/cs", language="csharp")) == "cs:10.0"
    assert (
        steps._sandbox_image(Project(name="t", repo="acme/cs", language="csharp", config={"sandbox_image": "x"})) == "x"
    )


def test_integrity_scan_recognizes_csharp_tests() -> None:
    from twinfix.orchestrator.integrity import check_patch

    removed = """\
diff --git a/orders.Tests/PricingTests.cs b/orders.Tests/PricingTests.cs
--- a/orders.Tests/PricingTests.cs
+++ b/orders.Tests/PricingTests.cs
@@ -1,8 +1,4 @@
 public static class PricingTests
 {
-    public static void TestNoCoupon()
-    {
-        Check.Equal(1, 1);
-    }
 }
"""
    report = check_patch(removed, CSharpProfile().test_paths())
    assert any("删除了测试方法" in item for item in report.violations)
    assert any("删减了断言" in item for item in report.violations)

    skipped = """\
diff --git a/orders.Tests/PricingTests.cs b/orders.Tests/PricingTests.cs
--- a/orders.Tests/PricingTests.cs
+++ b/orders.Tests/PricingTests.cs
@@ -1,3 +1,4 @@
 public static class PricingTests
 {
+    [Fact(Skip = "later")]
     public static void TestNoCoupon() { Check.Equal(1, 1); }
 }
"""
    report = check_patch(skipped, CSharpProfile().test_paths())
    assert any("新增了跳过/禁用测试" in item for item in report.violations)


def test_csharp_index_field_receiver_and_base_call() -> None:
    files = {
        "a.cs": """\
namespace A;

public class Base { public decimal Run() => 0; }

public class Service : Base
{
    private readonly PriceCalculator _calculator = new();

    public decimal Go(Coupon coupon)
    {
        var x = _calculator.DiscountFor(1, coupon);
        return this.Helper() + base.Run();
    }

    private decimal Helper() => 0;
}

public class PriceCalculator { public decimal DiscountFor(decimal x, Coupon c) => 0; }

public class Coupon { }
"""
    }
    index = build_csharp_index(files)
    calls = {(edge.name, edge.dst) for edge in index.edges if edge.kind == "calls" and edge.src == "A.Service#Go/1"}
    assert calls == {
        ("DiscountFor", "A.PriceCalculator#DiscountFor/2"),
        ("Helper", "A.Service#Helper/0"),
        ("Run", "A.Base#Run/0"),
    }
