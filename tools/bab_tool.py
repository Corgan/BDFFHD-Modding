#!/usr/bin/env python3
"""
bab_tool.py — Parser/Editor for BDFFHD .bab (BASB) battle action script files.

Structure: BABFHeader (32 bytes) + script section (TLV entries) + ASCII strings + UTF16 strings.
BABFHeader mirrors BTBHeader: magic(4) + fileSize(4) + scriptOffset(4) + scriptSize(4)
                              + stringOffset(4) + stringSize(4) + utf16Offset(4) + utf16Size(4)

Script entries are TLV: [type:u32, total_size:u32, ...args] where total_size includes the 8-byte header.

Usage:
    python bab_tool.py dump  <file.bab>                # Dump to JSON
    python bab_tool.py info  <file.bab>                # Show header info
    python bab_tool.py import <file.bab> <input.json> [-o output.bab]
    python bab_tool.py batch-dump <directory> [-o outdir]
"""
import struct
import json
import sys
import os
import argparse
from pathlib import Path
from dataclasses import dataclass

HEADER_SIZE = 32
MAGIC = b"BASB"


@dataclass
class BABHeader:
    magic: bytes
    file_size: int
    script_offset: int
    script_size: int
    string_offset: int
    string_size: int
    utf16_offset: int
    utf16_size: int


@dataclass
class ScriptEntry:
    type_id: int
    raw_args: bytes  # payload bytes after type+size


@dataclass
class BABFile:
    header: BABHeader
    entries: list  # list of ScriptEntry
    ascii_strings: bytes
    utf16_strings: bytes


def read_bab(filepath: str) -> BABFile:
    """Read a .bab file into a BABFile."""
    with open(filepath, "rb") as f:
        data = f.read()
    
    magic = data[:4]
    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic!r}, expected {MAGIC!r}")
    
    file_size, script_off, script_size, str_off, str_size, utf16_off, utf16_size = \
        struct.unpack_from("<7I", data, 4)
    
    header = BABHeader(magic, file_size, script_off, script_size,
                       str_off, str_size, utf16_off, utf16_size)
    
    # Parse script entries
    entries = []
    pos = script_off
    end = script_off + script_size
    while pos + 8 <= end:
        type_id, total_size = struct.unpack_from("<II", data, pos)
        if total_size < 8 or pos + total_size > end + 4:
            break
        payload = data[pos + 8:pos + total_size]
        entries.append(ScriptEntry(type_id, payload))
        pos += total_size
    
    # String tables
    ascii_strings = data[str_off:str_off + str_size] if str_size > 0 else b""
    utf16_strings = data[utf16_off:utf16_off + utf16_size] if utf16_size > 0 else b""
    
    return BABFile(header, entries, ascii_strings, utf16_strings)


def write_bab(bab: BABFile, filepath: str):
    """Write a BABFile back to a .bab binary."""
    # Rebuild script section
    script_section = b""
    for entry in bab.entries:
        total_size = 8 + len(entry.raw_args)
        script_section += struct.pack("<II", entry.type_id, total_size) + entry.raw_args
    
    # Recalculate offsets
    script_off = HEADER_SIZE
    script_size = len(script_section)
    str_off = script_off + script_size
    str_size = len(bab.ascii_strings)
    utf16_off = str_off + str_size
    # Align to 2-byte boundary if needed
    if utf16_off % 2 != 0:
        utf16_off += 1
    utf16_size = len(bab.utf16_strings)
    file_size = utf16_off + utf16_size
    
    # Pack header
    header = struct.pack("<4s7I", MAGIC, file_size, script_off, script_size,
                         str_off, str_size, utf16_off, utf16_size)
    
    # Assemble file
    out = bytearray(file_size)
    out[:HEADER_SIZE] = header
    out[script_off:script_off + script_size] = script_section
    if str_size > 0:
        out[str_off:str_off + str_size] = bab.ascii_strings
    if utf16_size > 0:
        out[utf16_off:utf16_off + utf16_size] = bab.utf16_strings
    
    with open(filepath, "wb") as f:
        f.write(out)


def get_ascii(data: bytes, offset: int) -> str:
    """Read null-terminated ASCII string from buffer."""
    end = data.index(0, offset) if 0 in data[offset:] else len(data)
    return data[offset:end].decode("ascii", errors="replace")


def get_utf16(data: bytes, offset: int) -> str:
    """Read null-terminated UTF-16LE string from buffer."""
    end = offset
    while end + 1 < len(data):
        if data[end] == 0 and data[end + 1] == 0:
            break
        end += 2
    return data[offset:end].decode("utf-16-le", errors="replace")


