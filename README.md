# BDFFHD Modding

Modding toolkit for **Bravely Default: Flying Fairy HD** (Unity 6 / IL2CPP).

Two approaches: **code mods** (BepInEx + Harmony) and **data mods** (btb_tool.py).

---

## Requirements

| Tool | Version | Link |
|------|---------|------|
| BepInEx 6 BE | Build 697+ | https://builds.bepinex.dev/projects/bepinex_be |
| .NET 6 SDK | 6.0+ | https://dotnet.microsoft.com/download/dotnet/6.0 |
| Il2CppDumper | 6.7.46+ | https://github.com/Perfare/Il2CppDumper/releases |
| Python | 3.10+ | https://python.org |

BepInEx: get **Unity IL2CPP x64**. Do NOT use the v6.0.0-pre.2 GitHub release — it can't handle metadata v31.

---

## Game Info

- **Engine:** Unity 6 (6000.0.37f1), IL2CPP, metadata v31
- **Data tables:** Custom "BTBF" binary format in `StreamingAssets/Common/`
- `Common/` = Japanese base, `Common_en/` = English override
- Other locales: `_de`, `_es`, `_fr`, `_it`, `_kr`, `_tw`, `_cn`

---

## Code Mods (BepInEx)

### Setup

1. Extract the BepInEx zip into the game directory (next to `BDFFHD.exe`)
2. Launch the game once — BepInEx generates interop assemblies in `BepInEx\interop\` (1-3 min first run)
3. Verify: `BepInEx\LogOutput.log` shows `Chainloader initialized`

### Building

```bash
cd hello_world
dotnet build
```

DLL goes to `BepInEx\plugins\HelloWorld\` automatically. Edit `<GameDir>` in the `.csproj` if your game isn't at the default Steam path.

### Writing patches

Run Il2CppDumper to get `dump.cs` with all class/method definitions:

```bash
Il2CppDumper.exe GameAssembly.dll global-metadata.dat dump_output
```

Search `dump.cs` to find what to hook, then write Harmony patches:

```csharp
[HarmonyPatch(typeof(BtlFunction), nameof(BtlFunction.BattleInit))]
[HarmonyPostfix]
public static void Postfix(BtlFunction __instance) {
    // runs after BattleInit
}
```

---

## Data Mods (btb_tool.py)

Python CLI for reading and editing `.btb` data files. Located at `tools/btb_tool.py`.

### Commands

```bash
set BTB=C:\Program Files (x86)\Steam\steamapps\common\BDFFHD\BDFFHD_Data\StreamingAssets\Common_en

python btb_tool.py info "%BTB%\Paramater\ItemTable.btb"       # file header info
python btb_tool.py dump "%BTB%\Paramater\ItemTable.btb" -o items.json  # dump to JSON
python btb_tool.py record "%BTB%\Paramater\ItemTable.btb" 0   # inspect one record
python btb_tool.py schemas                                      # list schemas
python btb_tool.py batch-dump "%BTB%" output_dir               # dump all BTBs
```

Schemas are auto-detected from filename. Use `-s SchemaName` to override.

### Editing (dump → edit → import)

```bash
# 1. Dump
python btb_tool.py dump "%BTB%\Paramater\ItemTable.btb" -o items.json

# 2. Edit items.json — change values or rename strings directly
#    e.g. "BUY": 70 → "BUY": 999
#    e.g. "NAME": "Broadsword" → "NAME": "Mega Sword"

# 3. Import back
python btb_tool.py import "%BTB%\Paramater\ItemTable.btb" items.json -o ItemTable_mod.btb

# 4. Copy modified file into Common_en/ to apply
```

New/changed strings are appended to the string table automatically. Unchanged records produce byte-identical output.

### Schemas

Schemas live in `tools/schemas/*.json`. Each maps int32 field indices to names and marks string offset fields.

```json
{
  "name": "MyTable",
  "description": "What it contains",
  "fields": [
    {"name": "ID", "index": 0},
    {"name": "NAME", "index": 1, "string": "utf16"},
    {"name": "ICON", "index": 2, "string": "ascii"},
    {"name": "STATS", "index": 3, "count": 5}
  ]
}
```

26 built-in schemas:

| Schema | Fields | Description |
|--------|--------|-------------|
| `ItemTable` | 163 | Items, equipment, consumables |
| `CommandAbility` | 106 | Command abilities (spells, skills) |
| `SupportAbility` | 67 | Support/passive abilities |
| `SpecialTable` | 81 | Special move definitions |
| `SpecialPartsTable` | 56 | Special move parts |
| `JobTable` | 8 | Job class definitions |
| `JobCommand` | 5 | Job → command ability links |
| `PcTable` | 48 | Player character parameters |
| `PcLevelTable` | 19 | Level-up stat growth |
| `MapTable` | 10 | Map/area definitions |
| `IconTable` | 2 | Icon name lookup |
| `ParameterUpItemTable` | 7 | Stat-boosting items |
| `SyogoTable` | 5 | Job titles |
| `SyogoIconTable` | 3 | Job title icons |
| `WorldmapEntrance` | 1 | Worldmap entrance names |
| `WorldmapExit` | 8 | Worldmap exit definitions |
| `RocationTable` | 13 | Location rotation table |
| `EncountTable` | 17 | Encounter rate tables |
| `EncountDungeonTable` | 11 | Dungeon encounter revisions |
| `EncountWorldmapTable` | 21 | Worldmap encounter revisions |
| `MonsterParty` | 37 | Monster party formations |
| `StatusTable` | 2 | Status effect names |
| `BadStatusData` | 9 | Bad status parameters |
| `DrugCombine` | 3 | Compounding recipes |
| `BtlDifficult` | 13 | Difficulty multipliers |
| `BuffLimit` | 13 | Buff/debuff caps |