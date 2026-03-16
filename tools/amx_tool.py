#!/usr/bin/env python3
"""
amx_tool.py — Parser/Disassembler for BDFFHD .amx (Pawn AMX v10) script files.

AMX is the compiled bytecode format for the Pawn scripting language.
All BDFFHD AMX files use compact encoding (flag 0x04).

The file layout:
  - Header (56 bytes): size, magic, versions, flags, section offsets
  - Tables region (publics, natives, libraries, pubvars, tags): 8 bytes per entry
  - Nametable: uint16 length prefix + null-terminated ASCII strings
  - Code section: compact-encoded Pawn opcodes (between nametable and file end)

Note: cod/dat/hea/stp in the header are EXPANDED (post-decompression) offsets,
not file offsets. The actual code starts right after the nametable.

Usage:
    python amx_tool.py info <file.amx>                    # Show header & tables
    python amx_tool.py dump <file.amx> [-o output.json]   # Dump to JSON
    python amx_tool.py disasm <file.amx>                  # Disassemble code
    python amx_tool.py batch-dump <directory> [-o outdir]  # Dump all .amx files
"""
import struct
import json
import sys
import os
import argparse
from pathlib import Path
from dataclasses import dataclass, field

HEADER_SIZE = 56
AMX_MAGIC = 0xF1E0

# BDFFHD uses a SHUFFLED opcode table (not standard Pawn numbering).
# Mapping confirmed via DLL execution testing + calling convention analysis.
# The compact encoding uses big-endian LEB128 (confirmed by Pawn.dll).

# Known shuffled opcode → name mapping
SHUFFLED_OPCODES = {
    # --- Confirmed by DLL execution + calling convention analysis ---
    46: "PROC",          # Standard 34. 0-arg. 100% confirmed (404K functions)
    48: "RETN",          # Standard 36. 0-arg. 92% confirmed
    44: "STACK",         # Standard 32. 1-arg. Confirmed via calling convention
    89: "ZERO.pri",      # 0-arg. Execution returns 0. Before 45% of RETNs
    39: "PUSH.C",        # Standard 27. 1-arg. Confirmed: call convention in hello.amx
    123: "SYSREQ.C",     # Standard 78. 1-arg. Confirmed: native call in hello.amx
    53: "SYSREQ.N",      # Standard 120. 2-arg. Native call with arg count (2.60%)
    120: "HALT",         # Standard 75. 1-arg. At program start: HALT 0
    137: "BREAK",        # Standard 124. 0-arg. Only in debug-compiled scripts
    133: "PUSH.S",       # Standard 29. 1-arg. Confirmed: push local before native call
    14: "STOR.S",        # Standard 14. 1-arg. Confirmed: ZERO.pri + STOR.S pattern
}

# Opcodes that take 1 argument (operand follows the opcode cell).
# Only includes opcodes with >70% operand-like followers in pair analysis.
SHUFFLED_1ARG = {
    44, 39, 123, 120, 133, 14,  # confirmed by calling convention
    11,   # 96% operand-like, 124K occurrences
    49,   # 93% operand-like, 576K
    78,   # 80% operand-like, 558K
    32,   # 80% operand-like, 86K
    24,   # 76% operand-like, 65K
    91,   # 72% operand-like, 40K
    119,  # 79% operand-like, 46K
    138,  # 76% operand-like, 26K
    157,  # 78% operand-like, 54K
    2,    # 70% operand-like, 49K
    156,  # 68% operand-like, 31K
}

# Opcodes that take 2 arguments
SHUFFLED_2ARG = {53}  # SYSREQ.N: native_index, num_args

