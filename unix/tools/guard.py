# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Static checks and rewrites for model-written scripts. No ``bpy`` import.

This is defence in depth, not a sandbox: scripts run in-process with full Python.
The guard stops the obviously destructive or exfiltrating things a confused or
prompt-injected model might write, and explains the refusal so the model can adapt.
``unrestricted=True`` skips the denylist (the rewrite still applies).
"""

from __future__ import annotations

import ast
from typing import List, Optional, Tuple

BLOCKED_IMPORTS = {
    "subprocess", "socket", "ctypes", "multiprocessing", "http", "urllib", "requests", "ftplib",
    "smtplib", "telnetlib", "webbrowser", "winreg", "shutil", "pty", "signal",
}
# attribute chains (dotted) that must not be called
BLOCKED_CALLS = {
    "os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename",
    "os.replace", "os.startfile", "os.kill", "os.chmod", "os.execv", "os.execl", "os.spawnl", "os.spawnv",
    "bpy.ops.wm.quit_blender", "bpy.ops.wm.read_factory_settings", "bpy.ops.wm.read_homefile",
    "bpy.ops.wm.open_mainfile", "bpy.ops.wm.revert_mainfile", "bpy.ops.wm.save_mainfile",
    "bpy.ops.wm.save_as_mainfile", "bpy.ops.wm.save_userpref", "bpy.ops.preferences.addon_install",
    "bpy.ops.preferences.addon_remove", "bpy.ops.preferences.addon_disable", "bpy.ops.wm.url_open",
    "bpy.ops.script.python_file_run", "bpy.ops.wm.console_toggle",
}
BLOCKED_NAMES = {"eval", "exec", "compile", "__import__", "breakpoint", "exit", "quit"}
# Depsgraph iterators hand out items that are only valid during the iteration itself,
# so they must not be copied into a list.
_NO_SNAPSHOT_ATTRS = {"object_instances"}


def _dotted(node: ast.AST) -> Optional[str]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.problems: List[str] = []

    def _add(self, node: ast.AST, message: str) -> None:
        self.problems.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] in BLOCKED_IMPORTS:
                self._add(node, f"import of '{alias.name}' is not allowed")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").split(".")[0] in BLOCKED_IMPORTS:
            self._add(node, f"import from '{node.module}' is not allowed")

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        if name in BLOCKED_CALLS:
            self._add(node, f"call to {name}() is not allowed")
        elif name in BLOCKED_NAMES:
            self._add(node, f"{name}() is not allowed")
        self.generic_visit(node)


class _SafeIteration(ast.NodeTransformer):
    """``for x in obj.modifiers:`` -> ``for x in list(obj.modifiers):``."""

    def visit_For(self, node: ast.For) -> ast.For:
        self.generic_visit(node)
        it = node.iter
        if isinstance(it, ast.Attribute) and it.attr in _NO_SNAPSHOT_ATTRS:
            return node
        if isinstance(it, (ast.Attribute, ast.Subscript)):
            node.iter = ast.copy_location(
                ast.Call(func=ast.Name(id="list", ctx=ast.Load()), args=[it], keywords=[]), it)
        return node


class _WindowContext(ast.NodeTransformer):
    """``bpy.context.temp_override(area=a)`` -> ``_unix_window_context(bpy.context, area=a)``.

    Inside the window pump the context already is the 3D viewport, and an override of the same
    window, area and region only puts back the Python dict that made operators crash Blender while
    other threads ran (``ui/window_pump.py``). ``tools._util.window_context`` skips it then and
    makes the real override otherwise."""

    used = False

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "temp_override":
            self.used = True
            return ast.copy_location(ast.Call(func=ast.Name(id="_unix_window_context", ctx=ast.Load()),
                                              args=[func.value, *node.args], keywords=node.keywords), node)
        return node


def prepare(code: str, unrestricted: bool = False) -> Tuple[Optional[object], Optional[str]]:
    """Returns (code_object, None) or (None, error message for the model)."""
    try:
        tree = ast.parse(code, filename="<unix>", mode="exec")
    except SyntaxError as exc:
        line = (exc.text or "").rstrip()
        return None, f"SyntaxError at line {exc.lineno}: {exc.msg}\n    {line}"
    if not unrestricted:
        checker = _Checker()
        checker.visit(tree)
        if checker.problems:
            return None, ("Script refused by the safety guard:\n  " + "\n  ".join(checker.problems[:8]) +
                          "\nUse the dedicated tools for files (import_file, export_file) and keep scripts "
                          "to scene work. The user can enable unrestricted scripts in the add-on preferences.")
    tree = _SafeIteration().visit(tree)
    windows = _WindowContext()
    tree = windows.visit(tree)
    if windows.used:
        tree.body.insert(0, ast.ImportFrom(module=f"{__package__}._util", level=0,
                                           names=[ast.alias(name="window_context", asname="_unix_window_context")]))
    tree = ast.fix_missing_locations(tree)
    return compile(tree, "<unix>", "exec"), None
