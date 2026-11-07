# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Scene copies are kept by size and free disk, not only by count (5 Nov: twenty 2 GB copies filled the disk).
Stand-in files and a fake disk; one real copy at the end. Run: blender -b --factory-startup --python tests/test_snapshot_room.py"""
import collections, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy  # noqa: F401
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_room_")
from unix.tools import files

fails = []
files.GB = GB = 1024        # a "GB" of 1 KB here: stand-ins must not fill a real disk


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:400]}"))
    if not cond:
        fails.append(label)


folder = _util.data_dir("snapshots")
real_usage = files.shutil.disk_usage
disk = {"free": 100 * GB}
files.shutil.disk_usage = lambda _p: collections.namedtuple("u", "total used free")(0, 0, disk["free"])


def fill(sizes):
    for name in os.listdir(folder):
        os.remove(os.path.join(folder, name))
    for index, size in enumerate(sizes):
        with open(os.path.join(folder, f"2026110{index // 10}_{index % 10:02d}0000_before_turn.blend"), "wb") as fh:
            fh.truncate(size)       # sparse: the size is all that is read
    return sorted(os.listdir(folder))


def left():
    return sorted(os.listdir(folder))


names = fill([1 * GB] * 10)
files.SNAPSHOTS_GB = 6.0
check("room is made for a 2 GB copy: the oldest go until all fit in 6 GB", files._make_room(folder, 2 * GB)
      and left() == names[-4:], left())
names = fill([10] * 25)
files.SNAPSHOTS_GB = 12.0
files._make_room(folder, 0)
check("still at most 20 copies, however small", len(left()) == files.KEEP_SNAPSHOTS - 1 and left()[-1] == names[-1], len(left()))
names = fill([1 * GB] * 5)
disk["free"] = 3 * GB
ok = files._make_room(folder, 2 * GB)
check("a nearly full disk: the oldest go, the newest three always stay", left() == names[-3:], left())
check("...and there is room for the 2 GB copy once they are gone", ok)
disk["free"] = int(0.5 * GB)
check("no room at all: says so (the copy is not written)", not files._make_room(folder, 2 * GB))
check("...and still keeps the newest three", left() == names[-3:], left())
names = fill([1 * GB] * 3)
check("no room at all: snapshot() writes nothing and returns None", files.snapshot("nope") is None and left() == names)

files.shutil.disk_usage = real_usage
fill([])
path = files.snapshot("real")
check("a real copy is written and kept", path and os.path.isfile(path) and os.path.getsize(path) > 0, path)
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
