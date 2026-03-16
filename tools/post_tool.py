#!/usr/bin/env python3
"""
post_tool.py — Parser/Editor for BDFFHD .post post-processing preset files.

Each .post file is exactly 88 bytes — a flat struct of booleans, floats, and ints
representing per-scene post-processing settings (bloom, color grading, etc).

Based on PostEffect.Status from the game's IL2CPP dump.

Usage:
    python post_tool.py dump   <file.post>              # Dump to JSON
    python post_tool.py import <file.post> <input.json> [-o output.post]
    python post_tool.py batch-dump <directory> [-o outdir]
"""
import struct
import json
import sys
import os
import argparse
from pathlib import Path
from dataclasses import dataclass

POST_SIZE = 88

# Field layout: (name, offset, format, description)
# Format: 'B'=uint8/bool, 'f'=float, 'H'=uint16, 'I'=uint32, 'i'=int32
FIELDS = [
    # Byte 0-3: packed booleans/flags
    ("antialias_enable",  0, "B", "Anti-aliasing enable"),
    ("outline_enable",    1, "B", "Outline/toon enable"),
    ("dof_enable",        2, "B", "Depth of field enable"),
    ("bloom_enable",      3, "B", "Bloom enable"),
    # Floats 4-8
    ("bloom_threshold",   4, "f", "Bloom threshold"),
    ("bloom_intensity",   8, "f", "Bloom intensity"),
    # Bloom expansion (packed short+short at 12)
    ("bloom_level",      12, "H", "Bloom expansion level"),
    ("bloom_type",       14, "H", "Bloom expansion type"),
    # Color scale (4 floats = RGBA)
    ("color_scale_r",    16, "f", "Color scale red"),
    ("color_scale_g",    20, "f", "Color scale green"),
    ("color_scale_b",    24, "f", "Color scale blue"),
    ("color_scale_a",    28, "f", "Color scale alpha"),
    # Color grading
    ("contrast_base",    32, "f", "Contrast base"),
    ("contrast",         36, "f", "Contrast"),
    ("brightness",       40, "f", "Brightness"),
    ("saturation",       44, "f", "Saturation"),
    # More color scale (second set)
    ("color_scale2_r",   48, "f", "Color scale 2 red"),
    ("color_scale2_g",   52, "f", "Color scale 2 green"),
    ("color_scale2_b",   56, "f", "Color scale 2 blue"),
    ("color_scale2_a",   60, "f", "Color scale 2 alpha"),
    # Buffer/misc
    ("buffer_scale_r",   64, "f", "Buffer scale red"),
    ("buffer_scale_g",   68, "f", "Buffer scale green"),
    ("buffer_scale_b",   72, "f", "Buffer scale blue"),
    ("buffer_scale_a",   76, "f", "Buffer scale alpha"),
    # Final fields
    ("buffer_scale_limit", 80, "I", "Buffer scale limit"),
    ("extra_flags",        84, "I", "Extra flags"),
]


def read_post(filepath: str) -> dict:
    """Read a .post file and return a dict of named fields."""
    with open(filepath, "rb") as f:
        data = f.read()
    if len(data) != POST_SIZE:
        raise ValueError(f"Expected {POST_SIZE} bytes, got {len(data)}")
    
    result = {}
    for name, offset, fmt, desc in FIELDS:
        val = struct.unpack_from(f"<{fmt}", data, offset)[0]
        result[name] = val
    return result


def write_post(fields: dict, filepath: str):
    """Write a dict of named fields back to a .post file."""
    data = bytearray(POST_SIZE)
    for name, offset, fmt, desc in FIELDS:
        if name in fields:
            struct.pack_into(f"<{fmt}", data, offset, fields[name])
    with open(filepath, "wb") as f:
        f.write(data)


def post_to_json(fields: dict) -> dict:
    """Convert post fields to a JSON-serializable dict."""
    return {"fields": fields}


def cmd_dump(args):
    fields = read_post(args.input)
    output = json.dumps({"file": os.path.basename(args.input), "fields": fields}, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Dumped to {args.output}", file=sys.stderr)
    else:
        print(output)


def cmd_import(args):
    with open(args.json_input, "r") as f:
        data = json.load(f)
    fields = data["fields"] if "fields" in data else data
    output = args.output or args.original
    write_post(fields, output)
    print(f"Written to {output}", file=sys.stderr)


def cmd_batch_dump(args):
    indir = args.input_dir
    outdir = args.output_dir or os.path.join(os.path.dirname(__file__), "dump_post")
    
    count = 0
    for root, dirs, files in os.walk(indir):
        for fn in files:
            if not fn.endswith(".post"):
                continue
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, indir)
            out_path = os.path.join(outdir, rel.replace(".post", ".json"))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            try:
                fields = read_post(fp)
                with open(out_path, "w") as f:
                    json.dump({"file": fn, "fields": fields}, f, indent=2)
                count += 1
            except Exception as e:
                print(f"ERROR: {rel}: {e}", file=sys.stderr)
    print(f"Dumped {count} .post files to {outdir}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="post_tool — Parser/Editor for BDFFHD .post files")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("dump", help="Dump .post to JSON")
    p.add_argument("input", help="Path to .post file")
    p.add_argument("-o", "--output", help="Output JSON file")

    p = sub.add_parser("import", help="Import JSON back to .post")
    p.add_argument("original", help="Original .post file (for reference)")
    p.add_argument("json_input", help="Modified JSON file")
    p.add_argument("-o", "--output", help="Output .post file")

    p = sub.add_parser("batch-dump", help="Dump all .post files in a directory")
    p.add_argument("input_dir", help="Directory to scan")
    p.add_argument("-o", "--output-dir", help="Output directory")

    args = parser.parse_args()
    if args.command == "dump":
        cmd_dump(args)
    elif args.command == "import":
        cmd_import(args)
    elif args.command == "batch-dump":
        cmd_batch_dump(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
