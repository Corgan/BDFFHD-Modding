using HarmonyLib;

namespace HelloWorld.Patches;

/// <summary>
/// Logs a message every time a new battle starts.
/// </summary>
[HarmonyPatch(typeof(BtlFunction), nameof(BtlFunction.BattleInit))]
public static class BattleInitPatch
{
    [HarmonyPostfix]
    public static void Postfix()
    {
        Plugin.Log.LogInfo("[HelloWorld] A battle just started!");
    }
}
