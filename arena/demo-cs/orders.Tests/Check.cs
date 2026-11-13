namespace Shop.Tests;

/// <summary>测试断言辅助；不依赖任何测试框架包。</summary>
public static class Check
{
    public static void Equal<T>(T expected, T actual, string? message = null)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
        {
            throw new Exception($"{message ?? "values differ"}: expected {expected}, actual {actual}");
        }
    }

    public static void True(bool condition, string? message = null)
    {
        if (!condition)
        {
            throw new Exception(message ?? "expected true");
        }
    }

    public static void False(bool condition, string? message = null)
    {
        if (condition)
        {
            throw new Exception(message ?? "expected false");
        }
    }

    public static void Null(object? value, string? message = null)
    {
        if (value is not null)
        {
            throw new Exception($"{message ?? "expected null"}: actual {value}");
        }
    }

    public static void NotNull(object? value, string? message = null)
    {
        if (value is null)
        {
            throw new Exception(message ?? "expected non-null");
        }
    }

    public static T Throws<T>(Action action, string? message = null)
        where T : Exception
    {
        try
        {
            action();
        }
        catch (T error)
        {
            return error;
        }
        catch (Exception error)
        {
            throw new Exception(
                $"{message ?? "wrong exception"}: expected {typeof(T).Name}, got {error.GetType().Name}",
                error);
        }

        throw new Exception($"{message ?? "no exception"}: expected {typeof(T).Name}");
    }
}
