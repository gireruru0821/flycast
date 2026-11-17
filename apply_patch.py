#!/usr/bin/env python3
"""Patch flyinghead/flycast LIBRETRO to load barcode text on L press.

No game ROM or BIOS is distributed or modified.
"""
from __future__ import annotations
import argparse
import difflib
from pathlib import Path
import re
import sys

MARK = 'MUSHIKING_BARCODE_FILE_V1'
CARD = Path('core/hw/naomi/card_reader.cpp')
LIBRETRO = Path('shell/libretro/libretro.cpp')

# Original master source already has BarcodeReader and barcode insertion.
CARD_ANCHOR = '''\tvoid insertCard()\n\t{\n\t\tif (toSend.size() >= 32)\n\t\t\treturn;\n\t\tINFO_LOG(NAOMI, "Card read: %s", card.c_str());'''

CARD_NEW = '''\tvoid insertCard()\n\t{\n#ifdef LIBRETRO\n\t\t// MUSHIKING_BARCODE_FILE_V1: read one barcode at the moment of insertion.\n\t\t// This remains separate from the normal standalone Flycast UI.\n\t\tif (!loadBarcodeText())\n\t\t\treturn;\n#endif\n\t\tif (toSend.size() >= 32)\n\t\t\treturn;\n\t\tINFO_LOG(NAOMI, "Card read: %s", card.c_str());'''

# Insert helper just after private: in BarcodeReader and before eotChar field.
CARD_PRIVATE = '''private:\n\tconst char eotChar;\n\tstd::deque<u8> toSend;'''
CARD_PRIVATE_NEW = '''private:\n#ifdef LIBRETRO\n\t// MUSHIKING_BARCODE_FILE_V1. File is in the per-game RetroArch save dir,\n\t// e.g. <Save Files>/reicast/mushi2k62.barcode.txt.\n\tbool loadBarcodeText()\n\t{\n\t\tconst std::string filename = hostfs::getArcadeFlashPath() + ".barcode.txt";\n\t\tFILE *file = nowide::fopen(filename.c_str(), "rb");\n\t\tif (file == nullptr)\n\t\t{\n\t\t\tWARN_LOG(NAOMI, "Barcode file missing: %s", filename.c_str());\n\t\t\treturn false;\n\t\t}\n\t\tchar buffer[256] = {};\n\t\tconst size_t len = fread(buffer, 1, sizeof(buffer) - 1, file);\n\t\tfclose(file);\n\t\tif (len == sizeof(buffer) - 1)\n\t\t{\n\t\t\tWARN_LOG(NAOMI, "Barcode input too long: %s", filename.c_str());\n\t\t\treturn false;\n\t\t}\n\t\tstd::string value(buffer, len);\n\t\t// Accept one line and tolerate UTF-8 BOM from Android text editors.\n\t\tif (value.compare(0, 3, "\\xEF\\xBB\\xBF") == 0)\n\t\t\tvalue.erase(0, 3);\n\t\tconst size_t lineEnd = value.find_first_of("\\r\\n");\n\t\tif (lineEnd != std::string::npos)\n\t\t\tvalue.resize(lineEnd);\n\t\tconst size_t begin = value.find_first_not_of(" \\t");\n\t\tif (begin == std::string::npos)\n\t\t\treturn false;\n\t\tvalue.erase(0, begin);\n\t\tconst size_t last = value.find_last_not_of(" \\t");\n\t\tvalue.erase(last + 1);\n\t\t// Physical Code39 labels sometimes show the framing '*'. The existing\n\t\t// BarcodeReader::insertCard() already appends the reader's terminator.\n\t\tif (value.size() >= 2 && value.front() == '*' && value.back() == '*')\n\t\t\tvalue = value.substr(1, value.size() - 2);\n\t\tif (value.empty() || value.size() > 128)\n\t\t{\n\t\t\tWARN_LOG(NAOMI, "Barcode must be 1-128 ASCII characters");\n\t\t\treturn false;\n\t\t}\n\t\tfor (unsigned char ch : value)\n\t\t{\n\t\t\tif (ch < 0x20 || ch > 0x7e || ch == '*')\n\t\t\t{\n\t\t\t\tWARN_LOG(NAOMI, "Invalid barcode character");\n\t\t\t\treturn false;\n\t\t\t}\n\t\t}\n\t\tsetCard(value);\n\t\tINFO_LOG(NAOMI, "Barcode text loaded from %s", filename.c_str());\n\t\treturn true;\n\t}\n#endif\n\tconst char eotChar;\n\tstd::deque<u8> toSend;'''

