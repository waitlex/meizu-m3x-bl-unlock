#!/usr/bin/env python3
"""Inject the Flyme dark-status-bar fix (meizuFlags |= 0x200) into a decompiled Activity.

Works on a baksmali/apktool output tree: it inserts one `invoke-static` right after the
super `onCreate(Bundle)` call and appends a self-contained `sbarFix` helper method.
The helper keeps its own stack frame, so the host method's .registers is never touched
(invoke-* can only encode v0-v15; bumping a 2-arg method's registers breaks assembly).

Usage:
    python3 patch_statusbar.py <smali_root> <activity_rel_path> [--dry-run]
    python3 patch_statusbar.py --self-test

Example:
    python3 patch_statusbar.py build/apk/smali mark/via/Shell.smali
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

PKG = "Landroid/app/Activity;"
HELPER_NAME = "sbarFix"

HELPER = """
.method private static sbarFix(Landroid/app/Activity;)V
    .registers 8
    :try_start_0
    invoke-virtual {p0}, Landroid/app/Activity;->getWindow()Landroid/view/Window;
    move-result-object v0
    invoke-virtual {v0}, Landroid/view/Window;->getAttributes()Landroid/view/WindowManager$LayoutParams;
    move-result-object v1
    const-class v2, Landroid/view/WindowManager$LayoutParams;
    const-string v3, "meizuFlags"
    invoke-virtual {v2, v3}, Ljava/lang/Class;->getDeclaredField(Ljava/lang/String;)Ljava/lang/reflect/Field;
    move-result-object v2
    const/4 v3, 0x1
    invoke-virtual {v2, v3}, Ljava/lang/reflect/Field;->setAccessible(Z)V
    const-class v4, Landroid/view/WindowManager$LayoutParams;
    const-string v5, "MEIZU_FLAG_DARK_STATUS_BAR_ICON"
    invoke-virtual {v4, v5}, Ljava/lang/Class;->getDeclaredField(Ljava/lang/String;)Ljava/lang/reflect/Field;
    move-result-object v4
    const/4 v5, 0x0
    invoke-virtual {v4, v5}, Ljava/lang/reflect/Field;->setAccessible(Z)V
    invoke-virtual {v4, v5}, Ljava/lang/reflect/Field;->getInt(Ljava/lang/Object;)I
    move-result v4
    invoke-virtual {v2, v1}, Ljava/lang/reflect/Field;->getInt(Ljava/lang/Object;)I
    move-result v5
    or-int/2addr v5, v4
    invoke-virtual {v2, v1, v5}, Ljava/lang/reflect/Field;->setInt(Ljava/lang/Object;I)V
    invoke-virtual {v0, v1}, Landroid/view/Window;->setAttributes(Landroid/view/WindowManager$LayoutParams;)V
    :try_end_0
    .catch Ljava/lang/Throwable; {:try_start_0 .. :try_end_0} :catch_0
    goto :goto_0
    :catch_0
    move-exception v0
    :goto_0
    return-void
.end method
"""

METHOD_RE = re.compile(
    r"^\.method\s+[^\n]*\bonCreate\(Landroid/os/Bundle;\)V\s*$", re.M
)
SUPER_RE = re.compile(
    r"^(\s*)invoke-(?:super|direct) \{p0(?:, p1)?\}, [^\n]*->onCreate\(Landroid/os/Bundle;\)V\s*$",
    re.M,
)


class PatchError(Exception):
    pass


def class_descriptor(rel_path: str) -> str:
    rel = rel_path.replace(os.sep, "/").lstrip("/")
    if not rel.endswith(".smali"):
        raise PatchError(f"not a .smali path: {rel_path}")
    return "L" + rel[: -len(".smali")] + ";"


def patch_text(text: str, desc: str) -> tuple[str, str]:
    """Return (new_text, status). status in {patched, already}."""
    if HELPER_NAME in text:
        return text, "already"

    m = METHOD_RE.search(text)
    if not m:
        raise PatchError("onCreate(Landroid/os/Bundle;)V not found")
    block_end = text.find(".end method", m.end())
    if block_end < 0:
        raise PatchError("unterminated onCreate method")
    block = text[m.start():block_end]

    sm = SUPER_RE.search(block)
    if not sm:
        raise PatchError("super.onCreate(Bundle) not found inside onCreate")
    indent = sm.group(1)
    insert_at = m.start() + sm.end()
    call = f"\n{indent}invoke-static {{p0}}, {desc}->{HELPER_NAME}({PKG})V"

    new_text = text[:insert_at] + call + text[insert_at:]
    new_text = new_text.rstrip() + "\n" + HELPER
    return new_text, "patched"


def run(root: str, rel: str, dry_run: bool) -> int:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise PatchError(f"missing file: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    new_text, status = patch_text(text, class_descriptor(rel))
    if status == "already":
        print(f"SKIP already patched: {rel}")
        return 0
    if dry_run:
        print(f"DRY_RUN would patch {rel} ({len(new_text) - len(text):+d} bytes)")
        return 0

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    print(f"PATCHED {rel}")
    print("next: reassemble (smali), drop META-INF signatures, re-sign, then adb install -r")
    print("verify: dumpsys window | grep -E 'meizuFlags=0x200|vsysui=0x2[67]00'")
    return 0


def self_test() -> int:
    sample = """.class public Lmark/via/Shell;
.super Landroidx/fragment/app/q;

.method protected onCreate(Landroid/os/Bundle;)V
    .registers 10
    invoke-super {p0, p1}, Landroidx/fragment/app/q;->onCreate(Landroid/os/Bundle;)V
    return-void
.end method
"""
    failures = []

    def check(name: str, cond: bool) -> None:
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "mark", "via"))
        rel = os.path.join("mark", "via", "Shell.smali")
        path = os.path.join(tmp, rel)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sample)

        run(tmp, rel, dry_run=False)
        with open(path, encoding="utf-8") as fh:
            once = fh.read()
        check("call inserted", "invoke-static {p0}, Lmark/via/Shell;->sbarFix(Landroid/app/Activity;)V" in once)
        check("helper appended", ".method private static sbarFix(Landroid/app/Activity;)V" in once)
        check("helper uses own frame", ".registers 8" in once)
        check("host registers untouched", ".registers 10" in once)
        check("helper after super", once.index("invoke-static") > once.index("invoke-super"))
        check("catch Throwable", ".catch Ljava/lang/Throwable;" in once)

        run(tmp, rel, dry_run=False)
        with open(path, encoding="utf-8") as fh:
            twice = fh.read()
        check("idempotent", twice.count("sbarFix") == 2)

        missing = os.path.join(tmp, "mark", "via")
        os.makedirs(missing, exist_ok=True)
        with open(os.path.join(missing, "NoOnCreate.smali"), "w", encoding="utf-8") as fh:
            fh.write(".class public Lmark/via/NoOnCreate;\n.super Landroid/app/Activity;\n")
        try:
            run(tmp, os.path.join("mark", "via", "NoOnCreate.smali"), dry_run=False)
            check("missing onCreate rejected", False)
        except PatchError:
            check("missing onCreate rejected", True)

    print(f"errors={len(failures)}")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("smali_root", nargs="?", help="root of decompiled smali tree")
    ap.add_argument("activity", nargs="?", help="Activity path relative to smali_root, e.g. mark/via/Shell.smali")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.smali_root or not args.activity:
        ap.print_help()
        return 2
    try:
        return run(args.smali_root, args.activity, args.dry_run)
    except PatchError as exc:
        print(f"ERROR {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
