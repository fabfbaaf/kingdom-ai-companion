// Test doubles exercise the reflection boundary and transaction safeguards.
// They do not claim that a live IL2CPP detour or a real save has been tested.
namespace HarmonyLib
{
    public sealed class Harmony
    {
        public static System.Reflection.MethodInfo? CommitPostfix;
        public void Patch(System.Reflection.MethodInfo method, HarmonyMethod? prefix = null, HarmonyMethod? postfix = null)
        {
            if (method.Name == "TransactionComplete") CommitPostfix = postfix?.Method;
        }
    }
    public sealed class HarmonyMethod(System.Reflection.MethodInfo method) { public System.Reflection.MethodInfo Method { get; } = method; }
}
namespace KingdomAI.Bridge
{
    public static class BridgeMod { public static bool ReceivePrefix(object player) => true; }
}
namespace UnityEngine
{
    public static class Time { public static float timeScale { get; set; } = 1; }
    public enum KeyCode { F8 }
    public static class Input
    {
        public static bool F8Pressed;
        public static bool GetKeyDown(KeyCode code) => F8Pressed;
        public static bool GetKeyDown(string name) => throw new Exception("Wrong GetKeyDown overload");
    }
    public sealed class GameObject { public bool activeInHierarchy { get; set; } = true; }
    public sealed class Transform { public Vector3 position { get; set; } = new(); }
    public sealed class Vector3 { public float x { get; set; } }
    public static class Resources
    {
        public static readonly Dictionary<Type, object[]> All = new();
        public static T[] FindObjectsOfTypeAll<T>() => All.GetValueOrDefault(typeof(T), []).Cast<T>().ToArray();
    }
}
namespace Il2CppRewired
{
    public sealed class Player(int identity) { public int id { get; } = identity; }
}
namespace Il2Cpp
{
    public enum CurrencyType { Coins, Gems }
    public enum PayState { None, Holding, Transaction, Completed, StateKeyDown, StateKeyHoldDetected, StateKeyHold, Cancelling }
    public sealed class Wallet
    {
        public int Coins { get; set; } = 10;
        public int Gems { get; set; } = 5;
        public int GetCurrency(CurrencyType currency) => currency == CurrencyType.Coins ? Coins : Gems;
        public void Spend(CurrencyType currency) { if (currency == CurrencyType.Coins) Coins--; else Gems--; }
    }
    public sealed class Steed { public float Stamina { get; set; } = 1; public bool IsTired { get; set; } }
    public sealed class Payable
    {
        public nint Pointer { get; set; } = 50;
        public string name { get; } = "test target";
        public int Price { get; set; } = 1;
        public CurrencyType Currency { get; set; } = CurrencyType.Coins;
        public bool PayableNow { get; set; } = true;
        public bool CanPay(Player player) => PayableNow;
        public float PlayerPayPoint() => 0;
        public void TransactionComplete()
        {
            PayableNow = false;
            HarmonyLib.Harmony.CommitPostfix?.Invoke(null, new object[] { this });
        }
    }
    public sealed class Player(int id, nint pointer)
    {
        public nint Pointer { get; set; } = pointer;
        public int playerId { get; set; } = id;
        public bool hasLocalAuthority { get; set; } = true;
        public bool _tunnel { get; set; }
        public object? _inputChannel { get; set; }
        public Wallet wallet { get; } = new();
        public Steed steed { get; } = new();
        public bool hasCrown { get; } = true;
        public bool isOnBoat { get; set; }
        public bool currencyDroppingEnabled { get; set; } = true;
        public void TryDropCurrency(CurrencyType currency)
        { if (currencyDroppingEnabled && wallet.GetCurrency(currency) > 0) { wallet.Spend(currency); GroundDrops++; } }
        public UnityEngine.GameObject gameObject { get; } = new();
        public UnityEngine.Transform transform { get; } = new();
        public Payable? selectedPayable { get; set; }
        public Payable? _completingPayable { get; set; }
        public List<object> _floatingCurrency { get; } = new();
        public PayState _payState { get; set; }
        public float keyDownThreshold { get; set; } = .2f;
        public float timeBetweenCoins { get; set; } = .1f;
        public float timeBeforeTransaction { get; set; } = .3f;
        public int NativePayments { get; private set; }
        public int HeldAfterFull { get; private set; }
        public int GroundDrops { get; private set; }
        public int ActionCalls { get; private set; }
        public int ReleaseCalls { get; private set; }
        public int SprintStarts { get; private set; }
        public bool SprintHeld { get; private set; }
        public bool SprintStopped { get; private set; }
        public bool DoubleTap { get; private set; }
        private float _timer;
        public void IControllable_ReceiveInput(Il2CppRewired.Player rewiredPlayer) { }
        public void UpdateActionState(int direction, bool startSprint, bool stopSprint, bool sprintKeyPressed, bool sprintKeyDoubleTap)
        {
            ActionCalls++;
            if (startSprint) SprintStarts++;
            SprintHeld = sprintKeyPressed;
            SprintStopped = stopSprint;
            DoubleTap = sprintKeyDoubleTap;
        }
        public void ReleaseInput()
        {
            ReleaseCalls++;
            SprintHeld = false;
            _payState = PayState.None;
            _floatingCurrency.Clear();
            _timer = 0;
        }
        public void UpdatePayState(bool held, bool down, bool touch)
        {
            if (_payState == PayState.None && down) { _payState = PayState.Holding; _timer = 0; }
            if (_payState == PayState.Holding)
            {
                if (!held) { GroundDrops++; wallet.Coins--; _payState = PayState.None; return; }
                _timer += .1f;
                if (_timer > keyDownThreshold) { _payState = PayState.Transaction; _timer = 0; }
            }
            if (_payState == PayState.Completed)
            {
                if (held) HeldAfterFull++;
                _timer += .1f;
                if (_timer > timeBeforeTransaction)
                {
                    _payState = PayState.None;
                    _floatingCurrency.Clear();
                    _completingPayable?.TransactionComplete();
                    _completingPayable = null;
                }
                return;
            }
            if (_payState != PayState.Transaction || !held) return;
            _timer += .1f;
            if (_floatingCurrency.Count == 0 || _timer > timeBetweenCoins)
            {
                wallet.Spend(selectedPayable!.Currency);
                NativePayments++;
                _floatingCurrency.Add(new object());
                _timer = 0;
                if (_floatingCurrency.Count == selectedPayable!.Price)
                {
                    _payState = PayState.Completed;
                    _completingPayable = selectedPayable;
                }
            }
        }
    }
    public sealed class Kingdom
    {
        public Boat boat { get; } = new();
        public Player playerOne { get; } = new(0, 10);
        public Player playerTwo { get; } = new(1, 20);
        public List<Player> Players { get; }
        public object ActivePlayers => new Il2CppSystem.Collections.Generic.IEnumerable<Player>(Players);
        public Kingdom() => Players = new() { playerOne, playerTwo };
        public Player GetPlayer(int id) => id == 0 ? playerOne : playerTwo;
    }
    public sealed class Game
    {
        public int state { get; set; } = 2;
        public bool blockStateProgression { get; set; }
        public object? _secondaryControllable { get; set; }
        public int CoopPromptCalls { get; private set; }
        public void TriggerCoopPrompt(bool tabletCoop) { CoopPromptCalls++; }
    }
    public sealed class Menu
    {
        public static Menu Inst { get; set; } = new();
        public bool CoopAllowed { get; set; } = true;
        public int MapState { get; set; } = 1;
        public MapTimelineMenu ActiveMap { get; } = new();
        public void OnButtonMap() { ActiveMap.gameObject.activeInHierarchy = true; MapState = 2; Managers.Inst.game.state = 4; UnityEngine.Time.timeScale = 0; }
        public void Hide() { Managers.Inst.game.state = 2; UnityEngine.Time.timeScale = 1; }
    }
    public sealed class Director
    {
        public bool IsTimePaused { get; set; }
        public bool IsNight { get; }
        public int TotalDaysInReign { get; } = 1;
    }
    public sealed class Managers
    {
        public static Managers Inst { get; set; } = new();
        public static bool COOP_ENABLED { get; set; } = true;
        public static bool IsP2Playing { get; set; } = true;
        public Kingdom kingdom { get; } = new();
        public Game game { get; } = new();
        public Director director { get; } = new();
        public EnemyManager enemies { get; } = new();
        public PayableManager payables { get; } = new();
        public Managers() => game._secondaryControllable = kingdom.playerTwo;
    }
    public sealed class Enemy
    {
        public string name { get; } = "test enemy";
        public UnityEngine.Transform transform { get; } = new();
        public bool IsThreat { get; } = true;
        public bool isAttacking { get; } = false;
    }
    public sealed class EnemyManager
    {
        public Il2CppSystem.Collections.Generic.ICollection<Enemy> AllEnemies { get; } = new(new());
    }
    public sealed class PayableManager { public Payable[] AllPayables { get; set; } = Array.Empty<Payable>(); }
    public static class NetworkBigBoss
    {
        public static bool IsOnline { get; set; }
        public static bool IsClientPresent { get; set; }
    }
}
