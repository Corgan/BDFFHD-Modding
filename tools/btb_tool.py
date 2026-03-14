#!/usr/bin/env python3
"""
BTB Tool — Parser/Editor for Bravely Default: Flying Fairy HD .btb files.

BTB Format:
  Header (48 bytes):
    [0x00] char[4]  fileID        — Magic "BTBF"
    [0x04] int32    fileSize      — Total file size in bytes
    [0x08] int32    dataOffset    — Offset to data section (always 0x30 = 48)
    [0x0C] int32    dataSize      — Size of all data records combined
    [0x10] int32    stringOffset  — Offset to ASCII string table
    [0x14] int32    stringSize    — Size of ASCII string table
    [0x18] int32    utf16Offset   — Offset to UTF-16LE string table
    [0x1C] int32    utf16Size     — Size of UTF-16LE string table
    [0x20] int32    dataOneSize   — Size of one data record in bytes
    [0x24] int32    dataNum       — Number of data records
    [0x28] int32    isSetuped     — Setup flag (always 0)
    [0x2C] int32    reserve       — Reserved (always 0)

  Data Section:
    dataNum records, each dataOneSize bytes.
    All fields are int32 (4 bytes). Arrays are inlined with fixed element counts.

  ASCII String Table:
    Null-terminated ASCII strings. Referenced by byte offset from table start.

  UTF-16 String Table:
    Null-terminated UTF-16LE strings. Referenced by byte offset from table start.
"""
import struct
import json
import sys
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict


MAGIC = b"BTBF"
HEADER_SIZE = 48
HEADER_FMT = "<4s11i"  # 4-byte magic + 11 int32s


@dataclass
class BTBHeader:
    file_id: bytes
    file_size: int
    data_offset: int
    data_size: int
    string_offset: int
    string_size: int
    utf16_offset: int
    utf16_size: int
    data_one_size: int
    data_num: int
    is_setuped: int
    reserve: int


@dataclass
class BTBFile:
    header: BTBHeader
    records: list  # list of list[int] — each record is a list of int32 values
    ascii_strings: bytes  # raw ASCII string table
    utf16_strings: bytes  # raw UTF-16 string table

    def get_ascii(self, byte_offset: int) -> str:
        """Get null-terminated ASCII string at byte offset in string table."""
        if byte_offset < 0 or byte_offset >= len(self.ascii_strings):
            return ""
        end = self.ascii_strings.index(0, byte_offset) if 0 in self.ascii_strings[byte_offset:] else len(self.ascii_strings)
        return self.ascii_strings[byte_offset:end].decode("ascii", errors="replace")

    def get_utf16(self, byte_offset: int) -> str:
        """Get null-terminated UTF-16LE string at byte offset in UTF-16 table."""
        if byte_offset < 0 or byte_offset >= len(self.utf16_strings):
            return ""
        # Find null terminator (two zero bytes on even boundary)
        pos = byte_offset
        while pos + 1 < len(self.utf16_strings):
            if self.utf16_strings[pos] == 0 and self.utf16_strings[pos + 1] == 0:
                break
            pos += 2
        return self.utf16_strings[byte_offset:pos].decode("utf-16-le", errors="replace")

    def all_ascii_strings(self) -> list[tuple[int, str]]:
        """Return list of (byte_offset, string) for all ASCII strings."""
        result = []
        pos = 0
        while pos < len(self.ascii_strings):
            end = self.ascii_strings.index(0, pos) if 0 in self.ascii_strings[pos:] else len(self.ascii_strings)
            s = self.ascii_strings[pos:end].decode("ascii", errors="replace")
            result.append((pos, s))
            pos = end + 1
        return result

    def all_utf16_strings(self) -> list[tuple[int, str]]:
        """Return list of (byte_offset, string) for all UTF-16 strings."""
        result = []
        pos = 0
        while pos < len(self.utf16_strings):
            end = pos
            while end + 1 < len(self.utf16_strings):
                if self.utf16_strings[end] == 0 and self.utf16_strings[end + 1] == 0:
                    break
                end += 2
            s = self.utf16_strings[pos:end].decode("utf-16-le", errors="replace")
            result.append((pos, s))
            pos = end + 2
        return result


