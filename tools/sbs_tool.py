#!/usr/bin/env python3
"""
Decode SimpleBinarySerializer (SBS) format used by .btb2 / .tbl2 files.

Wire format (reverse-engineered from hex dumps):
  File header: [0x00] [0x01=COMPRESS_BROTLI] [0x53 0x53 = "SS"]
  Body (after brotli decompression):
    [0x01] array_count_byte  <-- NOT_NULL marker (0x01) then record count as 7-bit encoded int
    Then for each record, fields are written IN DECLARATION ORDER:
      - int/float:   4 bytes little-endian
      - bool:        1 byte (0x00 or 0x01)  
      - string:      [length as 7-bit encoded int] [UTF-8 bytes] (0x00 for null? or length=0)
      - int[]:       [0x01=NOT_NULL] [count as 7-bit encoded int] [count * 4 bytes]
      - object[]:    [0x01=NOT_NULL] [count as 7-bit encoded int] [fields per element...]

BinaryWriter.Write7BitEncodedInt format:
  Values 0-127: 1 byte
  Values 128-16383: 2 bytes (low 7 bits | 0x80, high 7 bits)
  etc.

BinaryWriter string format: length-prefixed UTF-8.
"""
import brotli, struct, io
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
                # Nested struct/class — could be single object or array
                # Check if the name convention suggests array (plural/Data suffix)
                # Actually, we need to distinguish: SBS writes class refs with NOT_NULL marker
                # For a single nested class: [NOT_NULL] [fields...]
                # For an array of classes: [NOT_NULL] [count] [per-elem: [NOT_NULL] [fields...]]
                # We mark arrays by wrapping in a tuple: ("name", ("array", schema))
                rec[name] = self._read_nested_object(ftype)
            elif isinstance(ftype, tuple) and ftype[0] == "array":
                rec[name] = self._read_object_array(ftype[1])
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
        return rec

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
}


# ═══════════════════════════════════════════════════════════════
# Test
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    base = Path(r"C:\Program Files (x86)\Steam\steamapps\common\BDFFHD\BDFFHD_Data\StreamingAssets\Common_en")

    tests = [
        ("TutorialTable/Tutorial_Index.tbl2", SCHEMA_BtlTutorialIndexInfo),
        ("TutorialTable/TutorialJob_Data.tbl2", SCHEMA_TutorialJobData),
        ("TutorialTable/Tutorial_Data.tbl2", SCHEMA_BtlTutorialPageData),
        ("TutorialTable/Tutorial_Data_CATLEXS.tbl2", SCHEMA_BtlTutorialPageData),
        ("Battle/MonsterData.btb2", SCHEMA_BtlMonsterData),
        ("Battle/MonsterCMDABILData.btb2", SCHEMA_BtlMonsterCMDABILData),
        ("Battle/BMAData.btb2", SCHEMA_BtlMagActData),
    ]

    for relpath, schema in tests:
        fp = base / relpath
        data = fp.read_bytes()
        dec = brotli.decompress(data[4:])
        reader = SBSReader(dec)
        print(f"\n{'='*70}")
        print(f"{relpath} ({len(data)} -> {len(dec)} bytes)")
        print(f"{'='*70}")
        records = reader.read_all(schema)
        remaining = len(dec) - reader.stream.tell()
        print(f"Parsed {len(records)} records, {remaining} bytes remaining")
        if records:
            print(f"First record: {json.dumps(records[0], indent=2, ensure_ascii=False)}")
            if len(records) > 1:
                print(f"Last record: {json.dumps(records[-1], indent=2, ensure_ascii=False)}")
