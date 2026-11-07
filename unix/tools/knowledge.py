# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Knowledge tools: the skill library and a live lookup of this Blender's own API.

``api_lookup`` reads RNA from the running Blender, so the answer always matches the
installed version. That removes the most common failure of model-written bpy code:
calling an operator or property that was renamed or never existed.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import bpy

from .registry import ToolError, tool

SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_cache: Dict[str, Dict[str, str]] = {}
_cache_stamp = 0.0


def _load_skills() -> Dict[str, Dict[str, str]]:
    """{name: {domain, when, body, path}} from skills/<domain>/<name>.md (cached by mtime)."""
    global _cache, _cache_stamp
    stamp = 0.0
    files: List[str] = []
    for root, _dirs, names in os.walk(SKILLS_DIR):
        for name in names:
            if name.endswith(".md") and name.lower() != "readme.md":
                path = os.path.join(root, name)
                files.append(path)
                stamp = max(stamp, os.path.getmtime(path))
    if _cache and stamp == _cache_stamp and len(_cache) == len(files):
        return _cache
    skills: Dict[str, Dict[str, str]] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        match = _FRONT.match(text)
        meta: Dict[str, str] = {}
        body = text
        if match:
            body = match.group(2).strip()
            for line in match.group(1).splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip()] = value.strip()
        name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
        skills[name] = {"domain": meta.get("domain") or os.path.basename(os.path.dirname(path)),
                        "when": meta.get("when", ""), "body": body, "path": path}
    _cache, _cache_stamp = skills, stamp
    return skills


def skill_index() -> str:
    """Compact domain -> skill names index for the system prompt."""
    by_domain: Dict[str, List[str]] = {}
    for name, skill in _load_skills().items():
        by_domain.setdefault(skill["domain"], []).append(name)
    return "\n".join(f"- {domain}: {', '.join(sorted(names))}" for domain, names in sorted(by_domain.items()))


@tool(
    "skill_list",
    """List the skill library: short expert playbooks for specific kinds of 3D work. Each entry
    shows the skill name and when to use it. Pass a domain to narrow the list.""",
    {"domain": {"type": "string", "description": "Optional domain filter, e.g. modeling, uv, lighting, materials, rigging."}},
    main_thread=False,
    label="Browsed skills",
)
def skill_list(domain: str = "") -> Dict[str, Any]:
    skills = _load_skills()
    domains = sorted({s["domain"] for s in skills.values()})
    if domain and domain not in domains:
        raise ToolError(f"Unknown domain '{domain}'. Domains: {', '.join(domains)}")
    rows = [{"name": n, "domain": s["domain"], "when": s["when"]} for n, s in sorted(skills.items())
            if not domain or s["domain"] == domain]
    return {"domains": domains, "skills": rows}


@tool(
    "skill_load",
    """Load up to 6 skills by name and read their full instructions. Do this before starting a
    stage of work the skill covers (modeling, UVs, materials, lighting, rigging, export...) and
    follow them; each ends with a Check line to verify against.""",
    {"names": {"type": "array", "items": {"type": "string"}, "description": "Skill names from skill_list (max 6)."}},
    ["names"],
    main_thread=False,
    label="Loaded skills",
)
def skill_load(names: List[str]) -> str:
    skills = _load_skills()
    parts, missing = [], []
    for name in names[:6]:
        skill = skills.get(name)
        if skill is None:
            missing.append(name)
        else:
            parts.append(f"# SKILL: {name} ({skill['domain']})\nUse when: {skill['when']}\n\n{skill['body']}")
    if missing:
        close = [n for n in skills if any(m.split("-")[0] in n for m in missing)][:10]
        parts.append(f"[not found: {', '.join(missing)}" + (f"; similar: {', '.join(close)}]" if close else "]"))
    if not parts:
        raise ToolError("No skill names given.")
    return "\n\n---\n\n".join(parts)