# Cells that are ALWAYS opcodes and must NEVER be consumed as operands.
# This prevents misparses where a 1-arg opcode would eat a PROC/RETN/STACK.
OPCODE_BARRIER = {46, 48, 44, 137, 89}  # PROC, RETN, STACK, BREAK, ZERO.pri
# Standard Pawn AMX v10 opcode table (for reference only — NOT used for BDFFHD)
STD_OPCODES = {
    0: "NOP", 1: "LOAD.pri", 2: "LOAD.alt", 3: "LOAD.S.pri", 4: "LOAD.S.alt",
    5: "LREF.S.pri", 6: "LREF.S.alt", 7: "LOAD.I", 8: "LODB.I", 9: "CONST.pri",
    10: "CONST.alt", 11: "ADDR.pri", 12: "ADDR.alt", 13: "STOR", 14: "STOR.S",
    15: "SREF.S", 16: "STOR.I", 17: "STRB.I", 18: "ALIGN.pri", 19: "ALIGN.alt",
    20: "LCTRL", 21: "SCTRL", 22: "XCHG", 23: "PUSH.pri", 24: "PUSH.alt",
    25: "PUSHR.pri", 26: "PICK", 27: "PUSH.C", 28: "PUSH", 29: "PUSH.S",
    30: "POP.pri", 31: "POP.alt", 32: "STACK", 33: "HEAP", 34: "PROC",
    35: "RET", 36: "RETN", 37: "CALL", 38: "JUMP", 39: "JZER", 40: "JNZ",
    41: "SHL", 42: "SHR", 43: "SSHR", 44: "SHL.C.pri", 45: "SHL.C.alt",
    46: "SMUL", 47: "SDIV", 48: "ADD", 49: "SUB", 50: "AND", 51: "OR",
    52: "XOR", 53: "NOT", 54: "NEG", 55: "INVERT", 56: "EQ", 57: "NEQ",
    58: "SLESS", 59: "SLEQ", 60: "SGRTR", 61: "SGEQ", 62: "INC.pri",
    63: "INC.alt", 64: "INC", 65: "INC.S", 66: "INC.I", 67: "DEC.pri",
    68: "DEC.alt", 69: "DEC", 70: "DEC.S", 71: "DEC.I", 72: "MOVS",
    73: "CMPS", 74: "FILL", 75: "HALT", 76: "BOUNDS", 77: "SYSREQ.pri",
    78: "SYSREQ.C", 79: "FILE", 80: "LINE", 81: "SYMBOL", 82: "SRANGE",
    83: "SYMTAG", 84: "JUMP.pri", 85: "SWITCH", 86: "SWAP.pri", 87: "SWAP.alt",
    88: "PUSH.ADR", 119: "CASETBL", 120: "SYSREQ.N", 124: "BREAK",
}

# Keep backward compatibility aliases
OPCODES = STD_OPCODES
OPCODES_WITH_ARG = {
    1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 17, 18, 19,
    20, 21, 26, 27, 28, 29, 32, 33, 37, 38, 39, 40, 44, 45,
    64, 65, 69, 70, 72, 73, 74, 75, 76, 78, 80, 82, 83, 85, 88,
}
OPCODES_WITH_2ARGS = {120}
OPCODES_CASETBL = {119}


@dataclass
class AMXHeader:
    size: int
    magic: int
    file_version: int
    amx_version: int
    flags: int
    defsize: int
    cod: int       # expanded code offset
    dat: int       # expanded data offset
    hea: int       # initial heap
    stp: int       # stack top
    cip: int       # initial code instruction pointer
    publics: int   # offset to publics table
    natives: int   # offset to natives table
    libraries: int # offset to libraries table
    pubvars: int   # offset to public variables table
    tags: int      # offset to tags table
    nametable: int # offset to name table


@dataclass
class DefEntry:
    value: int    # address (publics) or index (natives) etc.
    name: str


@dataclass
class AMXFile:
    header: AMXHeader
    publics: list    # list of DefEntry
    natives: list    # list of DefEntry
    libraries: list  # list of DefEntry
    pubvars: list    # list of DefEntry
    tags: list       # list of DefEntry
    nametable_raw: bytes  # raw nametable bytes (for roundtrip)
    code_raw: bytes       # raw code section bytes (compact-encoded, for roundtrip)
    raw_data: bytes       # entire file bytes (for perfect roundtrip)


def read_name(data: bytes, offset: int) -> str:
    """Read null-terminated ASCII string at absolute file offset."""
    if offset >= len(data):
        return ""
    end = data.find(0, offset)
    if end < 0 or end > offset + 4096:
        end = min(offset + 4096, len(data))
    return data[offset:end].decode("ascii", errors="replace")


def read_amx(filepath: str) -> AMXFile:
    """Read an AMX file."""
    with open(filepath, "rb") as f:
        data = f.read()

    h = AMXHeader(
        size=struct.unpack_from("<i", data, 0)[0],
        magic=struct.unpack_from("<H", data, 4)[0],
        file_version=data[6],
        amx_version=data[7],
        flags=struct.unpack_from("<H", data, 8)[0],
        defsize=struct.unpack_from("<H", data, 10)[0],
        cod=struct.unpack_from("<i", data, 12)[0],
        dat=struct.unpack_from("<i", data, 16)[0],
        hea=struct.unpack_from("<i", data, 20)[0],
        stp=struct.unpack_from("<i", data, 24)[0],
        cip=struct.unpack_from("<i", data, 28)[0],
        publics=struct.unpack_from("<i", data, 32)[0],
        natives=struct.unpack_from("<i", data, 36)[0],
        libraries=struct.unpack_from("<i", data, 40)[0],
        pubvars=struct.unpack_from("<i", data, 44)[0],
        tags=struct.unpack_from("<i", data, 48)[0],
        nametable=struct.unpack_from("<i", data, 52)[0],
    )

    if h.magic != AMX_MAGIC:
        raise ValueError(f"Bad AMX magic: 0x{h.magic:04X} (expected 0x{AMX_MAGIC:04X})")

    defsize = h.defsize

    def read_table(start, end):
        entries = []
        count = (end - start) // defsize
        for i in range(count):
            off = start + i * defsize
            val, nameofs = struct.unpack_from("<II", data, off)
            name = read_name(data, nameofs)
            entries.append(DefEntry(val, name))
        return entries

    publics = read_table(h.publics, h.natives)
    natives = read_table(h.natives, h.libraries)
    libraries = read_table(h.libraries, h.pubvars)
    pubvars = read_table(h.pubvars, h.tags)
    tags = read_table(h.tags, h.nametable)

    # Nametable: starts at h.nametable, has uint16 namemax prefix
    # Name strings fill the space between nametable and cod.
    nametable_raw = data[h.nametable:h.cod]
    # Code section starts at file offset cod (compact-encoded if COMPACT flag set).
    # For compact files, data[cod:size] contains both code and data sections
    # that expand to (hea - cod) bytes via big-endian LEB128 decoding.
    code_raw = data[h.cod:]

    return AMXFile(
        header=h,
        publics=publics,
        natives=natives,
        libraries=libraries,
        pubvars=pubvars,
        tags=tags,
        nametable_raw=nametable_raw,
        code_raw=code_raw,
        raw_data=data,
    )