def read_btb(filepath: str) -> BTBFile:
    """Read and parse a .btb file."""
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < HEADER_SIZE:
        raise ValueError(f"File too small: {len(data)} bytes")

    values = struct.unpack_from(HEADER_FMT, data, 0)
    header = BTBHeader(*values)

    if header.file_id != MAGIC:
        raise ValueError(f"Invalid magic: {header.file_id!r} (expected {MAGIC!r})")

    # Parse data records
    ints_per_record = header.data_one_size // 4
    records = []
    for i in range(header.data_num):
        offset = header.data_offset + i * header.data_one_size
        record = list(struct.unpack_from(f"<{ints_per_record}i", data, offset))
        records.append(record)

    # Extract string tables
    ascii_strings = data[header.string_offset : header.string_offset + header.string_size]
    utf16_strings = data[header.utf16_offset : header.utf16_offset + header.utf16_size]

    return BTBFile(header, records, ascii_strings, utf16_strings)


def write_btb(btb: BTBFile, filepath: str):
    """Write a BTBFile back to a .btb binary file."""
    ints_per_record = btb.header.data_one_size // 4

    # Rebuild data section
    data_section = b""
    for record in btb.records:
        data_section += struct.pack(f"<{ints_per_record}i", *record)

    # Recalculate offsets and sizes
    data_offset = HEADER_SIZE
    data_size = len(data_section)
    string_offset = data_offset + data_size
    string_size = len(btb.ascii_strings)
    # Align UTF-16 table to even byte boundary (game tool adds padding byte)
    utf16_offset = string_offset + string_size
    pad = utf16_offset % 2
    utf16_offset += pad
    utf16_size = len(btb.utf16_strings)
    file_size = utf16_offset + utf16_size

    # Pack header
    header_bytes = struct.pack(
        HEADER_FMT,
        MAGIC,
        file_size,
        data_offset,
        data_size,
        string_offset,
        string_size,
        utf16_offset,
        utf16_size,
        btb.header.data_one_size,
        len(btb.records),
        0,  # isSetuped
        0,  # reserve
    )

    with open(filepath, "wb") as f:
        f.write(header_bytes)
        f.write(data_section)
        f.write(btb.ascii_strings)
        if pad:
            f.write(b"\x00")
        f.write(btb.utf16_strings)


def btb_to_json(btb: BTBFile, schema: dict | None = None) -> dict:
    """Convert BTBFile to JSON-serializable dict."""
    result = {
        "header": {
            "magic": btb.header.file_id.decode("ascii"),
            "file_size": btb.header.file_size,
            "data_one_size": btb.header.data_one_size,
            "data_num": btb.header.data_num,
            "ints_per_record": btb.header.data_one_size // 4,
        },
        "records": [],
    }

    for i, record in enumerate(btb.records):
        if schema:
            row = {}
            for field_def in schema["fields"]:
                name = field_def["name"]
                idx = field_def["index"]
                count = field_def.get("count", 1)
                str_type = field_def.get("string", None)

                if count == 1:
                    val = record[idx]
                    if str_type == "ascii":
                        row[name] = btb.get_ascii(val)
                    elif str_type == "utf16":
                        row[name] = btb.get_utf16(val)
                    else:
                        row[name] = val
                else:
                    row[name] = record[idx : idx + count]
            result["records"].append(row)
        else:
            result["records"].append({"_index": i, "values": record})

    return result


# ─── Schema Definitions ──────────────────────────────────
# Schemas are loaded from individual JSON files in the schemas/ directory.
# Each file is named {SchemaName}.json and contains a single schema object
# with "name", "description", and "fields" keys.

SCHEMA_DIR = Path(__file__).parent / "schemas"