def _prop_line(prop: bpy.types.Property) -> str:
    kind = prop.type.lower()
    extra = ""
    if prop.type == 'ENUM':
        items = [i.identifier for i in prop.enum_items][:24]
        extra = f" in {items}"
    elif prop.type in {'INT', 'FLOAT'}:
        if getattr(prop, "array_length", 0):
            kind += f"[{prop.array_length}]"
        try:
            extra = f" (default {prop.default if not getattr(prop, 'is_array', False) else list(prop.default_array)})"
        except Exception:
            extra = ""
    elif prop.type == 'BOOLEAN':
        extra = f" (default {getattr(prop, 'default', '')})"
    elif prop.type in {'POINTER', 'COLLECTION'} and prop.fixed_type is not None:
        kind += f"<{prop.fixed_type.identifier}>"
    ro = " [readonly]" if prop.is_readonly and prop.type not in {'POINTER', 'COLLECTION'} else ""
    desc = f" - {prop.description}" if prop.description else ""
    return f"  {prop.identifier}: {kind}{extra}{ro}{desc}"[:230]


def _describe_operator(module: str, name: str) -> Optional[str]:
    try:
        rna = getattr(getattr(bpy.ops, module), name).get_rna_type()
    except Exception:
        return None
    lines = [f"bpy.ops.{module}.{name}()  - {rna.description or rna.name}"]
    lines += [_prop_line(p) for p in rna.properties if p.identifier != "rna_type"]
    return "\n".join(lines)


def _describe_type(name: str) -> Optional[str]:
    cls = getattr(bpy.types, name, None)
    rna = getattr(cls, "bl_rna", None)
    if rna is None:
        return None
    base = f"({rna.base.identifier})" if rna.base else ""
    lines = [f"bpy.types.{name}{base}  - {rna.description}"]
    lines += [_prop_line(p) for p in rna.properties if p.identifier != "rna_type"][:70]
    funcs = [f"  {f.identifier}({', '.join(p.identifier for p in f.parameters if not p.is_output)})"
             f" - {f.description}"[:200] for f in rna.functions][:30]
    if funcs:
        lines += ["methods:"] + funcs
    return "\n".join(lines)


@tool(
    "api_lookup",
    """Look up the real bpy API of THIS Blender version before using something you are not sure
    exists. Query forms: 'ops.mesh.bevel' (exact operator with every parameter and default),
    'ops.mesh' (operators in a module), 'BevelModifier' (exact type with properties and methods),
    or free text such as 'solidify' (matching operators and types). Use it whenever a script
    fails with AttributeError or TypeError, instead of guessing again.""",
    {"query": {"type": "string", "description": "Operator path, type name, or search text."}},
    ["query"],
    label="Looked up the Blender API",
)
def api_lookup(query: str) -> str:
    q = query.strip()
    q = q[4:] if q.startswith("bpy.") else q
    if q.startswith("ops."):
        parts = q.split(".")
        if len(parts) >= 3:
            text = _describe_operator(parts[1], parts[2].rstrip("()"))
            if text:
                return text
        module = getattr(bpy.ops, parts[1], None) if len(parts) >= 2 else None
        names = sorted(dir(module)) if module is not None else []
        if names:
            return f"bpy.ops.{parts[1]}: {len(names)} operators\n" + ", ".join(names)
        return f"No operator module 'bpy.ops.{parts[1] if len(parts) > 1 else ''}'. Modules: {', '.join(sorted(dir(bpy.ops)))}"
    if q.startswith("types."):
        q = q[6:]
    exact = _describe_type(q)
    if exact:
        return exact

    needle = q.lower().replace(" ", "_")
    words = [w for w in re.split(r"[\s_.]+", q.lower()) if w]
    ops = []
    for module in dir(bpy.ops):
        for name in dir(getattr(bpy.ops, module)):
            full = f"{module}.{name}"
            if needle in full or all(w in full for w in words):
                ops.append(full)
    types = [t for t in dir(bpy.types) if needle.replace("_", "") in t.lower() or all(w in t.lower() for w in words)]
    out = []
    if ops:
        out.append(f"operators ({len(ops)}): " + ", ".join("ops." + o for o in ops[:60]))
        if len(ops) <= 3:
            out += [_describe_operator(*o.split(".")) or "" for o in ops]
    if types:
        out.append(f"types ({len(types)}): " + ", ".join(types[:60]))
        if len(types) == 1:
            out.append(_describe_type(types[0]) or "")
    if not out:
        return f"Nothing in bpy.ops or bpy.types matches '{query}'. Try a shorter keyword."
    out.append("Query an exact name (e.g. 'ops.mesh.bevel' or 'BevelModifier') for parameters.")
    return "\n".join(out)
