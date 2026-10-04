// Mirrors the actual generated IL2CPP interface shape, not CLR collections.
namespace Il2CppSystem
{
    public abstract class NativeObject
    {
        public static int Disposals;
        public T Cast<T>() where T : NativeObject => (T)As(typeof(T));
        protected abstract NativeObject As(Type type);
    }
    public sealed class IDisposable(Action dispose) : NativeObject
    {
        public void Dispose() => dispose();
        protected override NativeObject As(Type type) => type == GetType() ? this : throw new InvalidCastException();
    }
}
namespace Il2CppSystem.Collections
{
    public sealed class IEnumerator(Func<bool> next) : NativeObject
    {
        public bool MoveNext() => next();
        protected override NativeObject As(Type type) => type == GetType() ? this : throw new InvalidCastException();
    }
}
namespace Il2CppSystem.Collections.Generic
{
    public sealed class IEnumerable<T>(List<T> items) : NativeObject
    {
        public IEnumerator<T> GetEnumerator() => new(items);
        protected override NativeObject As(Type type) => type == GetType() ? this : throw new InvalidCastException();
    }
    public sealed class ICollection<T>(List<T> items) : NativeObject
    {
        public int Count => items.Count;
        public void Add(T item) => items.Add(item);
        protected override NativeObject As(Type type) => type == typeof(IEnumerable<T>) ? new IEnumerable<T>(items)
            : type == GetType() ? this : throw new InvalidCastException();
    }
    public sealed class IEnumerator<T>(List<T> items) : NativeObject
    {
        private int _index = -1;
        public T Current => items[_index];
        protected override NativeObject As(Type type) => type == typeof(Collections.IEnumerator)
            ? new Collections.IEnumerator(() => ++_index < items.Count)
            : type == typeof(Il2CppSystem.IDisposable) ? new Il2CppSystem.IDisposable(() => Disposals++)
            : type == GetType() ? this : throw new InvalidCastException();
    }
}
