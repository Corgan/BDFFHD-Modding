using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using Il2CppInterop.Common.XrefScans;

namespace HelloWorld.Patches;

/// <summary>
/// Workaround for Il2CppInterop Unity 6 bug:
/// "System.InvalidOperationException: Sequence contains more than one element"
/// in GenericMethod_GetMethod_Unity6_Hook.FindTargetMethod().
///
/// Must be applied BEFORE any ClassInjector.RegisterTypeInIl2Cpp calls.
/// See: https://github.com/BepInEx/Il2CppInterop/issues/183
/// </summary>
[HarmonyPatch]
internal static class UnwrapIl2CppCallPatch
{
    internal const string GuardKey = "InjectorFix.Applied";
    public static bool Enable { get; set; }

    [HarmonyPrepare]
    private static bool Prepare() => AppDomain.CurrentDomain.GetData(GuardKey) == null;

    [HarmonyTargetMethod]
    private static MethodInfo TargetMethod() =>
        AccessTools.Method("Il2CppInterop.Runtime.Injection.InjectorHelpers:GetIl2CppExport");

    [HarmonyPostfix]
    private static void Postfix(ref nint __result)
    {
        if (!Enable) return;
        var targets = XrefScannerLowLevel.JumpTargets(__result).ToList();
        if (targets.Count > 1)
            __result = targets[1];
        else if (targets.Count == 1)
            __result = targets[0];
        // else: leave __result unchanged
    }
}

[HarmonyPatch]
internal static class InjectorHookFixPatch
{
    [HarmonyPrepare]
    private static bool Prepare()
    {
        if (AppDomain.CurrentDomain.GetData(UnwrapIl2CppCallPatch.GuardKey) != null)
            return false;
        AppDomain.CurrentDomain.SetData(UnwrapIl2CppCallPatch.GuardKey, true);
        return true;
    }

    [HarmonyTargetMethods]
    private static IEnumerable<MethodBase> TargetMethods()
    {
        string[] targets =
        [
            "Il2CppInterop.Runtime.Injection.Hooks.Class_FromIl2CppType_Hook:FindTargetMethod",
            "Il2CppInterop.Runtime.Injection.Hooks.Class_FromName_Hook:FindTargetMethod",
            "Il2CppInterop.Runtime.Injection.Hooks.Class_GetFieldDefaultValue_Hook:FindClassGetFieldDefaultValueXref",
            "Il2CppInterop.Runtime.Injection.Hooks.GenericMethod_GetMethod_Hook:FindTargetMethod",
            "Il2CppInterop.Runtime.Injection.Hooks.GenericMethod_GetMethod_Unity6_Hook:FindTargetMethod",
            "Il2CppInterop.Runtime.Injection.Hooks.MetadataCache_GetTypeInfoFromTypeDefinitionIndex_Hook:FindGetTypeInfoFromTypeDefinitionIndex",
        ];

        foreach (var name in targets)
        {
            var method = AccessTools.Method(name);
            if (method != null)
                yield return method;
        }
    }

    [HarmonyPrefix]
    private static void Prefix()
    {
        UnwrapIl2CppCallPatch.Enable = true;
    }

    [HarmonyPostfix]
    private static void Postfix()
    {
        UnwrapIl2CppCallPatch.Enable = false;
    }
}
