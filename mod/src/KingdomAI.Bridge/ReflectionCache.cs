using System.Collections.Concurrent;
using System.Reflection;

namespace KingdomAI.Bridge;

// Cache metadata only. Native values and ownership are read again on every call.
internal static class ReflectionCache
{
    private const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.FlattenHierarchy;
    private sealed record TypeEntry(Type? Type, int Generation);
    internal sealed record Member(PropertyInfo? Property, FieldInfo? Field);
    private sealed record MethodEntry(MethodInfo? Method);
    private readonly record struct CallKey(Type Owner, string Name, int Count, Type? A, Type? B, Type? C, Type? D, Type? E);
    private static readonly ConcurrentDictionary<string, TypeEntry> Types = new();
    private static readonly ConcurrentDictionary<(Type, string), Member> Members = new();
    private static readonly ConcurrentDictionary<Type, MethodInfo[]> AllMethods = new();
    private static readonly ConcurrentDictionary<(Type, string), MethodInfo[]> NamedMethods = new();
    private static readonly ConcurrentDictionary<MethodInfo, ParameterInfo[]> Parameters = new();
    private static readonly ConcurrentDictionary<CallKey, MethodEntry> Calls = new();
    private static readonly ConcurrentDictionary<(Type, string, Type), MethodEntry> Generics = new();
    private static int _generation;
    private static readonly Lazy<bool> LoadBindings = new(() =>
    {
        foreach (var name in new[] { "Assembly-CSharp", "UnityEngine.CoreModule", "UnityEngine.InputLegacyModule", "Il2CppRewired_Core" })
            try { Assembly.Load(name); } catch { }
        return true;
    });

    static ReflectionCache() => AppDomain.CurrentDomain.AssemblyLoad += (_, _) => Interlocked.Increment(ref _generation);

    internal static Type? Find(string name)
    {
        _ = LoadBindings.Value;
        var generation = Volatile.Read(ref _generation);
        if (Types.TryGetValue(name, out var cached) && (cached.Type is not null || cached.Generation == generation))
            return cached.Type;
        var type = AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(name, false)).FirstOrDefault(t => t is not null);
        Types[name] = new(type, generation);
        return type;
    }

    internal static Member GetMember(Type type, string name) => Members.GetOrAdd((type, name), key =>
        new(key.Item1.GetProperty(key.Item2, Flags), key.Item1.GetField(key.Item2, Flags)));
    internal static MethodInfo[] Methods(Type type) => AllMethods.GetOrAdd(type, t => t.GetMethods(Flags));
    internal static MethodInfo[] Methods(Type type, string name) => NamedMethods.GetOrAdd((type, name), key =>
        Methods(key.Item1).Where(m => m.Name == key.Item2).ToArray());
    internal static ParameterInfo[] Params(MethodInfo method) => Parameters.GetOrAdd(method, m => m.GetParameters());

    private static MethodInfo? Select(Type type, string name, object?[] args) => Methods(type, name).SingleOrDefault(m =>
        Params(m).Length == args.Length && Params(m).Select((p, i) => args[i] is null
            ? !p.ParameterType.IsValueType : p.ParameterType.IsInstanceOfType(args[i])).All(v => v));

    internal static MethodInfo? Resolve(Type type, string name, object?[] args)
    {
        if (args.Length > 5) return Select(type, name, args);
        Type? At(int i) => i < args.Length ? args[i]?.GetType() : null;
        var key = new CallKey(type, name, args.Length, At(0), At(1), At(2), At(3), At(4));
        return Calls.GetOrAdd(key, _ => new(Select(type, name, args))).Method;
    }

    internal static MethodInfo? Generic(Type owner, string name, Type argument) => Generics.GetOrAdd((owner, name, argument), key =>
    {
        var candidates = Methods(key.Item1, key.Item2).Where(m => m.IsGenericMethodDefinition
            && m.GetGenericArguments().Length == 1 && Params(m).Length == 0);
        var method = key.Item2 == "Cast" ? candidates.SingleOrDefault() : candidates.FirstOrDefault();
        return new(method?.MakeGenericMethod(key.Item3));
    }).Method;
}
