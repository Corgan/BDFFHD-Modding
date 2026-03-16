#!/usr/bin/env python3
"""dump_all.py – Dump every data file in StreamingAssets to JSON/text.

Usage:
  python dump_all.py [output_dir]

Dumps:
  - BTBF files (.btb, .spb, .txb, .trb, .mtb, .tbl, .subtitles, .bak) → JSON via btb_tool
  - SBS files (.btb2, .tbl2) → JSON via sbs_tool
  - BASB files (.bab) → JSON via bab_tool
  - AMX files (.amx) → JSON via amx_tool
  - CCMF files (.ccm) → JSON via ccm_tool
  - POST files (.post) → JSON via post_tool
  - Plain text (.txt, .dat[text]) → copied as-is
  - Binary (.bin, .xzy, .tbl[non-BTBF]) → hex info only

Skips: AssetBundles (.bundle), audio (.acb/.acf), video (.dat[WebM])
"""
import os, sys, json, time, traceback
from pathlib import Path

GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\BDFFHD\BDFFHD_Data\StreamingAssets")
DEFAULT_OUT = Path(__file__).parent / "dump" / "full"

# Import tools
sys.path.insert(0, str(Path(__file__).parent))
from btb_tool import read_btb, btb_to_json, auto_detect_schema as btb_detect
from sbs_tool import read_sbs, sbs_to_json, auto_detect_schema as sbs_detect
from bab_tool import read_bab, bab_to_json
from amx_tool import read_amx, amx_to_json
from ccm_tool import read_ccm, ccm_to_json
from post_tool import read_post, post_to_json


