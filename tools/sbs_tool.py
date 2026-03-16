#!/usr/bin/env python3
"""
sbs_tool.py – Read/write SimpleBinarySerializer (SBS) format (.btb2 / .tbl2).

Wire format:
  File header: [0x00] [0x01=COMPRESS_BROTLI] [0x53 0x53 = "SS"]
  Body (after brotli decompression):
    [0x01] array_count_byte  <-- NOT_NULL marker then record count as 7-bit int
    Then per record: [NOT_NULL] [fields in declaration order]
      - int/float:   4 bytes LE
      - bool:        1 byte
      - string:      [length as 7-bit int] [UTF-8 bytes]
      - int[]:       [NOT_NULL] [count as 7-bit int] [count * 4 bytes]
      - object[]:    [NOT_NULL] [count as 7-bit int] [per-elem: [NOT_NULL] [fields...]]

Commands:
  info <file>               Show file info
  dump <file> [out.json]    Dump to JSON
  import <file.json> <out>  Rebuild from JSON
  batch-dump <dir> [outdir] Dump all SBS files to JSON
"""
import argparse, brotli, json, struct, io, os, sys
from pathlib import Path


def read_7bit_int(stream: io.BytesIO) -> int:
    """Read a 7-bit encoded integer (same as BinaryReader.Read7BitEncodedInt)."""
    result = 0
    shift = 0
    while True:
        b = stream.read(1)
        if not b:
            raise EOFError("Unexpected end of stream reading 7-bit int")
        byte_val = b[0]
        result |= (byte_val & 0x7F) << shift
        if (byte_val & 0x80) == 0:
            break
        shift += 7
    return result


def read_string(stream: io.BytesIO) -> str:
    """Read a BinaryWriter-format string (7-bit length + UTF-8 bytes)."""
    length = read_7bit_int(stream)
    if length == 0:
        return ""
    raw = stream.read(length)
    return raw.decode("utf-8", errors="replace")


def read_int32(stream: io.BytesIO) -> int:
    raw = stream.read(4)
    return struct.unpack("<i", raw)[0]


def read_uint32(stream: io.BytesIO) -> int:
    raw = stream.read(4)
    return struct.unpack("<I", raw)[0]


def read_float(stream: io.BytesIO) -> float:
    raw = stream.read(4)
    return struct.unpack("<f", raw)[0]


def read_bool(stream: io.BytesIO) -> bool:
    return stream.read(1)[0] != 0


def read_nullable_marker(stream: io.BytesIO) -> bool:
    """Read NOT_NULL (0x01) / NULL (0x00) marker."""
    return stream.read(1)[0] != 0