def write_amx(amx: AMXFile, filepath: str):
    """Write an AMX file (perfect byte-level roundtrip via raw_data)."""
    with open(filepath, "wb") as f:
        f.write(amx.raw_data)


# ─── Compact Encoding (Big-Endian LEB128) ────────────────────────────────

def compact_decode(raw: bytes) -> list:
    """Decode BDFFHD compact encoding (big-endian LEB128) into signed 32-bit cells.

    BDFFHD's Pawn VM uses MSB-first variable-length encoding where:
    - Bit 7 of each byte is the continuation flag (1 = more bytes follow)
    - Bits 6-0 carry the value (MSB first)
    - Bit 6 of the FIRST byte is the sign bit (sign-extended)
    """
    cells = []
    pos = 0
    n = len(raw)
    while pos < n:
        byte_list = []
        while pos < n:
            b = raw[pos]; pos += 1
            byte_list.append(b)
            if not (b & 0x80):
                break
        c = 0
        for b in byte_list:
            c = (c << 7) | (b & 0x7f)
        total_bits = len(byte_list) * 7
        if byte_list[0] & 0x40:
            if total_bits < 32:
                c |= (~0 & 0xFFFFFFFF) << total_bits
        c &= 0xFFFFFFFF
        if c >= 0x80000000:
            c -= 0x100000000
        cells.append(c)
    return cells


def compact_encode(cells: list) -> bytes:
    """Encode signed 32-bit cells into BDFFHD compact encoding (big-endian LEB128).

    Reverse of compact_decode(). For each cell:
    - Determine minimum number of 7-bit groups needed
    - Bit 6 of the first byte must match the sign (set for negative)
    - Emit bytes MSB-first with bit 7 = continuation flag
    """
    result = bytearray()
    for cell in cells:
        v = cell & 0xFFFFFFFF  # unsigned 32-bit
        negative = cell < 0

        # Find minimum number of bytes
        n_bytes = 1
        for n_bytes in range(1, 6):
            total_bits = n_bytes * 7
            if total_bits >= 32:
                truncated = v
            else:
                truncated = v & ((1 << total_bits) - 1)

            # Check sign bit (bit 6 of first byte = MSB of value portion)
            first_byte_val = truncated >> ((n_bytes - 1) * 7)
            sign_bit = bool(first_byte_val & 0x40)

            if negative and sign_bit:
                # Verify sign extension reproduces the value
                if total_bits >= 32:
                    break
                extended = (truncated | (0xFFFFFFFF << total_bits)) & 0xFFFFFFFF
                if extended == v:
                    break
            elif not negative and not sign_bit:
                # Verify value fits
                if truncated == v:
                    break

        # Extract 7-bit groups, LSB first then reverse for MSB-first
        groups = []
        temp = truncated
        for _ in range(n_bytes):
            groups.append(temp & 0x7F)
            temp >>= 7
        groups.reverse()

        # Set continuation bits on all but last byte
        for i in range(len(groups) - 1):
            groups[i] |= 0x80

        result.extend(groups)
    return bytes(result)


def expand_amx(amx: AMXFile) -> tuple:
    """Expand compact-encoded code+data into (code_cells, data_cells).

    Returns (code_cells: list[int], data_cells: list[int]).
    code_cells are the Pawn bytecode instructions + operands.
    data_cells are the initialized data segment.
    """
    h = amx.header
    all_cells = compact_decode(amx.code_raw)
    n_code = (h.dat - h.cod) // 4
    n_data = (h.hea - h.dat) // 4
    expected = n_code + n_data
    if len(all_cells) != expected:
        raise ValueError(
            f"Compact decode mismatch: got {len(all_cells)} cells, "
            f"expected {expected} (code={n_code} + data={n_data})"
        )
    return all_cells[:n_code], all_cells[n_code:]


