# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Reopened copies are capped like snapshots (5 Nov: twenty GB of reopened 2 GB copies piled up in restored/).
Run: blender -b --factory-startup --python tests/test_restored_cap.py"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy  # noqa: F401
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_restored_")
from unix import recovery

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:400]}"))
    if not cond:
        fails.append(label)


folder = _util.data_dir("restored")
recovery.RESTORED_GB = 1000 / 1024 ** 3        # 1000 bytes: tiny files, nothing fills a real disk


def make(name, size, age):
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    os.utime(path, (time.time() - age, time.time() - age))
    return name


mine = make("Ranch (my save).blend", 900, 900)         # a file the user named: never touched
olds = [make(f"hj_recovered_0{i}0000.blend", 100, 800 - i * 100) for i in range(5)]
recovery._prune_restored(folder, 100)
left = sorted(os.listdir(folder))
check("only the newest two of Unix's copies stay (with the new one, three)", left == sorted([mine] + olds[-2:]), left)
check("a file the user named stays", mine in left)

for name in os.listdir(folder):
    os.remove(os.path.join(folder, name))
olds = [make(f"big_recovered_0{i}0000.blend", 400, 500 - i * 100) for i in range(2)]
recovery._prune_restored(folder, 400)
check("over the size cap with the coming copy: the oldest goes", sorted(os.listdir(folder)) == [olds[-1]], os.listdir(folder))
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
