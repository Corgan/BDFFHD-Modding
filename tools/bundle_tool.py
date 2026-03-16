#!/usr/bin/env python3
"""bundle_tool.py – AssetBundle inspector/extractor/repacker for BDFFHD.

Uses UnityPy to read/write Unity AssetBundle (UnityFS) files from the
Addressables folder.

Commands:
  info <bundle>           Show objects inside a bundle
  extract <bundle> [dir]  Extract all exportable assets (textures, sprites, etc.)
  replace <bundle> <asset_name> <file> [-o out.bundle]
                          Replace an asset inside a bundle
  batch-extract [dir]     Extract all textures/sprites from all bundles
  survey                  Show asset type distribution across all bundles
  find <name_pattern>     Find bundles containing assets matching a pattern

Typical texture swap workflow:
  1. python bundle_tool.py find "TitlePlay"
  2. python bundle_tool.py extract <bundle_hash>.bundle ./extracted/
  3. (edit the PNG)
  4. python bundle_tool.py replace <bundle_hash>.bundle TitlePlay_txt edited.png -o patched.bundle
  5. Copy patched.bundle over original in aa/StandaloneWindows64/
"""
import argparse
import fnmatch
import hashlib
import os
import struct
import sys

try:
    import UnityPy
except ImportError:
    print("ERROR: UnityPy not installed.  pip install UnityPy", file=sys.stderr)
    sys.exit(1)

BUNDLE_DIR = os.path.join(
    r"C:\Program Files (x86)\Steam\steamapps\common\BDFFHD",
    "BDFFHD_Data", "StreamingAssets", "aa", "StandaloneWindows64"
)

# ── helpers ──────────────────────────────────────────────────────────

def _resolve_bundle(path_or_name):
    """Resolve a bundle path — accept full path or just the hash/filename."""
    if os.path.isfile(path_or_name):
        return path_or_name
    candidate = os.path.join(BUNDLE_DIR, path_or_name)
    if os.path.isfile(candidate):
        return candidate
    if not path_or_name.endswith('.bundle'):
        candidate = os.path.join(BUNDLE_DIR, path_or_name + '.bundle')
        if os.path.isfile(candidate):
            return candidate
    print(f"ERROR: Cannot find bundle: {path_or_name}", file=sys.stderr)
    sys.exit(1)


def _obj_name(obj):
    """Get the name of a Unity object."""
    try:
        data = obj.read()
        return getattr(data, 'm_Name', None) or getattr(data, 'name', '???')
    except:
        return '???'


def _export_object(obj, out_dir):
    """Export a single object to disk.  Returns (filename, type) or None."""
    data = obj.read()
    name = getattr(data, 'm_Name', None) or getattr(data, 'name', None)
    if not name:
        return None

    typ = obj.type.name

    if typ == "Texture2D":
        img = data.image
        fname = f"{name}.png"
        img.save(os.path.join(out_dir, fname))
        return fname, typ

    if typ == "Sprite":
        img = data.image
        fname = f"{name}.png"
        img.save(os.path.join(out_dir, fname))
        return fname, typ

    if typ == "TextAsset":
        raw = data.m_Script
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        ext = ".txt"
        fname = f"{name}{ext}"
        with open(os.path.join(out_dir, fname), 'wb') as f:
            f.write(raw)
        return fname, typ

    if typ == "Shader":
        # Export shader source if available
        try:
            src = data.export()
            fname = f"{name}.shader"
            with open(os.path.join(out_dir, fname), 'wb') as f:
                f.write(src if isinstance(src, bytes) else src.encode('utf-8'))
            return fname, typ
        except:
            return None

    if typ == "Font":
        font_data = data.m_FontData
        if font_data:
            ext = ".otf" if font_data[:4] == b'OTTO' else ".ttf"
            fname = f"{name}{ext}"
            with open(os.path.join(out_dir, fname), 'wb') as f:
                f.write(font_data)
            return fname, typ

    if typ == "Mesh":
        try:
            fname = f"{name}.obj"
            with open(os.path.join(out_dir, fname), 'w') as f:
                f.write(data.export())
            return fname, typ
        except:
            return None

    if typ == "AnimationClip":
        # Can't easily export, but note its presence
        return None

    return None