# ─── Disassembler ─────────────────────────────────────────────────────────

def disassemble(amx: AMXFile) -> dict:
    """Disassemble an AMX file into structured output.

    Returns a dict with:
      - functions: list of {name, addr, instructions: [{addr, cells, text}]}
      - data: {cells: [...], strings: [...]}
      - entry_point: cell address of cip
    """
    h = amx.header
    code_cells, data_cells = expand_amx(amx)

    # Build lookup tables
    native_names = {i: e.name for i, e in enumerate(amx.natives)}

    # Map byte-address → public name (addresses are relative to cod)
    pub_by_addr = {}
    for e in amx.publics:
        pub_by_addr[e.value] = e.name

    # Sort public addresses to determine function boundaries
    pub_addrs = sorted(pub_by_addr.keys())

    # Also add CIP as a known entry if not already a public
    if h.cip >= 0 and h.cip not in pub_by_addr:
        pub_by_addr[h.cip] = "__entry__"
        pub_addrs = sorted(pub_by_addr.keys())

    # Build instruction list with opcode+operand grouping
    # Each cell is 4 bytes in expanded form, addresses are byte offsets from cod
    n_natives = len(amx.natives)
    code_size = len(code_cells) * 4  # total code bytes

    def _fmt_arg(op_label, arg):
        """Format an instruction's text given its label and argument."""
        if arg < 0:
            return f"{op_label} {arg}"
        if arg > 200:
            return f"{op_label} {arg}  ; 0x{arg:X}"
        return f"{op_label} {arg}"

    instructions = []
    i = 0
    while i < len(code_cells):
        addr = i * 4
        cell = code_cells[i]
        label = pub_by_addr.get(addr)
        opname = SHUFFLED_OPCODES.get(cell)
        consumed = False

        # --- 2-arg opcodes ---
        if cell in SHUFFLED_2ARG and i + 2 < len(code_cells):
            arg1, arg2 = code_cells[i + 1], code_cells[i + 2]
            valid = True
            if cell == 53 and (arg1 < 0 or arg1 >= n_natives):
                valid = False
            if arg1 in OPCODE_BARRIER or arg2 in OPCODE_BARRIER:
                valid = False
            if valid:
                if cell == 53:
                    text = f"SYSREQ.N {native_names.get(arg1, f'native_{arg1}')}, {arg2}"
                else:
                    text = f"{opname or f'op_{cell}'} {arg1}, {arg2}"
                instructions.append({"addr": addr, "cell": cell, "label": label, "text": text})
                i += 3
                consumed = True

        # --- 1-arg opcodes ---
        if not consumed and cell in SHUFFLED_1ARG and i + 1 < len(code_cells):
            arg = code_cells[i + 1]
            valid = True
            if arg in OPCODE_BARRIER:
                valid = False
            if cell == 123 and (arg < 0 or arg >= n_natives):
                valid = False
            if valid:
                ol = opname or f"op_{cell}"
                if cell == 123:
                    text = f"SYSREQ.C {native_names.get(arg, f'native_{arg}')}"
                elif cell in (78,):
                    text = f"{ol} 0x{arg & 0xFFFFFFFF:04X}"
                else:
                    text = _fmt_arg(ol, arg)
                instructions.append({"addr": addr, "cell": cell, "label": label, "text": text})
                i += 2
                consumed = True

        # --- 0-arg named opcodes ---
        if not consumed and opname and cell not in SHUFFLED_1ARG and cell not in SHUFFLED_2ARG:
            instructions.append({"addr": addr, "cell": cell, "label": label, "text": opname})
            i += 1
            consumed = True

        # --- Unknown / raw cell ---
        if not consumed:
            text = str(cell)
            if cell < 0:
                text = f"{cell}  ; 0x{cell & 0xFFFFFFFF:08X}"
            elif cell > 200:
                text = f"{cell}  ; 0x{cell:X}"
            instructions.append({"addr": addr, "cell": cell, "label": label, "text": text})
            i += 1

    # Group instructions into functions based on PROC (46) boundaries
    functions = []
    current_func = None
    unlabeled_idx = 0

    for instr in instructions:
        # Start a new function when we hit PROC (46)
        if instr["cell"] == 46:
            if current_func:
                functions.append(current_func)
            name = instr["label"]
            if not name:
                name = f"__sub_{instr['addr']:04X}__"
                unlabeled_idx += 1
            current_func = {
                "name": name,
                "addr": f"0x{instr['addr']:04X}",
                "instructions": [],
            }
            current_func["instructions"].append({
                "addr": f"0x{instr['addr']:04X}",
                "text": instr["text"],
            })
        elif current_func is not None:
            entry = {"addr": f"0x{instr['addr']:04X}", "text": instr["text"]}
            if instr["label"]:
                entry["label"] = instr["label"]
            current_func["instructions"].append(entry)
        else:
            # Before first PROC — preamble/init code
            if current_func is None:
                current_func = {
                    "name": "__preamble__",
                    "addr": f"0x{instr['addr']:04X}",
                    "instructions": [],
                }
            entry = {"addr": f"0x{instr['addr']:04X}", "text": instr["text"]}
            if instr["label"]:
                entry["label"] = instr["label"]
            current_func["instructions"].append(entry)

    if current_func:
        functions.append(current_func)

    # Decode data section — extract strings
    data_strings = []
    data_values = [c & 0xFFFFFFFF for c in data_cells]
    # Scan for null-terminated ASCII strings in data cells
    i = 0
    while i < len(data_cells):
        # Check if this looks like the start of a string (printable ASCII or common escapes)
        if 0x20 <= (data_cells[i] & 0xFF) <= 0x7E or data_cells[i] in (0x0A, 0x0D, 0x09):
            chars = []
            j = i
            while j < len(data_cells) and data_cells[j] != 0:
                ch = data_cells[j] & 0xFF
                if 0x20 <= ch <= 0x7E or ch in (0x0A, 0x0D, 0x09):
                    chars.append(chr(ch))
                else:
                    break
                j += 1
            if j < len(data_cells) and data_cells[j] == 0 and len(chars) >= 1:
                data_strings.append({
                    "offset": f"0x{i * 4:04X}",
                    "text": "".join(chars),
                })
                i = j + 1
                continue
        i += 1

    return {
        "entry_point": f"0x{h.cip:04X}" if h.cip >= 0 else None,
        "code_cells": len(code_cells),
        "data_cells": len(data_cells),
        "functions": functions,
        "data_strings": data_strings,
        "data_values": data_values,
    }


