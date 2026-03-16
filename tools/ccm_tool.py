#!/usr/bin/env python3
"""
ccm_tool.py — Parser/Editor for BDFFHD .ccm collision mesh files.

CCMF format (collision mesh):
  Header       20 bytes: magic("CCMF") + version(u32) + flags(u32) + triCount(u32) + octCount(u32)
  GeometryBox 168 bytes: Scale(3f) + Rotation(4f) + Translation(3f) + Transform(4x4f) + InverseTransform(4x4f)
  Triangles   triCount × 40 bytes: Vertices(9 floats = 3 verts × xyz) + Info(u32)
  Octree:
    Node headers  octCount × 12 bytes: flags(u32) + tri_ref_count(u32) + has_children(u32)
    Triangle index list  (sum of tri_ref_counts) × 4 bytes

Usage:
    python ccm_tool.py dump   <file.ccm>              # Dump to JSON
    python ccm_tool.py import <file.ccm> <input.json> [-o output.ccm]
    python ccm_tool.py batch-dump <directory> [-o outdir]
"""
import struct
import json
import sys
import os
import argparse
from pathlib import Path

HEADER_SIZE = 20
BOX_SIZE = 168  # 42 floats
TRI_SIZE = 40   # 9 floats + 1 uint
NODE_SIZE = 12  # 3 uints


def read_ccm(filepath: str) -> dict:
    """Read a .ccm file and return a structured dict."""
    with open(filepath, "rb") as f:
        data = f.read()

    magic = data[:4]
    if magic != b'CCMF':
        raise ValueError(f"Bad magic: {magic!r}, expected b'CCMF'")

    version, flags, tri_count, oct_count = struct.unpack_from('<4I', data, 4)

    # GeometryBox: 42 floats starting at offset 20
    off = HEADER_SIZE
    scale = list(struct.unpack_from('<3f', data, off)); off += 12
    rotation = list(struct.unpack_from('<4f', data, off)); off += 16
    translation = list(struct.unpack_from('<3f', data, off)); off += 12
    transform = list(struct.unpack_from('<16f', data, off)); off += 64
    inverse_transform = list(struct.unpack_from('<16f', data, off)); off += 64

    # Triangles
    triangles = []
    for _ in range(tri_count):
        verts = list(struct.unpack_from('<9f', data, off))
        info = struct.unpack_from('<I', data, off + 36)[0]
        triangles.append({"vertices": verts, "info": info})
        off += TRI_SIZE

    # Octree node headers
    nodes = []
    for _ in range(oct_count):
        nflags, tri_ref_count, has_children = struct.unpack_from('<3I', data, off)
        nodes.append({
            "flags": nflags,
            "tri_ref_count": tri_ref_count,
            "has_children": has_children,
        })
        off += NODE_SIZE

    # Triangle index list
    total_refs = sum(n["tri_ref_count"] for n in nodes)
    tri_indices = list(struct.unpack_from(f'<{total_refs}I', data, off))
    off += total_refs * 4

    # Assign indices to nodes
    idx = 0
    for node in nodes:
        cnt = node["tri_ref_count"]
        node["triangle_indices"] = tri_indices[idx:idx + cnt]
        idx += cnt

    if off != len(data):
        raise ValueError(f"Trailing data: {len(data) - off} bytes at offset {off}")

    return {
        "version": version,
        "flags": flags,
        "geometry_box": {
            "scale": scale,
            "rotation": rotation,
            "translation": translation,
            "transform": transform,
            "inverse_transform": inverse_transform,
        },
        "triangles": triangles,
        "octree": nodes,
    }


def write_ccm(ccm: dict, filepath: str):
    """Write a structured dict back to a .ccm file."""
    tri_count = len(ccm["triangles"])
    oct_count = len(ccm["octree"])

    # Compute total triangle refs
    total_refs = sum(n["tri_ref_count"] for n in ccm["octree"])

    total_size = HEADER_SIZE + BOX_SIZE + tri_count * TRI_SIZE + oct_count * NODE_SIZE + total_refs * 4
    buf = bytearray(total_size)

    # Header
    struct.pack_into('<4s4I', buf, 0, b'CCMF',
                     ccm["version"], ccm["flags"], tri_count, oct_count)

    # GeometryBox
    off = HEADER_SIZE
    box = ccm["geometry_box"]
    struct.pack_into('<3f', buf, off, *box["scale"]); off += 12
    struct.pack_into('<4f', buf, off, *box["rotation"]); off += 16
    struct.pack_into('<3f', buf, off, *box["translation"]); off += 12
    struct.pack_into('<16f', buf, off, *box["transform"]); off += 64
    struct.pack_into('<16f', buf, off, *box["inverse_transform"]); off += 64

    # Triangles
    for tri in ccm["triangles"]:
        struct.pack_into('<9f', buf, off, *tri["vertices"])
        struct.pack_into('<I', buf, off + 36, tri["info"])
        off += TRI_SIZE

    # Octree node headers
    for node in ccm["octree"]:
        struct.pack_into('<3I', buf, off,
                         node["flags"], node["tri_ref_count"], node["has_children"])
        off += NODE_SIZE

    # Triangle index list (flat, in node order)
    for node in ccm["octree"]:
        for idx in node["triangle_indices"]:
            struct.pack_into('<I', buf, off, idx)
            off += 4

    with open(filepath, "wb") as f:
        f.write(buf)


def ccm_to_json(ccm: dict) -> dict:
    """Convert ccm data to a JSON-serializable dict."""
    return ccm


def cmd_dump(args):
    ccm = read_ccm(args.input)
    result = {"file": os.path.basename(args.input), **ccm}
    output = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Dumped to {args.output}", file=sys.stderr)
    else:
        print(output)


def cmd_import(args):
    with open(args.json_input, "r") as f:
        data = json.load(f)
    # Strip 'file' key if present
    data.pop("file", None)
    output = args.output or args.original
    write_ccm(data, output)
    print(f"Written to {output}", file=sys.stderr)


def cmd_batch_dump(args):
    indir = args.input_dir
    outdir = args.output_dir or os.path.join(os.path.dirname(__file__), "dump_ccm")

    count = 0
    for root, dirs, files in os.walk(indir):
        for fn in files:
            if not fn.endswith(".ccm"):
                continue
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, indir)
            out_path = os.path.join(outdir, rel.replace(".ccm", ".json"))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            try:
                ccm = read_ccm(fp)
                result = {"file": fn, **ccm}
                with open(out_path, "w") as f:
                    json.dump(result, f, indent=2)
                count += 1
            except Exception as e:
                print(f"ERROR: {rel}: {e}", file=sys.stderr)
    print(f"Dumped {count} .ccm files to {outdir}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="ccm_tool — Parser/Editor for BDFFHD .ccm collision mesh files")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("dump", help="Dump .ccm to JSON")
    p.add_argument("input", help="Path to .ccm file")
    p.add_argument("-o", "--output", help="Output JSON file")

    p = sub.add_parser("import", help="Import JSON back to .ccm")
    p.add_argument("original", help="Original .ccm file (for reference)")
    p.add_argument("json_input", help="Modified JSON file")
    p.add_argument("-o", "--output", help="Output .ccm file")

    p = sub.add_parser("batch-dump", help="Dump all .ccm files in a directory")
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