def is_btbf(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'BTBF'
    except:
        return False


def is_sbs(path):
    try:
        with open(path, 'rb') as f:
            h = f.read(4)
            return h[2:4] == b'SS'
    except:
        return False


def is_webm(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'\x1a\x45\xdf\xa3'
    except:
        return False


def is_text(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            f.read(256)
        return True
    except:
        return False


def _analyze_binary_table(raw):
    """Parse a non-BTBF .tbl file into structural JSON with detected strings and ints."""
    import struct as st
    obj = {"size": len(raw), "ints": [], "strings": []}
    # Read all int32s
    for i in range(0, len(raw) - 3, 4):
        v = st.unpack_from("<i", raw, i)[0]
        if 0 < v < len(raw):
            obj["ints"].append({"offset": i, "value": v})
    obj["ints"] = obj["ints"][:64]
    # Extract ASCII strings
    i = 0
    while i < len(raw):
        if 0x20 <= raw[i] < 0x7f:
            start = i
            while i < len(raw) and 0x20 <= raw[i] < 0x7f:
                i += 1
            if i - start >= 3:
                obj["strings"].append({"offset": start, "text": raw[start:i].decode('ascii')})
        i += 1
    obj["hex"] = raw.hex()
    return obj


def _analyze_bin_file(raw):
    """Parse a .bin file — detect float arrays, camera data, etc."""
    import struct as st
    obj = {"size": len(raw)}
    # Try as float array
    if len(raw) % 4 == 0:
        floats = [st.unpack_from("<f", raw, i)[0] for i in range(0, len(raw), 4)]
        # Check if values are reasonable floats
        reasonable = sum(1 for f in floats if -10000 < f < 10000 and f == f)
        if reasonable > len(floats) * 0.8:
            obj["format"] = "float_array"
            obj["count"] = len(floats)
            obj["data"] = floats
            return obj
    obj["format"] = "raw"
    obj["hex"] = raw.hex()
    return obj


def dump_file(src, out_dir, rel):
    """Dump a single file, return (format_name, success)."""
    ext = src.suffix.lower()
    out_base = out_dir / rel

    # BTBF format
    if ext in ('.btb', '.spb', '.txb', '.trb', '.mtb', '.subtitles', '.bak') or (ext == '.tbl' and is_btbf(src)):
        out_path = out_base.with_suffix(out_base.suffix + '.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        btb = read_btb(str(src))
        schema = btb_detect(str(src))
        obj = btb_to_json(btb, schema)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "btb", True

    # SBS format
    if ext in ('.btb2', '.tbl2'):
        schema = sbs_detect(src)
        if not schema:
            # No schema — dump decompressed data as hex analysis
            import brotli
            raw = src.read_bytes()
            decompressed = brotli.decompress(raw[4:])
            out_path = out_base.with_suffix(out_base.suffix + '.json')
            out_path.parent.mkdir(parents=True, exist_ok=True)
            obj = {"format": "sbs-raw", "size": len(raw), "decompressed_size": len(decompressed),
                   "hex": decompressed.hex()}
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
            return "sbs-raw", True
        out_path = out_base.with_suffix(out_base.suffix + '.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        records, schema, remaining = read_sbs(str(src))
        obj = sbs_to_json(records, schema)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "sbs", True

    # BASB format
    if ext == '.bab':
        out_path = out_base.with_suffix('.bab.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        bab = read_bab(str(src))
        obj = bab_to_json(bab)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "bab", True

    # AMX format
    if ext == '.amx':
        out_path = out_base.with_suffix('.amx.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        amx = read_amx(str(src))
        obj = amx_to_json(amx)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "amx", True

    # CCMF format
    if ext == '.ccm':
        out_path = out_base.with_suffix('.ccm.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ccm = read_ccm(str(src))
        obj = ccm_to_json(ccm)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "ccm", True

    # POST format
    if ext == '.post':
        out_path = out_base.with_suffix('.post.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        post = read_post(str(src))
        obj = post_to_json(post)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "post", True

    # WebM video — skip (too large, not useful as JSON)
    if ext == '.dat' and is_webm(src):
        return "webm-skip", True

    # Audio — skip
    if ext in ('.acb', '.acf'):
        return "audio-skip", True

    # Plain text
    if ext == '.txt' or (ext == '.dat' and is_text(src)):
        out_path = out_base
        out_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(src, out_path)
        return "text", True

    # Non-BTBF .tbl files → binary table analysis JSON
    if ext == '.tbl' and not is_btbf(src):
        out_path = out_base.with_suffix('.tbl.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        raw = src.read_bytes()
        obj = _analyze_binary_table(raw)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "tbl-parsed", True

    # Binary files → structural analysis JSON
    if ext == '.bin':
        out_path = out_base.with_suffix('.bin.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        raw = src.read_bytes()
        obj = _analyze_bin_file(raw)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "bin-parsed", True

    # Encrypted/opaque files → hex dump JSON
    if ext == '.xzy':
        out_path = out_base.with_suffix('.xzy.json')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        import base64
        raw = src.read_bytes()
        obj = {"size": len(raw), "base64": base64.b64encode(raw).decode('ascii')}
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return "xzy-encoded", True

    # Unknown binary
    out_path = out_base
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(src, out_path)
    return "unknown-copy", True


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Source: {GAME_DIR}")
    print(f"Output: {out_dir}")
    print()

    stats = {}
    errors = []
    total = 0
    t0 = time.time()

    for root, dirs, files in os.walk(GAME_DIR):
        # Skip AssetBundles
        if 'aa' in dirs:
            dirs.remove('aa')
        
        for fname in sorted(files):
            src = Path(root) / fname
            rel = src.relative_to(GAME_DIR)
            total += 1

            try:
                fmt, ok = dump_file(src, out_dir, rel)
                stats[fmt] = stats.get(fmt, 0) + 1
            except Exception as e:
                errors.append((str(rel), str(e)))
                stats['error'] = stats.get('error', 0) + 1

            if total % 500 == 0:
                elapsed = time.time() - t0
                print(f"  {total} files processed ({elapsed:.1f}s)...", flush=True)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"Dumped {total} files in {elapsed:.1f}s to {out_dir}")
    print(f"\nFormat breakdown:")
    for fmt, count in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {fmt:<20} {count:>6}")

    if errors:
        print(f"\n{len(errors)} errors:")
        for path, err in errors[:20]:
            print(f"  {path}: {err}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")


if __name__ == "__main__":
    main()