# ── commands ─────────────────────────────────────────────────────────

def cmd_info(args):
    path = _resolve_bundle(args.bundle)
    env = UnityPy.load(path)
    sz = os.path.getsize(path)
    print(f"Bundle: {os.path.basename(path)} ({sz:,} bytes)")
    print(f"Unity: {env.file.version_engine}")
    print(f"Cab files: {list(env.files.keys())}")
    print()

    # Container paths
    container = getattr(env.file, 'container', {})
    if container:
        print("Container paths:")
        for cpath in sorted(container.keys()):
            print(f"  {cpath}")
        print()

    print(f"{'PathID':<22} {'Type':<24} {'Name'}")
    print("-" * 70)
    for obj in env.objects:
        name = _obj_name(obj)
        print(f"  {obj.path_id:<20} {obj.type.name:<24} {name}")


def cmd_extract(args):
    path = _resolve_bundle(args.bundle)
    out_dir = args.output or os.path.join("dump", "_bundle_extract")
    os.makedirs(out_dir, exist_ok=True)

    env = UnityPy.load(path)
    exported = 0
    for obj in env.objects:
        try:
            result = _export_object(obj, out_dir)
            if result:
                fname, typ = result
                print(f"  [{typ}] {fname}")
                exported += 1
        except Exception as e:
            name = _obj_name(obj)
            print(f"  [ERROR] {obj.type.name} {name}: {e}")

    print(f"\nExported {exported} assets to {out_dir}")


def cmd_replace(args):
    bundle_path = _resolve_bundle(args.bundle)
    asset_name = args.asset_name
    replace_file = args.file
    out_path = args.output or bundle_path  # overwrite in-place if no -o

    if not os.path.isfile(replace_file):
        print(f"ERROR: File not found: {replace_file}", file=sys.stderr)
        sys.exit(1)

    # Determine expected type from file extension
    ext = os.path.splitext(replace_file)[1].lower()
    preferred_types = {
        '.png': ("Texture2D", "Sprite"),
        '.jpg': ("Texture2D", "Sprite"),
        '.jpeg': ("Texture2D", "Sprite"),
        '.tga': ("Texture2D", "Sprite"),
        '.txt': ("TextAsset",),
        '.bytes': ("TextAsset",),
        '.ttf': ("Font",),
        '.otf': ("Font",),
    }
    want_types = preferred_types.get(ext)

    env = UnityPy.load(bundle_path)
    replaced = False

    for obj in env.objects:
        data = obj.read()
        name = getattr(data, 'm_Name', None) or getattr(data, 'name', None)
        if name != asset_name:
            continue

        typ = obj.type.name

        # Skip if this isn't the right type for our replacement file
        if want_types and typ not in want_types:
            continue

        if typ in ("Texture2D", "Sprite"):
            from PIL import Image
            img = Image.open(replace_file)
            data.image = img
            data.save()
            replaced = True
            print(f"  Replaced {typ} '{name}' with {replace_file}")
            break

        elif typ == "TextAsset":
            with open(replace_file, 'rb') as f:
                content = f.read()
            data.m_Script = content
            data.save()
            replaced = True
            print(f"  Replaced TextAsset '{name}' with {replace_file}")
            break

        elif typ == "Font":
            with open(replace_file, 'rb') as f:
                font_data = f.read()
            data.m_FontData = font_data
            data.save()
            replaced = True
            print(f"  Replaced Font '{name}' with {replace_file}")
            break

        else:
            print(f"  WARNING: Cannot replace type {typ} for '{name}'")
            break

    if not replaced:
        print(f"ERROR: Asset '{asset_name}' not found in bundle", file=sys.stderr)
        sys.exit(1)

    repacked = env.file.save(packer="original")
    with open(out_path, 'wb') as f:
        f.write(repacked)
    print(f"  Saved: {out_path} ({len(repacked):,} bytes)")