def _load_schemas() -> dict:
    """Load all schema JSON files from the schemas/ directory."""
    schemas = {}
    if not SCHEMA_DIR.is_dir():
        return schemas
    for fp in sorted(SCHEMA_DIR.glob("*.json")):
        with open(fp, "r", encoding="utf-8") as f:
            schema = json.load(f)
        schemas[schema["name"]] = schema
    return schemas


SCHEMAS = _load_schemas()


def auto_detect_schema(filepath: str) -> dict | None:
    """Try to detect the schema from the filename."""
    name = Path(filepath).stem
    for key in SCHEMAS:
        if name.startswith(key) or name == key:
            return SCHEMAS[key]
    return None


def cmd_info(args):
    """Show BTB file header info."""
    btb = read_btb(args.input)
    h = btb.header
    print(f"File:           {args.input}")
    print(f"Magic:          {h.file_id.decode('ascii')}")
    print(f"File Size:      {h.file_size} (0x{h.file_size:X})")
    print(f"Data Offset:    {h.data_offset} (0x{h.data_offset:X})")
    print(f"Data Size:      {h.data_size} (0x{h.data_size:X})")
    print(f"String Offset:  {h.string_offset} (0x{h.string_offset:X})")
    print(f"String Size:    {h.string_size} (0x{h.string_size:X})")
    print(f"UTF16 Offset:   {h.utf16_offset} (0x{h.utf16_offset:X})")
    print(f"UTF16 Size:     {h.utf16_size} (0x{h.utf16_size:X})")
    print(f"Record Size:    {h.data_one_size} bytes ({h.data_one_size // 4} int32s)")
    print(f"Record Count:   {h.data_num}")
    print(f"ASCII Strings:  {len(btb.all_ascii_strings())}")
    print(f"UTF16 Strings:  {len(btb.all_utf16_strings())}")


def cmd_dump(args):
    """Dump BTB records to JSON."""
    btb = read_btb(args.input)
    schema = None
    if args.schema:
        if args.schema in SCHEMAS:
            schema = SCHEMAS[args.schema]
        else:
            with open(args.schema) as f:
                schema = json.load(f)
    elif args.auto_schema:
        schema = auto_detect_schema(args.input)
        if schema:
            print(f"Auto-detected schema: {schema['name']}", file=sys.stderr)

    result = btb_to_json(btb, schema)
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


def cmd_record(args):
    """Show a single record in detail."""
    btb = read_btb(args.input)
    idx = args.index
    if idx < 0 or idx >= len(btb.records):
        print(f"Record index {idx} out of range (0-{len(btb.records)-1})")
        return
    record = btb.records[idx]
    schema = None
    if args.schema and args.schema in SCHEMAS:
        schema = SCHEMAS[args.schema]
    elif args.auto_schema:
        schema = auto_detect_schema(args.input)

    print(f"Record #{idx} ({len(record)} fields):")
    if schema:
        for fd in schema["fields"]:
            name = fd["name"]
            i = fd["index"]
            count = fd.get("count", 1)
            str_type = fd.get("string")
            if count == 1:
                val = record[i]
                if str_type == "ascii":
                    print(f"  {name:30s} = {val:8d}  → \"{btb.get_ascii(val)}\"")
                elif str_type == "utf16":
                    print(f"  {name:30s} = {val:8d}  → \"{btb.get_utf16(val)}\"")
                else:
                    print(f"  {name:30s} = {val}")
            else:
                vals = record[i : i + count]
                print(f"  {name:30s} = {vals}")
        # Show remaining unmapped fields
        mapped = set()
        for fd in schema["fields"]:
            for j in range(fd.get("count", 1)):
                mapped.add(fd["index"] + j)
        unmapped = [(i, record[i]) for i in range(len(record)) if i not in mapped and record[i] != 0]
        if unmapped:
            print(f"\n  Unmapped non-zero fields:")
            for i, val in unmapped:
                print(f"    [{i:3d}] 0x{i*4:03X} = {val}")
    else:
        for i, val in enumerate(record):
            if val != 0 or args.all:
                print(f"  [{i:3d}] 0x{i*4:03X} = {val}")


