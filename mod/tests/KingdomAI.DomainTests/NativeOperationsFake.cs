namespace Il2Cpp;

public sealed class CampaignSaveData
{
    public static CampaignSaveData current { get; set; } = new();
    public int realStartDateTime { get; set; } = 1728000123;
    public int reign { get; set; } = 1;
    public int challengeId { get; set; } = 0;
}

public sealed class Button
{
    public nint Pointer { get; } = 160;
    public UnityEngine.GameObject gameObject { get; } = new();
    public bool interactable { get; set; } = true;
}
public sealed class MapOptions { public bool userCanCloseMap { get; set; } = true; }
public sealed class UILand(int index)
{
    public int _landIndex { get; } = index;
    public Button _button { get; } = new();
    public void LandClickedHandler() => Menu.Inst.ActiveMap.focusedLand = _landIndex;
}
public sealed class MapTimelineMenu
{
    public nint Pointer { get; } = 110;
    public UnityEngine.GameObject gameObject { get; } = new() { activeInHierarchy = false };
    public int focusedLand { get; set; } = 0;
    public int landResult { get; set; } = -1;
    public List<UILand> lands { get; } = [new(0), new(1), new(2)];
    public MapOptions currentOptions { get; } = new();
    public Button confirmButton { get; } = new();
    public void OnButtonLeft() => focusedLand--;
    public void OnButtonRight() => focusedLand++;
    public void OnButtonConfirm() { landResult = focusedLand; }
    public void OnButtonClose()
    { gameObject.activeInHierarchy = false; Menu.Inst.MapState = 1; Managers.Inst.game.state = 2; UnityEngine.Time.timeScale = 1; }
}
public sealed class SteedAbility(Player owner)
{
    public nint Pointer { get; } = 120;
    public UnityEngine.GameObject gameObject { get; } = new();
    public string name { get; } = "fake steed skill";
    public Player _rider { get; } = owner;
    public bool IsAbilityReady { get; set; } = true;
    public bool IsAbilityInProgress { get; private set; }
    public int Activations { get; private set; }
    public void Activate() { Activations++; IsAbilityInProgress = true; }
    public void Deactivate() => IsAbilityInProgress = false;
}
public sealed class Boat
{
    public int state { get; set; } = 1;
    public int Calls { get; private set; }
    public Player? SailPlayer { get; private set; }
    public void SailAway(Player interactingPlayer) { Calls++; SailPlayer = interactingPlayer; }
}
public sealed class IUnitControllable
{
    public nint Pointer { get; } = 140;
    public int PlayerId { get; set; } = 1;
    public int Direction { get; private set; }
    public bool PayHeld { get; private set; }
    public void UpdateActionState(int direction, bool startSprint, bool stopSprint, bool sprintKeyPressed, bool sprintKeyDoubleTap) => Direction = direction;
    public void UpdatePayState(bool payKey, bool payKeyDown, bool usingTouch) => PayHeld = payKey;
}