def cmd_find(args):
    pattern = args.pattern
    bundles = sorted(os.listdir(BUNDLE_DIR))
    matches = 0

    for name in bundles:
        if not name.endswith('.bundle'):
            continue
        path = os.path.join(BUNDLE_DIR, name)
        try:
            env = UnityPy.load(path)

            # Check container paths
            container = getattr(env.file, 'container', {})
            for cpath in container.keys():
                if fnmatch.fnmatch(cpath.lower(), f"*{pattern.lower()}*"):
                    print(f"  {name}  path={cpath}")
                    matches += 1
                    break
            else:
                # Check object names
                for obj in env.objects:
                    n = _obj_name(obj)
                    if fnmatch.fnmatch(str(n).lower(), f"*{pattern.lower()}*"):
                        print(f"  {name}  [{obj.type.name}] {n}")
                        matches += 1
                        break
        except:
            pass

    print(f"\nFound {matches} bundles matching '{pattern}'")


def cmd_survey(args):
    bundles = sorted(os.listdir(BUNDLE_DIR))
    total = sum(1 for b in bundles if b.endswith('.bundle'))
    type_counts = {}
    err = 0

    for i, name in enumerate(bundles):
        if not name.endswith('.bundle'):
            continue
        if (i + 1) % 1000 == 0:
            print(f"  Scanning {i+1}/{total}...", file=sys.stderr, flush=True)
        try:
            env = UnityPy.load(os.path.join(BUNDLE_DIR, name))
            for obj in env.objects:
                t = obj.type.name
                type_counts[t] = type_counts.get(t, 0) + 1
        except:
            err += 1

    print(f"\n{'Type':<30} {'Count':>8}")
    print("-" * 40)
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<28} {c:>8,}")
    print(f"\n{total} bundles scanned, {err} errors")


def cmd_batch_extract(args):
    """Extract all textures and sprites from all bundles."""
    out_dir = args.output or os.path.join("dump", "_all_textures")
    os.makedirs(out_dir, exist_ok=True)
    bundles = sorted(os.listdir(BUNDLE_DIR))
    total = sum(1 for b in bundles if b.endswith('.bundle'))
    exported = 0

    types_to_extract = {"Texture2D", "Sprite"}

    for i, name in enumerate(bundles):
        if not name.endswith('.bundle'):
            continue
        if (i + 1) % 500 == 0:
            print(f"  Scanning {i+1}/{total} (exported {exported})...",
                  file=sys.stderr, flush=True)
        try:
            env = UnityPy.load(os.path.join(BUNDLE_DIR, name))
            for obj in env.objects:
                if obj.type.name not in types_to_extract:
                    continue
                try:
                    result = _export_object(obj, out_dir)
                    if result:
                        exported += 1
                except:
                    pass
        except:
            pass

    print(f"\nExported {exported} textures/sprites to {out_dir}")


# ── main ─────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="BDFFHD AssetBundle tool (UnityPy-based)")
    sub = p.add_subparsers(dest="command")

    # info
    s = sub.add_parser("info", help="Inspect a bundle")
    s.add_argument("bundle")

    # extract
    s = sub.add_parser("extract", help="Extract assets from a bundle")
    s.add_argument("bundle")
    s.add_argument("output", nargs="?")

    # replace
    s = sub.add_parser("replace", help="Replace an asset in a bundle")
    s.add_argument("bundle")
    s.add_argument("asset_name")
    s.add_argument("file")
    s.add_argument("-o", "--output")

    # find
    s = sub.add_parser("find", help="Find bundles by asset name pattern")
    s.add_argument("pattern")

    # survey
    sub.add_parser("survey", help="Show asset type distribution")

    # batch-extract
    s = sub.add_parser("batch-extract", help="Extract all textures/sprites")
    s.add_argument("output", nargs="?")

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return

    cmds = {
        "info": cmd_info,
        "extract": cmd_extract,
        "replace": cmd_replace,
        "find": cmd_find,
        "survey": cmd_survey,
        "batch-extract": cmd_batch_extract,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