# ─── Assembler ────────────────────────────────────────────────────────────

# Reverse lookup: opcode name → shuffled opcode number
SHUFFLED_OPCODES_REV = {v: k for k, v in SHUFFLED_OPCODES.items()}


def parse_instruction(text: str, native_by_name: dict) -> list:
    """Parse a disassembly instruction text line back to a list of cell values.

    Handles all formats produced by disassemble():
      "PROC"                    → [46]
      "STACK 16"                → [44, 16]
      "PUSH.C 5"                → [39, 5]
      "PUSH.S -8"               → [133, -8]
      "STOR.S 12  ; 0xC"        → [14, 12]
      "SYSREQ.C printf"         → [123, index]
      "SYSREQ.N printf, 2"      → [53, index, 2]
      "op_11 128"               → [11, 128]
      "op_78 0x003C"            → [78, 0x3C]
      "42  ; 0x0000002A"        → [42]      (raw cell)
      "-256  ; 0xFFFFFF00"      → [-256]    (raw cell)

    Args:
        text: instruction text from disassembly JSON
        native_by_name: dict mapping native name → index
    Returns:
        list of signed 32-bit cell values
    """
    # Strip trailing comment
    if ";" in text:
        text = text[:text.index(";")].rstrip()
    text = text.strip()
    if not text:
        return [0]

    parts = text.split()
    mnemonic = parts[0]

    # Try resolving mnemonic to opcode number
    opcode = None
    if mnemonic in SHUFFLED_OPCODES_REV:
        opcode = SHUFFLED_OPCODES_REV[mnemonic]
    elif mnemonic.startswith("op_"):
        try:
            opcode = int(mnemonic[3:])
        except ValueError:
            pass

    # If no mnemonic matched, this is a raw cell value
    if opcode is None:
        try:
            if mnemonic.startswith("0x") or mnemonic.startswith("0X"):
                v = int(mnemonic, 16)
            else:
                v = int(mnemonic)
            if v > 0x7FFFFFFF:
                v -= 0x100000000
            return [v]
        except ValueError:
            raise ValueError(f"Cannot parse instruction: {text!r}")

    # Parse arguments
    if len(parts) == 1:
        # 0-arg opcode
        return [opcode]

    # Join remaining parts and split by comma for multi-arg opcodes
    arg_str = " ".join(parts[1:])
    args_raw = [a.strip() for a in arg_str.split(",")]

    cells = [opcode]
    for i, arg in enumerate(args_raw):
        # Handle native function names (SYSREQ.C / SYSREQ.N first arg)
        if opcode in (123, 53) and i == 0:
            # Try as native name first
            if arg in native_by_name:
                cells.append(native_by_name[arg])
                continue
            # Try as native_N format
            if arg.startswith("native_"):
                try:
                    cells.append(int(arg[7:]))
                    continue
                except ValueError:
                    pass

        # Parse numeric argument (always as signed 32-bit cell)
        arg_clean = arg.strip()
        if arg_clean.startswith("0x") or arg_clean.startswith("0X"):
            v = int(arg_clean, 16)
        else:
            v = int(arg_clean)
        # Convert unsigned 32-bit to signed (hex values may be unsigned)
        if v > 0x7FFFFFFF:
            v -= 0x100000000
        cells.append(v)

    return cells