class SBSReader:
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)

    def read_array(self, element_reader):
        """Read [NOT_NULL marker] [count] [elements...]"""
        marker = read_nullable_marker(self.stream)
        if not marker:
            return None
        count = read_7bit_int(self.stream)
        return [element_reader() for _ in range(count)]

    def read_int_array(self):
        return self.read_array(lambda: read_int32(self.stream))

    def read_uint_array(self):
        return self.read_array(lambda: read_uint32(self.stream))

    def read_record(self, schema):
        """Read one record given a schema of (name, type) tuples."""
        rec = {}
        for name, ftype in schema:
            pos = self.stream.tell()
            if ftype == "int":
                rec[name] = read_int32(self.stream)
            elif ftype == "uint":
                rec[name] = read_uint32(self.stream)
            elif ftype == "float":
                rec[name] = read_float(self.stream)
            elif ftype == "bool":
                rec[name] = read_bool(self.stream)
            elif ftype == "string":
                rec[name] = read_string(self.stream)
            elif ftype == "int[]":
                rec[name] = self.read_int_array()
            elif ftype == "uint[]":
                rec[name] = self.read_uint_array()
            elif isinstance(ftype, list):
                # Nested class: [NOT_NULL] [fields...]
                rec[name] = self._read_nested_object(ftype)
            elif isinstance(ftype, tuple) and ftype[0] == "array":
                rec[name] = self._read_object_array(ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "struct":
                # Inline struct (value type): [fields...] no NOT_NULL marker
                rec[name] = self._read_struct(ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "struct_array":
                # Array of struct: [NOT_NULL] [count] [per-elem: [fields...]] no per-elem marker
                rec[name] = self._read_struct_array(ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "float_array":
                # Nullable float array: [NOT_NULL] [count] [floats...]
                rec[name] = self._read_float_array()
            else:
                raise ValueError(f"Unknown type: {ftype} for field {name} at offset {pos}")
        return rec

    def _read_nested_object(self, schema):
        """Read a single nested class/struct: [NOT_NULL] [fields...]"""
        marker = read_nullable_marker(self.stream)
        if not marker:
            return None
        return self._read_struct(schema)

    def _read_object_array(self, schema):
        """Read array of nested objects: [NOT_NULL] [count] [per-elem: [NOT_NULL] [fields...]]"""
        marker = read_nullable_marker(self.stream)
        if not marker:
            return None
        count = read_7bit_int(self.stream)
        result = []
        for _ in range(count):
            elem_marker = read_nullable_marker(self.stream)
            if not elem_marker:
                result.append(None)
                continue
            result.append(self._read_struct(schema))
        return result

    def _read_struct(self, schema):
        rec = {}
        for name, ftype in schema:
            if ftype == "int":
                rec[name] = read_int32(self.stream)
            elif ftype == "float":
                rec[name] = read_float(self.stream)
            elif ftype == "bool":
                rec[name] = read_bool(self.stream)
            elif ftype == "string":
                rec[name] = read_string(self.stream)
            elif ftype == "int[]":
                rec[name] = self.read_int_array()
            elif ftype == "uint[]":
                rec[name] = self.read_uint_array()
            elif isinstance(ftype, list):
                rec[name] = self._read_nested_object(ftype)
            elif isinstance(ftype, tuple) and ftype[0] == "array":
                rec[name] = self._read_object_array(ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "struct":
                rec[name] = self._read_struct(ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "struct_array":
                rec[name] = self._read_struct_array(ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "float_array":
                rec[name] = self._read_float_array()
        return rec

    def _read_struct_array(self, schema):
        """Read array of struct: [NOT_NULL] [count] [per-elem: fields directly]"""
        marker = read_nullable_marker(self.stream)
        if not marker:
            return None
        count = read_7bit_int(self.stream)
        return [self._read_struct(schema) for _ in range(count)]

    def _read_float_array(self):
        """Read nullable float array: [NOT_NULL] [count] [floats...]"""
        marker = read_nullable_marker(self.stream)
        if not marker:
            return None
        count = read_7bit_int(self.stream)
        return [read_float(self.stream) for _ in range(count)]

    def read_all(self, schema) -> list[dict]:
        """Read top-level: [NOT_NULL] [count] then per-record: [NOT_NULL] [fields...]"""
        marker = read_nullable_marker(self.stream)
        if not marker:
            return []
        count = read_7bit_int(self.stream)
        records = []
        for i in range(count):
            try:
                # Each element in a class array has a NOT_NULL/NULL marker
                elem_marker = read_nullable_marker(self.stream)
                if not elem_marker:
                    records.append(None)
                    continue
                rec = self.read_record(schema)
                records.append(rec)
            except Exception as e:
                print(f"  Error at record {i}, offset {self.stream.tell()}: {e}")
                break
        return records


# ═══════════════════════════════════════════════════════════════
# Schema definitions from dump.cs class fields
# ═══════════════════════════════════════════════════════════════

SCHEMA_BtlTutorialIndexInfo = [
    ("id", "int"),
    ("pTitle", "string"),
    ("pageArray", "int[]"),
]

SCHEMA_BtlTutorialPageData = [
    ("id", "int"),
    ("pTitle", "string"),
    ("pSubTitle", "string"),
    ("type", "int"),
    ("pText", "string"),
    ("pArchiveName", "string"),
    ("pImageName", "string"),
]

# TutorialJobData — SBS version keeps raw int offsets for ASCII fields,
# but serializes utf16 pDescription as a real string.
# Layout: int id, int arcOff, int imgOff, string pDescription, 8 equip ints, 12 ability ints, int motionArcOff, int motionNameOff  
SCHEMA_TutorialJobData = [
    ("id", "int"),
    ("pArchiveNameOffset", "int"),
    ("pImageNameOffset", "int"),
    ("pDescription", "string"),
    ("equipRating_0", "int"),
    ("equipRating_1", "int"),
    ("equipRating_2", "int"),
    ("equipRating_3", "int"),
    ("equipRating_4", "int"),
    ("equipRating_5", "int"),
    ("equipRating_6", "int"),
    ("equipRating_7", "int"),
    ("abilityRating_0", "int"),
    ("abilityRating_1", "int"),
    ("abilityRating_2", "int"),
    ("abilityRating_3", "int"),
    ("abilityRating_4", "int"),
    ("abilityRating_5", "int"),
    ("abilityRating_6", "int"),
    ("abilityRating_7", "int"),
    ("abilityRating_8", "int"),
    ("abilityRating_9", "int"),
    ("abilityRating_10", "int"),
    ("abilityRating_11", "int"),
    ("pMotionArchive", "string"),
    ("pMotionName", "string"),
]
STRUCT_BtlAtkHitEffectData = [
    ("pHitEffectName", "string"),
    ("pCriticalHitEffectName", "string"),
    ("pHitSEName", "string"),
    ("pCriticalHitSEName", "string"),
]

STRUCT_BtlStationEffect = [
    ("pName", "string"),
    ("pNode", "string"),
]

STRUCT_BtlItemRate = [
    ("itemId", "int"),
    ("rate", "int"),
]

SCHEMA_BtlMonsterData = [
    ("index", "int"),
    ("pName", "string"),
    ("pDebugName", "string"),
    ("level", "int"),
    ("jobLevel", "int"),
    ("size", "int"),
    ("pRoleID", "string"),
    ("pModelID", "string"),
    ("pModelSuffix", "string"),
    ("atkHitEffectData", STRUCT_BtlAtkHitEffectData),  # single nested object [NOT_NULL][fields]
    ("cameraOffsetX", "int"),
    ("cameraOffsetY", "int"),
    ("cameraOffsetZ", "int"),
    ("hitEffectOffset", "int"),
    ("effectScale", "int"),
    ("pSEGroupName", "string"),
    ("pStartSEName", "string"),
    ("startSEFrame", "int"),
    ("hpMax", "int"),
    ("mpMax", "int"),
    ("attack", "int"),
    ("deffence", "int"),
    ("magicAttack", "int"),
    ("magicDeffence", "int"),
    ("agility", "int"),
    ("dexterity", "int"),
    ("intelligence", "int"),
    ("mind", "int"),
    ("hitRate", "int"),
    ("evasionRate", "int"),
    ("actSpeed", "int"),
    ("criticalRate", "int"),
    ("atkActIdx", "int"),
    ("elementRegistArray", "int[]"),
    ("statusRegistArray", "int[]"),
    ("typeArray", "uint[]"),
    ("genericParameter", "int[]"),
    ("stationEffectInfo", ("array", STRUCT_BtlStationEffect)),  # object array
    ("getEXP", "int"),
    ("getJobEXP", "int"),
    ("getGIL", "int"),
    ("dropItemData", ("array", STRUCT_BtlItemRate)),  # object array
    ("rootItemData", ("array", STRUCT_BtlItemRate)),  # object array
    ("bribe", "int"),
    ("avoidAbility", "bool"),
    ("deadStartMotion", "int"),
    ("deadLoopMotion", "int"),
    ("dNoteID", "int"),
]

SCHEMA_BtlMonsterCMDABILData = [
    ("index", "int"),
    ("pName", "string"),
    ("actScriptIdx", "int"),
    ("actIdx", "int"),
    ("repeatIndex", "int"),
    ("hate", "int"),
    ("useCategory", "int"),
    ("useMP", "int"),
    ("useHP", "int"),
    ("useBP", "int"),
    ("isMagic", "bool"),
    ("genomeAbilityID", "int"),
    ("isSilence", "bool"),
    ("defaultTargetType", "int"),
    ("friendTargetType", "int"),
    ("enemyTargetType", "int"),
    ("damageType", "int"),
    ("damageCategory", "int"),
    ("damageTarget", "int"),
    ("element", "int"),
    ("power", "int"),
    ("powerRatio", "int"),
    ("isMultiRate", "bool"),
    ("multiHit", "int"),
    ("isSupport", "bool"),
    ("num", "int"),
    ("turn", "int"),
    ("isRefrect", "bool"),
    ("isSuccess", "bool"),
    ("ignoreDeffence", "int"),
    ("buffEffectArray", "int[]"),
    ("selfBuffTiming", "int"),
    ("selfBuffArray", "int[]"),
    ("addStatusArray", "int[]"),
    ("addBuffElementArray", "int[]"),
    ("successRate", "int"),
    ("avoidAbility", "bool"),
]

SCHEMA_BtlMagActData = [
    ("index", "int"),
    ("subIndex", "int"),
    ("startCamera", "int"),
    ("isStartCameraOffset", "bool"),
    ("startCameraOffsetX", "float"),
    ("startCameraOffsetY", "float"),
    ("startCameraOffsetZ", "float"),
    ("startDisp", "int"),
    ("startAnimDelay", "float"),
    ("startAnimIndex", "int"),
    ("loopAnimIndex", "int"),
    ("endAnimIndex", "int"),
    ("startEffectDelay", "float"),
    ("pStartEffectName", "string"),
    ("pStartEffectNodeName", "string"),
    ("isStartEffectScale", "bool"),
    ("pStartSEName", "string"),
    ("startSEDelay", "float"),
    ("isStartDyeingUp", "bool"),
    ("startDyeingUpColorR", "float"),
    ("startDyeingUpColorG", "float"),
    ("startDyeingUpColorB", "float"),
    ("startDyeingUpColorA", "float"),
    ("startDyeingUpFrame", "float"),
    ("attackEffectDelay", "float"),
    ("pAttackEffectName", "string"),
    ("pAttackEffectNodeName", "string"),
    ("isAttackEffectScale", "bool"),
    ("isAttackEffectFollow", "bool"),
    ("attackEffectDelay2", "float"),
    ("pAttackEffectName2", "string"),
    ("pAttackEffectNodeName2", "string"),
    ("isAttackEffectScale2", "bool"),
    ("isAttackEffectFollow2", "bool"),
    ("attackWepEffectDelay", "float"),
    ("pAttackWepEffectName", "string"),
    ("isAttackWepEffectScale", "bool"),
    ("pAttackSEName", "string"),
    ("attackSEDelay", "float"),
    ("attackBlurIndex", "int"),
    ("wakeupWait", "float"),
    ("isMove", "bool"),
    ("isHitStartDyeingUp", "bool"),
    ("hitDyeingUpColorR", "float"),
    ("hitDyeingUpColorG", "float"),
    ("hitDyeingUpColorB", "float"),
    ("hitDyeingUpColorA", "float"),
    ("hitDyeingUpFrame", "float"),
    ("hitCamera", "int"),
    ("hitDisp", "int"),
    ("pHitEffectName", "string"),
    ("pHitEffectNodeName", "string"),
    ("isHitEffectOffset", "bool"),
    ("hitEffectRotX", "float"),
    ("hitEffectRotY", "float"),
    ("hitEffectRotZ", "float"),
    ("hitEffectMoveType", "int"),
    ("hitEffectStartFrame", "float"),
    ("pHitSEName", "string"),
    ("hitSEDelay", "float"),
    ("isHitShake", "bool"),
    ("hitShakeX", "float"),
    ("hitShakeY", "float"),
    ("hitShakeZ", "float"),
    ("hitShakeCicleA", "float"),
    ("hitShakeCicleB", "float"),
    ("hitShakeX2", "float"),
    ("hitShakeY2", "float"),
    ("hitShakeZ2", "float"),
    ("hitShakeCicle2A", "float"),
    ("hitShakeCicle2B", "float"),
    ("hitShakeDuration", "float"),
    ("hitShakeWait", "float"),
    ("hitMessageWait", "float"),
    ("hitMessageTime", "float"),
    ("pHitMessage", "string"),
    ("hitDelay", "float"),
    ("hitBlurIndex", "int"),
    ("returnCamera", "int"),
    ("returnDisp", "int"),
    ("returnDelay", "float"),
    ("pReturnEffectName", "string"),
    ("pReturnEffectNodeName", "string"),
    ("isReturnEffectScale", "bool"),
    ("pReturnSEName", "string"),
    ("returnSEDelay", "float"),
    ("returnMessageWait", "float"),
    ("returnMessageTime", "float"),
    ("pReturnMessage", "string"),
    ("returnWait", "float"),
    ("endWait", "float"),
    ("allType", "int"),
    ("allWait", "float"),
    ("allCamera", "int"),
    ("allDisp", "int"),
]

# ── Additional schemas (auto-generated from dump.cs) ────────

SCHEMA_BtlAIData = [
    ("index", "int"), ("subIndex", "int"), ("abilityID", "int"),
    ("rate", "int"), ("useConfuse", "bool"), ("useCharm", "bool"),
    ("executeCondition", "int"), ("targetCondition", "int"),
]

SCHEMA_AllOneTableFormat = [("all", "int"), ("one", "int")]

SCHEMA_AttributeTableFormat = [("attr", "int"), ("regi", "int"), ("id", "int")]

STRUCT_BtlBlurInfo = [
    ("start", "float"), ("time", "float"), ("alpha", "float"), ("scale", "float"),
]

SCHEMA_BtlMagActBlur = [
    ("index", "int"), ("blurType", "int"), ("pNode", "string"),
    ("attachType", "int"), ("arrangePoint", "int"),
    ("screenX", "float"), ("screenY", "float"),
    ("info", ("array", STRUCT_BtlBlurInfo)),
]

# BadStatusData wrapper: outer class has nested StatusData class
SCHEMA_BtlBadStatusData_Inner = [
    ("cancelRate", "int[]"), ("pEffectName", "string"),
    ("pEffectNode", "string"), ("pSEName", "string"),
]
SCHEMA_BtlBadStatusData = [("data", SCHEMA_BtlBadStatusData_Inner)]

SCHEMA_BtlBalloonMessage = [
    ("index", "int"), ("friendship", "int"),
    ("window1", "int"), ("text1", "string"),
    ("window2", "int"), ("text2", "string"),
]

SCHEMA_BtlDifficultRate = [
    ("hp", "int"), ("attack", "int"), ("deffence", "int"),
    ("magicAttack", "int"), ("magicDeffence", "int"),
    ("agility", "int"), ("dexterity", "int"), ("intelligence", "int"),
    ("mind", "int"), ("hitRate", "int"), ("evasionRate", "int"),
    ("actSpeed", "int"), ("criticalRate", "int"),
]

SCHEMA_BtlEventData = [
    ("scriptID", "int"), ("eventID", "int"), ("conditionIdx", "int"),
    ("assist", "int"), ("target", "int[]"), ("abilityID", "int"),
    ("isNoChangeBGM", "bool"), ("limitNo", "int[]"),
]

SCHEMA_BtlEventMessageData = [
    ("index", "int"), ("pMessage", "string"), ("pVoice", "string"),
    ("wait", "float"), ("depth", "float"),
]

SCHEMA_BtlEventTalk = [
    ("index", "int"), ("pModelID", "string"), ("motionID", "int"),
]

SCHEMA_BtlGameFlagDefine = [("index", "int"), ("gameFlagIndex", "int")]

SCHEMA_BtlBuffLimit = [("max", "int"), ("min", "int")]

SCHEMA_BtlCameraData = [
    ("index", "int"), ("isAttachChara", "bool"), ("isCharaRot", "bool"),
    ("existSideChange", "bool"), ("existBiggestType", "bool"),
    ("depthLevel", "float"), ("isOffset", "bool"),
]

SCHEMA_BtlCorrectionData = [
    ("criticalDamagePercent", "int"), ("elementRegistCoeffFatal", "int"),
    ("elementRegistCoeffWeak", "int"), ("elementRegistCoeffHalfDamage", "int"),
    ("elementRegistCoeffNoDamage", "int"), ("berserkDamagePercent", "int"),
    ("guardDamagePercent", "int"), ("speciesAtkRate", "int"),
    ("footGripAtkRate", "int"), ("footGripDefRate", "int"),
    ("revenge", "int[]"), ("riskHeadgeRate", "int"), ("aspilRate", "int"),
    ("magSympathy", "int[]"), ("robRate", "int"), ("spiritBarrierRate", "int"),
    ("deadCountRate", "float"), ("buffUp", "int"), ("guardDefaultRate", "int"),
    ("statusBuff", "int"), ("statusShort", "int"), ("shadowCopyRate", "int"),
    ("poisonRatio", "int"),
]

SCHEMA_BtlTableFormat = [("item1", "int"), ("item2", "int"), ("combinedItem", "int")]

SCHEMA_BtlFriendShip = [
    ("levelExp", "int"), ("damageRate", "int"),
    ("statusRate", "int"), ("buffTurn", "int"),
]

SCHEMA_GoldCostTableFormat = [("goldcost", "float")]

# MGSEffect wrapper: outer class has nested NameData class
SCHEMA_BtlMGSEffect_NameData = [
    ("pWepEffectName", "string"), ("pHitEffectName1", "string"),
    ("pHitEffectName2", "string"), ("pRingEffectName", "string"),
    ("pRingSEName", "string"),
]
SCHEMA_BtlMGSEffect = [("data", SCHEMA_BtlMGSEffect_NameData)]

SCHEMA_BtlMapData = [
    ("index", "int"), ("useAntialias", "bool"), ("useBloom", "bool"),
    ("bloomThreshold", "float"), ("bloomIntensity", "float"),
    ("bloomExpansion", "float"), ("shadowColorR", "float"),
    ("shadowColorG", "float"), ("shadowColorB", "float"),
    ("shadowColorA", "float"),
]

SCHEMA_BtlMirroringTable = [
    ("jobID", "int"), ("abilityID", "int"),
    ("executeCondition", "int"), ("targetCondition", "int"),
]

SCHEMA_BtlMultiMagic = [("rate", "int[]")]

SCHEMA_BtlSpecialBGM = [("pBGMName", "string"), ("playTime", "float")]

SCHEMA_BtlSpecialCount = [("wepType", "int"), ("count", "int[]")]

SCHEMA_BtlSpecialVoice = [
    ("charaID", "int"), ("pVoiceName", "string"), ("specialType", "int"),
]

SCHEMA_StatusTableFormat = [("id", "int"), ("filename", "string")]

SCHEMA_TutorialMessageFormat = [("id_", "int"), ("message_", "string"), ("dialogtag_", "string")]

SCHEMA_BtlReinforceCharaData = [("index", "int"), ("charaID", "int")]

# RegistData wrapper: outer class has nested StatusData with just int[] cancelRate
SCHEMA_BtlElementRegist_Inner = [("cancelRate", "int[]")]
SCHEMA_BtlElementRegist = [("data", SCHEMA_BtlElementRegist_Inner)]

SCHEMA_BtlReverseEffect = [("pEffectName", "string")]

SCHEMA_BtlExperimentData = [
    ("itemID", "int"),
    ("data", ("array", STRUCT_BtlItemRate)),
]

SCHEMA_ExperimentFormat = [
    ("selectitem_", "int"), ("firstitem_", "int"), ("firstitemprobability_", "int"),
    ("seconditem_", "int"), ("seconditemprobability_", "int"),
    ("thirditem_", "int"), ("thirditemprobability_", "int"),
]

SCHEMA_BtlGatherData = [
    ("mapID", "int"),
    ("data", ("array", STRUCT_BtlItemRate)),
]

SCHEMA_BtlSmnData = [("data", "uint[]")]

# BAAData = BtlAtkActData with nested BtlAtkActOneData elements
SCHEMA_BtlAtkActOneData = [
    ("animIndex", "int"), ("pEffectName", "string"),
    ("effectDelay", "float"), ("pEffectNodeName", "string"),
    ("pSEName", "string"), ("seDelay", "float"),
    ("hitDelay", "float"), ("hitEffectRotX", "float"),
    ("hitEffectRotY", "float"), ("hitEffectRotZ", "float"),
]
SCHEMA_BtlAtkActData = [
    ("index", "int"), ("attackCamera", "int"), ("hitCamera", "int"),
    ("atkOneData", ("array", SCHEMA_BtlAtkActOneData)),
    ("endWait", "float"), ("multiHitType", "int"),
]

SCHEMA_MonsterParty = [
    ("index", "int"), ("num", "int"), ("type", "int"),
    ("pBGMName", "string"), ("pBGMNameLoop", "string"),
    ("monsterData", "int[]"),
]

SCHEMA_EncountTable = [
    ("EC_ID", "int"), ("INTERVAL", "int"),
    ("JUDGE_TBL", "int[]"),
    ("JUDGE_MIN", "int"), ("JUDGE_MAX", "int"), ("INTERVAL_MAX", "int"),
]

SCHEMA_BtlTransactionMessage = [("index", "int"), ("pMessage", "string")]

SCHEMA_BtlMessageDataSimple = [("index", "int"), ("pMessage", "string")]

# SMNBlurData: BtlSummonBlur with 4-float blur info elements
SCHEMA_BtlSmnBlurInfo = [
    ("f0", "float"), ("f1", "float"), ("f2", "float"), ("f3", "float"),
]
SCHEMA_BtlSmnActBlur = [
    ("blurType", "int"), ("pNode", "string"),
    ("screenX", "float"), ("screenY", "float"),
    ("info", ("array", SCHEMA_BtlSmnBlurInfo)),
]

# File → schema mapping
SBS_SCHEMAS = {
    "Tutorial_Index.tbl2": SCHEMA_BtlTutorialIndexInfo,
    "Tutorial_Index_CATLEXS.tbl2": SCHEMA_BtlTutorialIndexInfo,
    "Tutorial_Data.tbl2": SCHEMA_BtlTutorialPageData,
    "Tutorial_Data_CATLEXS.tbl2": SCHEMA_BtlTutorialPageData,
    "TutorialJob_Data.tbl2": SCHEMA_TutorialJobData,
    "MonsterData.btb2": SCHEMA_BtlMonsterData,
    "MonsterCMDABILData.btb2": SCHEMA_BtlMonsterCMDABILData,
    "BMAData.btb2": SCHEMA_BtlMagActData,
    # New schemas
    "AIData.btb2": SCHEMA_BtlAIData,
    "AllOneAbilityTable.btb2": SCHEMA_AllOneTableFormat,
    "AttrTable.btb2": SCHEMA_AttributeTableFormat,
    # BAAData = BtlAtkActData
    "BAAData.btb2": SCHEMA_BtlAtkActData,
    "BMABlurData.btb2": SCHEMA_BtlMagActBlur,
    "BadStatusData.btb2": SCHEMA_BtlBadStatusData,
    "BalloonMessage.btb2": SCHEMA_BtlBalloonMessage,
    "BtlDifficult.btb2": SCHEMA_BtlDifficultRate,
    "BtlEvent.btb2": SCHEMA_BtlEventData,
    "BtlEventMessage.btb2": SCHEMA_BtlEventMessageData,
    "BtlEventTalk.btb2": SCHEMA_BtlEventTalk,
    "BtlGameFlagDefine.btb2": SCHEMA_BtlGameFlagDefine,
    "BtlTransactionMessage.btb2": SCHEMA_BtlTransactionMessage,
    "BuffLimit.btb2": SCHEMA_BtlBuffLimit,
    "CameraData.btb2": SCHEMA_BtlCameraData,
    "CorrectionData.btb2": SCHEMA_BtlCorrectionData,
    "DrugCombine.btb2": SCHEMA_BtlTableFormat,
    "EncountTable.btb2": SCHEMA_EncountTable,
    "FriendShip.btb2": SCHEMA_BtlFriendShip,
    "LevelGoldCostTable.btb2": SCHEMA_GoldCostTableFormat,
    "MGSEffect.btb2": SCHEMA_BtlMGSEffect,
    "MapData.btb2": SCHEMA_BtlMapData,
    "MessageData.btb2": SCHEMA_BtlMessageDataSimple,
    "MirroringTable.btb2": SCHEMA_BtlMirroringTable,
    # MonsterParty: complex nested structure (172 bytes/record), schema TBD
    "MultiMagicData.btb2": SCHEMA_BtlMultiMagic,
    "RegistData.btb2": SCHEMA_BtlElementRegist,
    "ReinforceList.btb2": SCHEMA_BtlReinforceCharaData,
    "ReverseEffect.btb2": SCHEMA_BtlReverseEffect,
    "SMNBlurData.btb2": SCHEMA_BtlSmnActBlur,
    "SMNData.btb2": SCHEMA_BtlSmnData,
    "SpecialBGM.btb2": SCHEMA_BtlSpecialBGM,
    "SpecialCount.btb2": SCHEMA_BtlSpecialCount,
    "SpecialVoice.btb2": SCHEMA_BtlSpecialVoice,
    "StatusTable.btb2": SCHEMA_StatusTableFormat,
    "TutorialMessage.btb2": SCHEMA_TutorialMessageFormat,
    "experiment.btb2": SCHEMA_BtlExperimentData,
    "experiment_aho.btb2": SCHEMA_ExperimentFormat,
    "gather.btb2": SCHEMA_BtlGatherData,
}


def auto_detect_schema(filepath):
    """Detect schema from filename."""
    name = Path(filepath).name
    schema = SBS_SCHEMAS.get(name)
    if schema is None:
        return None
    return schema


# ═══════════════════════════════════════════════════════════════
# Writer
# ═══════════════════════════════════════════════════════════════

def write_7bit_int(stream: io.BytesIO, value: int):
    """Write a 7-bit encoded integer (same as BinaryWriter.Write7BitEncodedInt)."""
    while value > 0x7F:
        stream.write(bytes([value & 0x7F | 0x80]))
        value >>= 7
    stream.write(bytes([value & 0x7F]))


def write_string(stream: io.BytesIO, s: str):
    """Write a BinaryWriter-format string (7-bit length + UTF-8 bytes)."""
    encoded = s.encode("utf-8")
    write_7bit_int(stream, len(encoded))
    stream.write(encoded)


def write_int32(stream: io.BytesIO, v: int):
    stream.write(struct.pack("<i", v))


def write_uint32(stream: io.BytesIO, v: int):
    stream.write(struct.pack("<I", v))


def write_float(stream: io.BytesIO, v):
    stream.write(struct.pack("<f", v))


def write_bool(stream: io.BytesIO, v: bool):
    stream.write(b'\x01' if v else b'\x00')


class SBSWriter:
    def __init__(self):
        self.stream = io.BytesIO()

    def write_nullable_marker(self, is_not_null: bool):
        self.stream.write(b'\x01' if is_not_null else b'\x00')

    def write_int_array(self, arr):
        if arr is None:
            self.write_nullable_marker(False)
            return
        self.write_nullable_marker(True)
        write_7bit_int(self.stream, len(arr))
        for v in arr:
            write_int32(self.stream, v)

    def write_uint_array(self, arr):
        if arr is None:
            self.write_nullable_marker(False)
            return
        self.write_nullable_marker(True)
        write_7bit_int(self.stream, len(arr))
        for v in arr:
            write_uint32(self.stream, v)

    def write_object_array(self, arr, schema):
        if arr is None:
            self.write_nullable_marker(False)
            return
        self.write_nullable_marker(True)
        write_7bit_int(self.stream, len(arr))
        for elem in arr:
            if elem is None:
                self.write_nullable_marker(False)
            else:
                self.write_nullable_marker(True)
                self._write_struct(elem, schema)

    def write_nested_object(self, obj, schema):
        if obj is None:
            self.write_nullable_marker(False)
            return
        self.write_nullable_marker(True)
        self._write_struct(obj, schema)

    def _write_struct(self, rec, schema):
        for name, ftype in schema:
            val = rec[name]
            if ftype == "int":
                write_int32(self.stream, val)
            elif ftype == "uint":
                write_uint32(self.stream, val)
            elif ftype == "float":
                write_float(self.stream, val)
            elif ftype == "bool":
                write_bool(self.stream, val)
            elif ftype == "string":
                write_string(self.stream, val)
            elif ftype == "int[]":
                self.write_int_array(val)
            elif ftype == "uint[]":
                self.write_uint_array(val)
            elif isinstance(ftype, list):
                self.write_nested_object(val, ftype)
            elif isinstance(ftype, tuple) and ftype[0] == "array":
                self.write_object_array(val, ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "struct":
                self._write_struct(val, ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "struct_array":
                self.write_struct_array(val, ftype[1])
            elif isinstance(ftype, tuple) and ftype[0] == "float_array":
                self.write_float_array(val)

    def write_struct_array(self, arr, schema):
        if arr is None:
            self.write_nullable_marker(False)
            return
        self.write_nullable_marker(True)
        write_7bit_int(self.stream, len(arr))
        for elem in arr:
            self._write_struct(elem, schema)

    def write_float_array(self, arr):
        if arr is None:
            self.write_nullable_marker(False)
            return
        self.write_nullable_marker(True)
        write_7bit_int(self.stream, len(arr))
        for v in arr:
            write_float(self.stream, v)

    def write_record(self, rec, schema):
        self._write_struct(rec, schema)

    def write_all(self, records, schema) -> bytes:
        """Write top-level: [NOT_NULL] [count] then per-record: [NOT_NULL] [fields...]"""
        self.write_nullable_marker(True)
        write_7bit_int(self.stream, len(records))
        for rec in records:
            if rec is None:
                self.write_nullable_marker(False)
            else:
                self.write_nullable_marker(True)
                self.write_record(rec, schema)
        return self.stream.getvalue()


def read_sbs(filepath):
    """Read an SBS file → (records, schema, raw_decompressed)."""
    data = Path(filepath).read_bytes()
    # Header: 00 01 53 53
    if data[2:4] != b'SS':
        raise ValueError(f"Not an SBS file (expected SS magic): {filepath}")
    decompressed = brotli.decompress(data[4:])
    schema = auto_detect_schema(filepath)
    if not schema:
        raise ValueError(f"No schema for: {Path(filepath).name}")
    reader = SBSReader(decompressed)
    records = reader.read_all(schema)
    remaining = len(decompressed) - reader.stream.tell()
    return records, schema, remaining


def write_sbs(records, schema, filepath):
    """Write records → SBS file (Brotli compressed)."""
    writer = SBSWriter()
    body = writer.write_all(records, schema)
    compressed = brotli.compress(body)
    header = b'\x00\x01SS'
    Path(filepath).write_bytes(header + compressed)
    return len(header) + len(compressed)


def sbs_to_json(records, schema):
    """Convert records + schema name to a JSON-serializable dict."""
    # Find schema name
    schema_name = None
    for k, v in SBS_SCHEMAS.items():
        if v is schema:
            schema_name = k
            break
    return {
        "_schema": schema_name,
        "records": records,
    }


def json_to_sbs(data):
    """Parse JSON dict back to (records, schema)."""
    schema_name = data.get("_schema")
    if not schema_name or schema_name not in SBS_SCHEMAS:
        raise ValueError(f"Unknown or missing _schema: {schema_name}")
    schema = SBS_SCHEMAS[schema_name]
    return data["records"], schema


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def cmd_info(args):
    data = Path(args.file).read_bytes()
    print(f"File: {args.file} ({len(data):,} bytes)")
    print(f"Header: {data[:4].hex(' ')}")
    decompressed = brotli.decompress(data[4:])
    print(f"Decompressed: {len(decompressed):,} bytes")
    schema = auto_detect_schema(args.file)
    if schema:
        reader = SBSReader(decompressed)
        records = reader.read_all(schema)
        remaining = len(decompressed) - reader.stream.tell()
        print(f"Schema: {Path(args.file).name}")
        print(f"Records: {len(records)}")
        print(f"Fields per record: {len(schema)}")
        print(f"Remaining bytes: {remaining}")
    else:
        print(f"Schema: UNKNOWN")


def cmd_dump(args):
    records, schema, remaining = read_sbs(args.file)
    obj = sbs_to_json(records, schema)
    out = args.output or args.file.rsplit('.', 1)[0] + '.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    print(f"Dumped {len(records)} records to {out} ({remaining} bytes remaining)")


def cmd_import(args):
    with open(args.json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    records, schema = json_to_sbs(data)
    sz = write_sbs(records, schema, args.output)
    print(f"Wrote {len(records)} records to {args.output} ({sz:,} bytes)")


def cmd_batch_dump(args):
    base = Path(args.directory)
    out_dir = Path(args.output) if args.output else Path("dump") / "sbs"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = list(base.rglob("*.btb2")) + list(base.rglob("*.tbl2"))
    dumped = 0
    for fp in sorted(files):
        schema = auto_detect_schema(fp)
        if not schema:
            print(f"  SKIP (no schema): {fp.name}")
            continue
        try:
            records, schema, remaining = read_sbs(fp)
            obj = sbs_to_json(records, schema)
            rel = fp.relative_to(base)
            out_path = out_dir / rel.with_suffix('.json')
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
            print(f"  {rel} → {len(records)} records ({remaining} remaining)")
            dumped += 1
        except Exception as e:
            print(f"  ERROR {fp.name}: {e}")

    print(f"\nDumped {dumped} files to {out_dir}")


def main():
    p = argparse.ArgumentParser(description="BDFFHD SBS (.btb2/.tbl2) tool")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("info", help="Show file info")
    s.add_argument("file")

    s = sub.add_parser("dump", help="Dump to JSON")
    s.add_argument("file")
    s.add_argument("output", nargs="?")

    s = sub.add_parser("import", help="Import from JSON")
    s.add_argument("json_file")
    s.add_argument("output")

    s = sub.add_parser("batch-dump", help="Dump all SBS files from a directory")
    s.add_argument("directory")
    s.add_argument("output", nargs="?")

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return

    {"info": cmd_info, "dump": cmd_dump, "import": cmd_import,
     "batch-dump": cmd_batch_dump}[args.command](args)


if __name__ == "__main__":
    main()