def cmd_schemas(args):
    """List available schemas."""
    for name, schema in SCHEMAS.items():
        print(f"  {name:20s} — {schema['description']}")
        print(f"  {'':20s}   {len(schema['fields'])} field definitions")


def cmd_batch_dump(args):
    """Dump all .btb files in a directory."""
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    btb_files = sorted(input_dir.rglob("*.btb"))
    print(f"Found {len(btb_files)} .btb files")

    for btb_path in btb_files:
        try:
            btb = read_btb(str(btb_path))
            schema = auto_detect_schema(str(btb_path)) if args.auto_schema else None
            rel = btb_path.relative_to(input_dir)
            out_path = output_dir / rel.with_suffix(".json")
            out_path.parent.mkdir(parents=True, exist_ok=True)

            result = btb_to_json(btb, schema)
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

            tag = f" [{schema['name']}]" if schema else ""
            print(f"  ✓ {rel}{tag} → {out_path.name} ({btb.header.data_num} records)")
        except Exception as e:
            print(f"  ✗ {btb_path.name}: {e}")


def cmd_import(args):
    """Import a modified JSON dump back into a BTB file.

    Reads the original .btb for structure (record size, record count),
    then applies field changes from the JSON. String tables are rebuilt
    from scratch so renamed strings get correct offsets automatically.

    If the JSON was dumped WITH a schema, named fields are mapped back.
    If without a schema, the raw 'values' arrays are used directly.
    """
    btb = read_btb(args.original)

    with open(args.json_input, "r", encoding="utf-8") as f:
        data = json.load(f)

    json_records = data["records"]

    # Detect schema — needed to map named fields back to int32 indices
    schema = None
    if args.schema:
        if args.schema in SCHEMAS:
            schema = SCHEMAS[args.schema]
        else:
            with open(args.schema) as f:
                schema = json.load(f)
    elif args.auto_schema:
        schema = auto_detect_schema(args.original)
        if schema:
            print(f"Auto-detected schema: {schema['name']}", file=sys.stderr)

    ints_per_record = btb.header.data_one_size // 4
    changes = 0

    if schema:
        # ── Schema mode: preserve existing string tables, append new strings ──
        # This avoids breaking hidden string references in fields not covered
        # by the schema (e.g. COS_PC in ItemTable are ASCII offsets but not
        # marked as strings). Changed strings get appended to the tables.
        ascii_buf = bytearray(btb.ascii_strings)
        utf16_buf = bytearray(btb.utf16_strings)

        # Build reverse lookups of existing strings
        ascii_lookup = {s: off for off, s in btb.all_ascii_strings()}
        utf16_lookup = {s: off for off, s in btb.all_utf16_strings()}

        def resolve_ascii(val):
            if val in ascii_lookup:
                return ascii_lookup[val]
            # New string — append to table
            offset = len(ascii_buf)
            ascii_buf.extend(val.encode("ascii", errors="replace"))
            ascii_buf.append(0)
            ascii_lookup[val] = offset
            return offset

        def resolve_utf16(val):
            if val in utf16_lookup:
                return utf16_lookup[val]
            offset = len(utf16_buf)
            utf16_buf.extend(val.encode("utf-16-le"))
            utf16_buf.extend(b"\x00\x00")
            utf16_lookup[val] = offset
            return offset

        for rec_idx, json_rec in enumerate(json_records):
            if rec_idx >= len(btb.records):
                print(f"  Warning: JSON has more records ({len(json_records)}) than BTB ({len(btb.records)}), skipping extras", file=sys.stderr)
                break

            record = btb.records[rec_idx]

            for fd in schema["fields"]:
                name = fd["name"]
                idx = fd["index"]
                count = fd.get("count", 1)
                str_type = fd.get("string")

                if name not in json_rec:
                    continue

                if str_type:
                    str_val = json_rec[name]
                    orig_offset = record[idx]
                    get_fn = btb.get_ascii if str_type == "ascii" else btb.get_utf16
                    if get_fn(orig_offset) == str_val:
                        pass  # String unchanged — keep original offset
                    else:
                        # String was changed — find or append new value
                        resolver = resolve_ascii if str_type == "ascii" else resolve_utf16
                        new_offset = resolver(str_val)
                        record[idx] = new_offset
                        changes += 1
                elif count == 1:
                    new_val = json_rec[name]
                    if record[idx] != new_val:
                        changes += 1
                        record[idx] = new_val
                else:
                    arr = json_rec[name]
                    for j in range(min(count, len(arr))):
                        if record[idx + j] != arr[j]:
                            changes += 1
                            record[idx + j] = arr[j]

        # Update string tables (may have grown if new strings were added)
        btb.ascii_strings = bytes(ascii_buf)
        btb.utf16_strings = bytes(utf16_buf)
    else:
        # Raw mode
        for rec_idx, json_rec in enumerate(json_records):
            if rec_idx >= len(btb.records):
                print(f"  Warning: JSON has more records ({len(json_records)}) than BTB ({len(btb.records)}), skipping extras", file=sys.stderr)
                break

            record = btb.records[rec_idx]

            if "values" in json_rec:
                new_vals = json_rec["values"]
                for i in range(min(len(new_vals), len(record))):
                    if record[i] != new_vals[i]:
                        changes += 1
                        record[i] = new_vals[i]
            else:
                print(f"  Error: JSON uses named fields but no schema provided. Use -s <schema>.", file=sys.stderr)
                return

    output = args.output or args.original
    write_btb(btb, output)
    print(f"Imported {len(json_records)} records, {changes} field(s) changed.", file=sys.stderr)
    print(f"Written to {output}", file=sys.stderr)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="BTB Tool — Parser/Editor for BDFFHD .btb files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    # info
    p_info = sub.add_parser("info", help="Show BTB file header info")
    p_info.add_argument("input", help="Path to .btb file")

    # dump
    p_dump = sub.add_parser("dump", help="Dump records to JSON")
    p_dump.add_argument("input", help="Path to .btb file")
    p_dump.add_argument("-o", "--output", help="Output file path")
    p_dump.add_argument("-s", "--schema", help="Schema name or JSON file path")
    p_dump.add_argument("--auto-schema", action="store_true", default=True, help="Auto-detect schema (default)")
    p_dump.add_argument("--no-auto-schema", dest="auto_schema", action="store_false")

    # record
    p_rec = sub.add_parser("record", help="Show single record details")
    p_rec.add_argument("input", help="Path to .btb file")
    p_rec.add_argument("index", type=int, help="Record index (0-based)")
    p_rec.add_argument("-s", "--schema", help="Schema name")
    p_rec.add_argument("--auto-schema", action="store_true", default=True)
    p_rec.add_argument("--no-auto-schema", dest="auto_schema", action="store_false")
    p_rec.add_argument("-a", "--all", action="store_true", help="Show all fields (including zeros)")

    # schemas
    sub.add_parser("schemas", help="List available schemas")

    # batch-dump
    p_bd = sub.add_parser("batch-dump", help="Dump all .btb files in directory")
    p_bd.add_argument("input_dir", help="Input directory")
    p_bd.add_argument("output_dir", help="Output directory")
    p_bd.add_argument("--auto-schema", action="store_true", default=True)
    p_bd.add_argument("--no-auto-schema", dest="auto_schema", action="store_false")

    # import
    p_imp = sub.add_parser("import", help="Import modified JSON dump back into a BTB file")
    p_imp.add_argument("original", help="Path to original .btb file (for structure/string tables)")
    p_imp.add_argument("json_input", help="Path to modified JSON file")
    p_imp.add_argument("-o", "--output", help="Output .btb file (default: overwrite original)")
    p_imp.add_argument("-s", "--schema", help="Schema name or JSON file path")
    p_imp.add_argument("--auto-schema", action="store_true", default=True)
    p_imp.add_argument("--no-auto-schema", dest="auto_schema", action="store_false")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "info": cmd_info,
        "dump": cmd_dump,
        "record": cmd_record,
        "schemas": cmd_schemas,
        "batch-dump": cmd_batch_dump,
        "import": cmd_import,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