def assemble(disasm_dict: dict, amx: 'AMXFile') -> tuple:
    """Reassemble a disassembly dict back to (code_cells, data_cells).

    Takes the output of disassemble() (possibly modified) and the original
    AMX file (for native name resolution), and produces cell arrays suitable
    for rebuild_amx().

    Returns (code_cells: list[int], data_cells: list[int]).
    """
    # Build native name → index lookup
    native_by_name = {e.name: i for i, e in enumerate(amx.natives)}

    # Flatten all function instructions back to linear cells
    code_cells = []
    for func in disasm_dict["functions"]:
        for instr in func["instructions"]:
            cells = parse_instruction(instr["text"], native_by_name)
            code_cells.extend(cells)

    # Data section from data_values (already unsigned 32-bit ints)
    data_values = disasm_dict.get("data_values", [])
    # Convert back to signed for compact encoding
    data_cells = []
    for v in data_values:
        if v >= 0x80000000:
            data_cells.append(v - 0x100000000)
        else:
            data_cells.append(v)

    return code_cells, data_cells


def rebuild_amx(amx: 'AMXFile', code_cells: list, data_cells: list) -> bytes:
    """Rebuild a complete AMX binary from modified code/data cells.

    Uses the original AMX as a template for tables and nametable.
    Recalculates header offsets based on new code/data sizes.
    Compact-encodes the cells for the output file.

    Args:
        amx: original AMXFile (template for header/tables/nametable)
        code_cells: list of signed 32-bit code cells
        data_cells: list of signed 32-bit data cells
    Returns:
        bytes: complete AMX file content
    """
    h = amx.header

    # The pre-code section (header + tables + nametable) stays byte-identical.
    # Its size is h.cod bytes (cod is the file offset where code starts).
    pre_code = amx.raw_data[:h.cod]

    # Compact-encode code + data cells together
    all_cells = code_cells + data_cells
    compact_data = compact_encode(all_cells)

    # Recalculate expanded offsets
    new_cod = h.cod  # unchanged — pre-code section is same size
    new_dat = new_cod + len(code_cells) * 4
    new_hea = new_dat + len(data_cells) * 4

    # Stack top: maintain same stack size as original
    orig_stack_size = h.stp - h.hea
    new_stp = new_hea + orig_stack_size

    # Total file size
    new_size = len(pre_code) + len(compact_data)

    # Build the new header (56 bytes)
    new_header = bytearray(pre_code[:HEADER_SIZE])
    struct.pack_into("<i", new_header, 0, new_size)     # size
    struct.pack_into("<i", new_header, 12, new_cod)     # cod
    struct.pack_into("<i", new_header, 16, new_dat)     # dat
    struct.pack_into("<i", new_header, 20, new_hea)     # hea
    struct.pack_into("<i", new_header, 24, new_stp)     # stp
    # cip stays the same (entry point address in code)

    # Assemble final binary
    result = bytes(new_header) + pre_code[HEADER_SIZE:] + compact_data
    return result


def amx_to_json(amx: AMXFile) -> dict:
    """Convert AMX to a JSON-serializable dict."""
    h = amx.header
    return {
        "header": {
            "size": h.size,
            "magic": f"0x{h.magic:04X}",
            "file_version": h.file_version,
            "amx_version": h.amx_version,
            "flags": h.flags,
            "flags_desc": ", ".join(
                name for bit, name in [
                    (0x02, "DEBUG"), (0x04, "COMPACT"), (0x08, "SLEEP"),
                    (0x10, "NOCHECKS"), (0x20, "DSEG_INIT"),
                ] if h.flags & bit
            ),
            "defsize": h.defsize,
            "cod": h.cod,
            "dat": h.dat,
            "hea": h.hea,
            "stp": h.stp,
            "cip": h.cip,
            "code_size_expanded": h.dat - h.cod,
            "data_size_expanded": h.hea - h.dat,
        },
        "publics": [{"addr": f"0x{e.value:04X}", "name": e.name} for e in amx.publics],
        "natives": [{"index": i, "name": e.name} for i, e in enumerate(amx.natives)],
        "libraries": [{"name": e.name} for e in amx.libraries],
        "pubvars": [{"addr": f"0x{e.value:04X}", "name": e.name} for e in amx.pubvars],
        "tags": [{"id": e.value, "name": e.name} for e in amx.tags],
        "code_bytes_compact": len(amx.code_raw),
        "disassembly": disassemble(amx),
    }