def entry_to_dict(entry: ScriptEntry, ascii_data: bytes, utf16_data: bytes) -> dict:
    """Convert a ScriptEntry to a JSON-friendly dict."""
    d = {"type": entry.type_id, "size": 8 + len(entry.raw_args)}
    
    # Parse args as int32 array
    n_ints = len(entry.raw_args) // 4
    if n_ints > 0:
        args = list(struct.unpack(f"<{n_ints}i", entry.raw_args[:n_ints * 4]))
        d["args"] = args
        
        # Try to resolve string references
        str_refs = []
        for i, val in enumerate(args):
            resolved = None
            if 0 < val < len(ascii_data):
                try:
                    s = get_ascii(ascii_data, val)
                    if s and all(32 <= ord(c) < 127 for c in s) and len(s) >= 2:
                        resolved = ("ascii", s)
                except:
                    pass
            if resolved is None and 0 < val < len(utf16_data) and val % 2 == 0:
                try:
                    s = get_utf16(utf16_data, val)
                    if s and len(s) >= 2:
                        resolved = ("utf16", s)
                except:
                    pass
            if resolved:
                str_refs.append({"arg": i, "type": resolved[0], "value": resolved[1]})
        
        if str_refs:
            d["strings"] = str_refs
    
    # Leftover bytes
    leftover = len(entry.raw_args) % 4
    if leftover:
        d["tail_bytes"] = list(entry.raw_args[-leftover:])
    
    return d


def bab_to_json(bab: BABFile) -> dict:
    """Convert a BABFile to a JSON-serializable dict."""
    entries = [entry_to_dict(e, bab.ascii_strings, bab.utf16_strings) for e in bab.entries]
    
    # Collect all ASCII strings
    all_ascii = []
    pos = 0
    while pos < len(bab.ascii_strings):
        s = get_ascii(bab.ascii_strings, pos)
        all_ascii.append(s)
        pos += len(s) + 1
    
    return {
        "header": {
            "magic": bab.header.magic.decode("ascii"),
            "file_size": bab.header.file_size,
            "script_size": bab.header.script_size,
            "string_size": bab.header.string_size,
            "utf16_size": bab.header.utf16_size,
        },
        "entries": entries,
        "entry_count": len(entries),
        "ascii_strings": all_ascii if all_ascii else None,
    }


def json_to_bab(data: dict, original: BABFile) -> BABFile:
    """Reconstruct a BABFile from JSON + original for reference."""
    entries = []
    for jentry in data["entries"]:
        type_id = jentry["type"]
        args = jentry.get("args", [])
        raw = struct.pack(f"<{len(args)}i", *args) if args else b""
        if "tail_bytes" in jentry:
            raw += bytes(jentry["tail_bytes"])
        entries.append(ScriptEntry(type_id, raw))
    
    return BABFile(original.header, entries, original.ascii_strings, original.utf16_strings)


def cmd_info(args):
    bab = read_bab(args.input)
    h = bab.header
    print(f"File:           {args.input}")
    print(f"Magic:          {h.magic.decode('ascii')}")
    print(f"File Size:      {h.file_size}")
    print(f"Script:         offset={h.script_offset} size={h.script_size}")
    print(f"ASCII Strings:  offset={h.string_offset} size={h.string_size}")
    print(f"UTF16 Strings:  offset={h.utf16_offset} size={h.utf16_size}")
    print(f"Entries:        {len(bab.entries)}")
    
    # Type distribution
    from collections import Counter
    types = Counter(e.type_id for e in bab.entries)
    print(f"Entry types:")
    for tid, cnt in types.most_common():
        print(f"  type={tid} (0x{tid:04X}): {cnt}x, size={8 + len(bab.entries[[e.type_id for e in bab.entries].index(tid)].raw_args)}")


def cmd_dump(args):
    bab = read_bab(args.input)
    data = bab_to_json(bab)
    output = json.dumps(data, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Dumped {len(bab.entries)} entries to {args.output}", file=sys.stderr)
    else:
        print(output)


def cmd_import(args):
    original = read_bab(args.original)
    with open(args.json_input, "r", encoding="utf-8") as f:
        data = json.load(f)
    bab = json_to_bab(data, original)
    output = args.output or args.original
    write_bab(bab, output)
    print(f"Written {len(bab.entries)} entries to {output}", file=sys.stderr)


def cmd_batch_dump(args):
    indir = args.input_dir
    outdir = args.output_dir or os.path.join(os.path.dirname(__file__), "dump_bab")
    
    count = errors = 0
    for root, dirs, files in os.walk(indir):
        for fn in files:
            if not fn.endswith(".bab"):
                continue
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, indir)
            out_path = os.path.join(outdir, rel.replace(".bab", ".json"))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            try:
                bab = read_bab(fp)
                data = bab_to_json(bab)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                count += 1
            except Exception as e:
                errors += 1
                print(f"ERROR: {rel}: {e}", file=sys.stderr)
    print(f"Dumped {count} .bab files ({errors} errors) to {outdir}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="bab_tool — Parser/Editor for BDFFHD .bab battle action scripts")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("info", help="Show .bab header info")
    p.add_argument("input")

    p = sub.add_parser("dump", help="Dump .bab to JSON")
    p.add_argument("input")
    p.add_argument("-o", "--output")

    p = sub.add_parser("import", help="Import JSON back to .bab")
    p.add_argument("original", help="Original .bab file")
    p.add_argument("json_input", help="Modified JSON")
    p.add_argument("-o", "--output")

    p = sub.add_parser("batch-dump", help="Dump all .bab files")
    p.add_argument("input_dir")
    p.add_argument("-o", "--output-dir")

    args = parser.parse_args()
    cmds = {"info": cmd_info, "dump": cmd_dump, "import": cmd_import, "batch-dump": cmd_batch_dump}
    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
