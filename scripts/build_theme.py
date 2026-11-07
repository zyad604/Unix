# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Builds unix/theme/Unix.xml from Blender's default dark theme.
Run: blender -b --factory-startup --python scripts/build_theme.py

Blue UI accents become purple, neutral dark greys become a slightly deeper charcoal with a
faint violet cast. Axis colours, selection orange and semantic colours are left alone.
"""
import colorsys, os
import bpy, rna_xml

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unix", "theme", "Unix.xml")
SKIP = ("user_interface.axis_", "bone_color_sets", "collection_color", "strip_color")
stats = {"accent": 0, "grey": 0}

def recolor(color):
    r, g, b = color[0], color[1], color[2]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    deg = h * 360.0
    if s >= 0.18 and 185.0 <= deg <= 255.0:            # Blender's blue accents
        stats["accent"] += 1
        nh = (268.0 + (deg - 215.0) * 0.35) / 360.0
        return colorsys.hsv_to_rgb(nh, min(1.0, s * 1.02), v) + tuple(color[3:])
    if s < 0.10 and v < 0.42:                          # dark neutral surfaces
        stats["grey"] += 1
        return colorsys.hsv_to_rgb(262.0 / 360.0, 0.07, v * 0.80) + tuple(color[3:])
    return None

def walk(obj, path="", depth=0):
    if depth > 7: return
    for prop in obj.bl_rna.properties:
        ident = prop.identifier
        if ident == "rna_type": continue
        sub = f"{path}.{ident}" if path else ident
        if sub.startswith(SKIP): continue
        try: value = getattr(obj, ident)
        except Exception: continue
        if prop.type == 'FLOAT' and prop.subtype in {'COLOR', 'COLOR_GAMMA'} and prop.array_length in {3, 4}:
            new = recolor(value)
            if new is not None and not prop.is_readonly:
                setattr(obj, ident, new)
        elif prop.type == 'POINTER' and value is not None and hasattr(value, "bl_rna"):
            walk(value, sub, depth + 1)
        elif prop.type == 'COLLECTION':
            for i, item in enumerate(value): walk(item, f"{sub}[{i}]", depth + 1)

theme = bpy.context.preferences.themes[0]
walk(theme)
grad = theme.view_3d.space.gradients
grad.background_type = 'SINGLE_COLOR'
grad.high_gradient = (0.066, 0.064, 0.074)
theme.name = "Unix"
from bl_ui.space_userpref import USERPREF_MT_interface_theme_presets as menu
rna_xml.xml_file_write(bpy.context, OUT, menu.preset_xml_map)
print("THEME", stats, os.path.getsize(OUT), OUT)
