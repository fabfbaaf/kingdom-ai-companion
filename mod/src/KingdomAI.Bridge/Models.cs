using System.Text.Json;
using System.Text.Json.Serialization;

namespace KingdomAI.Bridge;

internal static class Wire
{
    internal static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = new SnakePolicy(),
        DefaultIgnoreCondition = JsonIgnoreCondition.Never
    };
    internal static string Serialize(object value) => JsonSerializer.Serialize(value, Json);
    private sealed class SnakePolicy : JsonNamingPolicy
    {
        public override string ConvertName(string name) => System.Text.RegularExpressions.Regex.Replace(name, "(?<!^)([A-Z])", "_$1").ToLowerInvariant();
    }
}

public sealed record PayableState(string TargetId, string Name, double? X, int? Price, string? Currency, bool? CanPay);
public sealed record PlayerState(int PlayerId, double? X, int? Coins, double? Stamina, bool? Crown, PayableState? CurrentPayable, bool? TransactionPending = null, bool? CanSprint = null,
    Dictionary<string, int?>? Currencies = null, string? SteedType = null, double? PaymentTimeoutMs = null);
public sealed record WorldState(bool? IsNight, bool? IsPaused, double? Day, object[] NearbyEnemies, object[] NearbyPayables,
    object? Campaign = null, object? Environment = null, object[]? Targets = null, object[]? Enemies = null,
    object[]? DroppedItems = null, object[]? Units = null, object[]? Structures = null, object[]? Abilities = null,
    object? Ui = null, object? Control = null, object[]? Quests = null);
public sealed record Observation(
    string BridgeVersion, string GameVersion, string SessionId, long ObservationSeq, string CapturedAt,
    string Scene, bool Ready, string Reason, bool Coop, int ControlledPlayerId, bool InputReleased,
    PlayerState[] Players, string[] Capabilities, string[] Diagnostics, WorldState? World = null, string? CoopRequestResult = null, long ControlEpoch = 0,
    bool UiReady = false)
{
    [JsonIgnore] public PlayerState? P2 => Players.FirstOrDefault(p => p.PlayerId == 1);
}
public sealed record Command(string ActionId, string SessionId, int PlayerId, string Operation,
    string? Direction = null, int DurationMs = 500, string? TargetId = null, int MaxCoins = 1, string? ControlId = null, long ControlEpoch = 0, bool Sprint = false,
    string? Currency = null, int Amount = 1, string? Ability = null, string? MapAction = null, int? Land = null, double? TargetX = null, string? AbilityAction = null);
public sealed record Receipt(string ActionId, string SessionId, string Status, string Message, PlayerState? Before, PlayerState? After, object? Effect = null);
public sealed record NativeResult(bool Success, string Message, object? Effect = null);
public sealed record ControlInput(int Direction, bool Pay, bool PayDown, string? ActionId = null, int MaxCoins = 1, string? TargetId = null, bool Sprint = false,
    string Operation = "move", string? Currency = null);

public sealed class CommandError(int code, string message) : Exception(message)
{
    public int Code { get; } = code;
}
