using System.Reflection;
using System.Text;
using System.Xml;

namespace Shop.Tests;

/// <summary>
/// 极简测试 harness：反射执行本程序集里的 public static void Test* 方法。
/// 不依赖 xUnit/NUnit/MSTest，离线环境无需 NuGet；结果同时输出到控制台与 JUnit XML。
/// 用法：dotnet run -- [--junit path] [Class::Method | Class | Method ...]
/// </summary>
public static class TestHarness
{
    private sealed record Result(string Class, string Name, string? Error);

    public static int Main(string[] args)
    {
        var junit = "";
        var filters = new List<string>();
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] == "--junit" && i + 1 < args.Length)
            {
                junit = args[++i];
                continue;
            }

            filters.Add(args[i].Replace("::", ".").Replace("#", "."));
        }

        var results = new List<Result>();
        var types = typeof(TestHarness).Assembly
            .GetTypes()
            .Where(type => type.IsClass && type.IsPublic)
            .OrderBy(type => type.FullName, StringComparer.Ordinal);
        foreach (var type in types)
        {
            var methods = type.GetMethods(BindingFlags.Public | BindingFlags.Static)
                .Where(method => method.Name.StartsWith("Test", StringComparison.Ordinal))
                .Where(method => method.GetParameters().Length == 0)
                .OrderBy(method => method.Name, StringComparer.Ordinal);
            foreach (var method in methods)
            {
                if (!Matches(filters, type, method))
                {
                    continue;
                }

                results.Add(Run(type, method));
            }
        }

        var failed = results.Count(result => result.Error is not null);
        foreach (var result in results)
        {
            if (result.Error is null)
            {
                Console.WriteLine($"PASS {result.Class}.{result.Name}");
            }
            else
            {
                Console.WriteLine($"FAIL {result.Class}.{result.Name}");
                Console.WriteLine("  " + result.Error.Replace("\n", "\n  ", StringComparison.Ordinal));
            }
        }

        Console.WriteLine($"SUMMARY total={results.Count} passed={results.Count - failed} failed={failed}");
        if (!string.IsNullOrEmpty(junit))
        {
            WriteJUnit(junit, results);
        }

        return failed == 0 ? 0 : 1;
    }

    private static bool Matches(List<string> filters, Type type, MethodInfo method)
    {
        if (filters.Count == 0)
        {
            return true;
        }

        var full = $"{type.FullName}.{method.Name}";
        var className = type.FullName ?? type.Name;
        foreach (var filter in filters)
        {
            if (full == filter || full.EndsWith($".{filter}", StringComparison.Ordinal))
            {
                return true;
            }

            if (className == filter || className.EndsWith($".{filter}", StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    private static Result Run(Type type, MethodInfo method)
    {
        try
        {
            var value = method.Invoke(null, null);
            if (value is Task task)
            {
                task.GetAwaiter().GetResult();
            }

            return new Result(type.FullName ?? type.Name, method.Name, null);
        }
        catch (Exception error)
        {
            var actual = error is TargetInvocationException { InnerException: not null } invocation
                ? invocation.InnerException!
                : error;
            return new Result(
                type.FullName ?? type.Name,
                method.Name,
                $"{actual.GetType().Name}: {actual.Message}");
        }
    }

    private static void WriteJUnit(string path, List<Result> results)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var settings = new XmlWriterSettings { Indent = true, Encoding = new UTF8Encoding(false) };
        using var writer = XmlWriter.Create(path, settings);
        writer.WriteStartDocument();
        writer.WriteStartElement("testsuites");
        writer.WriteStartElement("testsuite");
        writer.WriteAttributeString("name", "Orders.Tests");
        writer.WriteAttributeString("tests", results.Count.ToString());
        writer.WriteAttributeString("failures", results.Count(result => result.Error is not null).ToString());
        foreach (var result in results)
        {
            writer.WriteStartElement("testcase");
            writer.WriteAttributeString("classname", result.Class);
            writer.WriteAttributeString("name", result.Name);
            if (result.Error is not null)
            {
                writer.WriteStartElement("failure");
                writer.WriteAttributeString("message", result.Error);
                writer.WriteString(result.Error);
                writer.WriteEndElement();
            }

            writer.WriteEndElement();
        }

        writer.WriteEndElement();
        writer.WriteEndElement();
        writer.WriteEndDocument();
    }
}