# ─── CLI ──────────────────────────────────────────────────────────────────

def cmd_info(args):
    """Show AMX file info."""
    amx = read_amx(args.input)
    h = amx.header
    print(f"File:            {args.input}")
    print(f"Size:            {h.size} bytes")
    print(f"Magic:           0x{h.magic:04X}")
    print(f"Version:         file={h.file_version} amx={h.amx_version}")
    print(f"Flags:           0x{h.flags:04X}", end="")
    flag_names = []
    if h.flags & 0x02: flag_names.append("DEBUG")
    if h.flags & 0x04: flag_names.append("COMPACT")
    if h.flags & 0x08: flag_names.append("SLEEP")
    if h.flags & 0x10: flag_names.append("NOCHECKS")
    if flag_names:
        print(f" ({', '.join(flag_names)})")
    else:
        print()
    print(f"Defsize:         {h.defsize}")
    print(f"Code (expanded): {h.dat - h.cod} bytes (cod=0x{h.cod:X} dat=0x{h.dat:X})")
    print(f"Data (expanded): {h.hea - h.dat} bytes (hea=0x{h.hea:X})")
    print(f"Stack top:       0x{h.stp:X}")
    print(f"Entry point:     cip={h.cip}")
    print(f"Publics:         {len(amx.publics)}")
    for e in amx.publics[:20]:
        print(f"  0x{e.value:04X}  {e.name}")
    if len(amx.publics) > 20:
        print(f"  ... {len(amx.publics) - 20} more")
    print(f"Natives:         {len(amx.natives)}")
    for i, e in enumerate(amx.natives[:20]):
        print(f"  [{i}] {e.name}")
    if len(amx.natives) > 20:
        print(f"  ... {len(amx.natives) - 20} more")
    print(f"Libraries:       {len(amx.libraries)}")
    for e in amx.libraries:
        print(f"  {e.name}")
    if amx.pubvars:
        print(f"Public vars:     {len(amx.pubvars)}")
    if amx.tags:
        print(f"Tags:            {len(amx.tags)}")


def cmd_dump(args):
    """Dump AMX to JSON."""
    amx = read_amx(args.input)
    data = amx_to_json(amx)
    output = json.dumps(data, indent=2, ensure_ascii=False)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


def cmd_batch_dump(args):
    """Batch dump all .amx files in a directory to JSON."""
    indir = args.input
    outdir = args.output or os.path.join("dump", os.path.basename(indir.rstrip("/\\")))

    files = []
    for root, dirs, fnames in os.walk(indir):
        for fn in fnames:
            if fn.endswith(".amx"):
                files.append(os.path.join(root, fn))

    print(f"Found {len(files)} .amx files", file=sys.stderr)
    ok = err = 0
    for fp in sorted(files):
        rel = os.path.relpath(fp, indir)
        out_path = os.path.join(outdir, os.path.splitext(rel)[0] + ".json")
        try:
            amx = read_amx(fp)
            data = amx_to_json(amx)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            ok += 1
        except Exception as e:
            print(f"  ERROR: {rel}: {e}", file=sys.stderr)
            err += 1

    print(f"Done: {ok} dumped, {err} errors", file=sys.stderr)


def cmd_disasm(args):
    """Disassemble an AMX file to human-readable text."""
    amx = read_amx(args.input)
    result = disassemble(amx)
    h = amx.header
    lines = []
    lines.append(f"; AMX Disassembly: {args.input}")
    lines.append(f"; Entry point: {result['entry_point']}")
    lines.append(f"; Code: {result['code_cells']} cells ({result['code_cells'] * 4} bytes)")
    lines.append(f"; Data: {result['data_cells']} cells ({result['data_cells'] * 4} bytes)")
    lines.append(f"; Publics: {len(amx.publics)}  Natives: {len(amx.natives)}")
    lines.append("")

    # Native table
    if amx.natives:
        lines.append("; ─── Native Functions ─────────────────")
        for i, e in enumerate(amx.natives):
            lines.append(f";   [{i:3d}] {e.name}")
        lines.append("")

    # Functions
    lines.append("; ─── Code Section ─────────────────────")
    for func in result["functions"]:
        lines.append("")
        lines.append(f"; ═══ {func['name']} ═══")
        for instr in func["instructions"]:
            label = instr.get("label", "")
            prefix = f"{label}: " if label else "    "
            lines.append(f"  {instr['addr']}  {prefix}{instr['text']}")

    # Data strings
    if result["data_strings"]:
        lines.append("")
        lines.append("; ─── Data Strings ─────────────────────")
        for s in result["data_strings"]:
            lines.append(f";   {s['offset']}  {s['text']!r}")

    output = "\n".join(lines)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