LIBRETRO_BUTTON_PATTERN = re.compile(
    r'(?P<indent>[ \t]*)case RETRO_DEVICE_ID_JOYPAD_L:\s*'
    r'if \(haveCardReader\)\s*\{\s*'
    r'if \(ret & \(1 << RETRO_DEVICE_ID_JOYPAD_L\)\)\s*'
    r'card_reader::insertCard\(port\);\s*\}'
)

def replace_exact(text: str, old: str, new: str, target: str) -> str:
    num = text.count(old)
    if num != 1:
        raise RuntimeError(f'{target}: expected 1 anchor, found {num}; source may have changed')
    return text.replace(old, new, 1)


def patch_card(text: str) -> str:
    if MARK in text:
        return text
    text = replace_exact(text, CARD_ANCHOR, CARD_NEW, str(CARD))
    text = replace_exact(text, CARD_PRIVATE, CARD_PRIVATE_NEW, str(CARD))
    # stdio prototypes can be included transitively in the upstream source,
    # but explicit inclusion is more portable.
    text = replace_exact(text, '#include <cerrno>\n', '#include <cerrno>\n#include <cstdio>\n', str(CARD))
    return text


def patch_libretro(text: str) -> str:
    if MARK in text:
        return text
    text = replace_exact(text, 'static bool select_pressed[4] = {false};',
        'static bool select_pressed[4] = {false};\n'
        '// MUSHIKING_BARCODE_FILE_V1: insert a card only on the L button down-edge.\n'
        'static bool mushi_card_l_was_down[4] = {false, false, false, false};', str(LIBRETRO))
    matches = list(LIBRETRO_BUTTON_PATTERN.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f'{LIBRETRO}: expected exactly 1 L button handler, found {len(matches)}')
    match = matches[0]
    ind = match.group('indent')
    # The 'else' after the selected block is still in upstream code.
    out = (ind + 'case RETRO_DEVICE_ID_JOYPAD_L:\n'
           + ind + '\tif (haveCardReader)\n'
           + ind + '\t{\n'
           + ind + '\t\tconst bool down = (ret & (1 << RETRO_DEVICE_ID_JOYPAD_L)) != 0;\n'
           + ind + '\t\tif (down && !mushi_card_l_was_down[port])\n'
           + ind + '\t\t\tcard_reader::insertCard(port);\n'
           + ind + '\t\tmushi_card_l_was_down[port] = down;\n'
           + ind + '\t}')
    text = text[:match.start()] + out + text[match.end():]
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description='Patch Flycast LIBRETRO for barcode-file card input')
    ap.add_argument('root', nargs='?', default='.', help='Flycast source root')
    ap.add_argument('--dry-run', action='store_true', help='Validate and show change summary without writing')
    ap.add_argument('--diff', action='store_true', help='Print unified diff without writing')
    args = ap.parse_args()
    root = Path(args.root)
    files = ((CARD, patch_card), (LIBRETRO, patch_libretro))
    changes = []
    for file, func in files:
        path = root / file
        if not path.is_file():
            raise SystemExit(f'Missing source file: {path}\nUse a recursive checkout of https://github.com/flyinghead/flycast')
        old = path.read_text(encoding='utf-8')
        new = func(old)
        changes.append((path, file, old, new))
    for path, relative, old, new in changes:
        if args.diff:
            print(''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                fromfile=f'a/{relative}', tofile=f'b/{relative}')))
        elif args.dry_run:
            print(f'{relative}: {"already patched" if old == new else "ready to patch"}')
        else:
            if old != new:
                path.write_text(new, encoding='utf-8')
            print(f'{relative}: {"already patched" if old == new else "patched"}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, UnicodeError) as exc:
        sys.exit(f'Patch failed safely: {exc}')