def cmd_asm(args):
    """Reassemble an AMX from a modified JSON dump back to binary."""
    amx = read_amx(args.original)
    with open(args.json_input, "r", encoding="utf-8") as f:
        dump = json.load(f)

    disasm_dict = dump["disassembly"]
    code_cells, data_cells = assemble(disasm_dict, amx)
    new_binary = rebuild_amx(amx, code_cells, data_cells)

    output = args.output or args.original
    with open(output, "wb") as f:
        f.write(new_binary)

    orig_h = amx.header
    new_code_n = len(code_cells)
    orig_code_n = (orig_h.dat - orig_h.cod) // 4
    new_data_n = len(data_cells)
    orig_data_n = (orig_h.hea - orig_h.dat) // 4
    print(f"Assembled {output}", file=sys.stderr)
    print(f"  Code: {orig_code_n} → {new_code_n} cells", file=sys.stderr)
    print(f"  Data: {orig_data_n} → {new_data_n} cells", file=sys.stderr)
    print(f"  Size: {orig_h.size} → {len(new_binary)} bytes", file=sys.stderr)


def cmd_roundtrip(args):
    """Verify disassemble→assemble roundtrip produces identical cells."""
    amx = read_amx(args.input)
    h = amx.header

    # Original cells
    orig_code, orig_data = expand_amx(amx)

    # Disassemble then reassemble
    disasm_dict = disassemble(amx)
    new_code, new_data = assemble(disasm_dict, amx)

    # Compare
    code_match = orig_code == new_code
    data_match = orig_data == new_data

    print(f"File: {args.input}", file=sys.stderr)
    print(f"  Code cells: {len(orig_code)} original, {len(new_code)} reassembled — {'MATCH' if code_match else 'MISMATCH'}", file=sys.stderr)
    print(f"  Data cells: {len(orig_data)} original, {len(new_data)} reassembled — {'MATCH' if data_match else 'MISMATCH'}", file=sys.stderr)

    if not code_match:
        # Find first difference
        for i in range(min(len(orig_code), len(new_code))):
            if orig_code[i] != new_code[i]:
                print(f"  First code diff at cell {i} (addr 0x{i*4:04X}): "
                      f"orig={orig_code[i]} new={new_code[i]}", file=sys.stderr)
                break
        if len(orig_code) != len(new_code):
            print(f"  Code length mismatch: {len(orig_code)} vs {len(new_code)}", file=sys.stderr)

    if not data_match and len(orig_data) != len(new_data):
        print(f"  Data length mismatch: {len(orig_data)} vs {len(new_data)}", file=sys.stderr)

    # Also verify compact encoding roundtrip
    all_cells = new_code + new_data
    compact = compact_encode(all_cells)
    decoded = compact_decode(compact)
    encode_match = decoded == all_cells
    print(f"  Compact encode→decode: {'MATCH' if encode_match else 'MISMATCH'}", file=sys.stderr)

    ok = code_match and data_match and encode_match
    if ok:
        print("  ✓ Perfect roundtrip", file=sys.stderr)
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="AMX Tool — Parser/Assembler for BDFFHD Pawn AMX script files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    p_info = sub.add_parser("info", help="Show AMX file header info")
    p_info.add_argument("input", help="Path to .amx file")

    p_dump = sub.add_parser("dump", help="Dump AMX to JSON")
    p_dump.add_argument("input", help="Path to .amx file")
    p_dump.add_argument("-o", "--output", help="Output JSON file")

    p_batch = sub.add_parser("batch-dump", help="Batch dump directory")
    p_batch.add_argument("input", help="Directory containing .amx files")
    p_batch.add_argument("-o", "--output", help="Output directory")

    p_disasm = sub.add_parser("disasm", help="Disassemble AMX code")
    p_disasm.add_argument("input", help="Path to .amx file")
    p_disasm.add_argument("-o", "--output", help="Output .asm file")

    p_asm = sub.add_parser("asm", help="Reassemble AMX from modified JSON dump")
    p_asm.add_argument("original", help="Path to original .amx file (template)")
    p_asm.add_argument("json_input", help="Path to modified JSON dump")
    p_asm.add_argument("-o", "--output", help="Output .amx file (default: overwrite original)")

    p_rt = sub.add_parser("roundtrip", help="Verify disasm→asm roundtrip")
    p_rt.add_argument("input", help="Path to .amx file")

    args = parser.parse_args()
    if args.command == "info":
        cmd_info(args)
    elif args.command == "dump":
        cmd_dump(args)
    elif args.command == "batch-dump":
        cmd_batch_dump(args)
    elif args.command == "disasm":
        cmd_disasm(args)
    elif args.command == "asm":
        cmd_asm(args)
    elif args.command == "roundtrip":
        cmd_roundtrip(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
