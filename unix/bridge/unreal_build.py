# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Runs inside the Unreal Editor (sent over remote execution by ``tools/unreal.py``): builds what
Blender exported into the level that is open. Not imported by the add-on.

Blender writes a folder: one FBX per distinct mesh (at its own origin, materials named
``UnixSlot0..n`` in slot order), PNG pictures, and ``manifest.json`` that lists the meshes,
pictures, materials and actors with their transforms already in Unreal's space. This script
imports the files, keeps one master material per blend mode that Unix owns and gives each
Blender material an instance of it, then spawns the actors in the open level. Every actor is
tagged with the asset's name, so a second send removes the first one's actors and puts the new
ones in their place instead of adding a copy.

Content goes under the mount point of the open level: a level in a plugin (a mod's
``/UGC123/Maps/Foo``) gets ``/UGC123/Unix/<asset>`` so the plugin packages it; anything else
``/Game/Unix/<asset>``. The report is written to the path the manifest names.
"""

import hashlib
import json
import os
import traceback

import unreal

MASTER_VERSION = "2"           # change when the master graph changes: masters with another version are rebuilt
BUILD_VERSION = "1"            # change when how pictures, meshes or instances are set up changes: all are made again
DIGEST = "UnixDigest"         # each asset remembers what it was made from: the same again is not imported again
TAG = "Unix"
NOT_MOUNTS = {"", "Temp", "Engine", "Script", "Memory"}      # an unsaved level lives in /Temp
FORWARD_SHADOWS = 4            # forward shading shadows at most this many movable lights over any one spot


def _mount(world):
    path = world.get_path_name() if world else ""
    mount = path.split("/")[1] if path.startswith("/") and path.count("/") > 1 else ""
    return ("Game" if mount in NOT_MOUNTS else mount), path.split(".")[0]


def _vector(values):
    return unreal.Vector(float(values[0]), float(values[1]), float(values[2]))


def _transform(item):
    rotation = unreal.Quat(*[float(v) for v in item["quat"]]).rotator()
    return unreal.Transform(_vector(item["location"]), rotation, _vector(item.get("scale", (1.0, 1.0, 1.0))))


def _color(values):
    return unreal.LinearColor(float(values[0]), float(values[1]), float(values[2]), float(values[3]) if len(values) > 3 else 1.0)


def _fresh(path, digest):
    """The asset at ``path`` when it was made from exactly this (the digest Blender sent), else None."""
    library = unreal.EditorAssetLibrary
    if not digest or not library.does_asset_exist(path):
        return None
    asset = library.load_asset(path)
    if asset is None or library.get_metadata_tag(asset, DIGEST) != f"{BUILD_VERSION}:{digest}":
        return None
    return asset


def _remember(asset, digest):
    if digest:
        unreal.EditorAssetLibrary.set_metadata_tag(asset, DIGEST, f"{BUILD_VERSION}:{digest}")


def _import(tasks):
    if tasks:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)


def _task(filename, path, name, options=None):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", filename)
    task.set_editor_property("destination_path", path)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", False)
    if options is not None:
        task.set_editor_property("options", options)
    return task


def _fbx_options():
    options = unreal.FbxImportUI()
    options.set_editor_property("import_mesh", True)
    options.set_editor_property("import_textures", False)
    options.set_editor_property("import_materials", False)
    options.set_editor_property("import_as_skeletal", False)
    options.set_editor_property("import_animations", False)
    options.set_editor_property("automated_import_should_detect_type", False)
    options.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_STATIC_MESH)
    data = options.get_editor_property("static_mesh_import_data")
    data.set_editor_property("combine_meshes", True)
    data.set_editor_property("auto_generate_collision", True)
    data.set_editor_property("generate_lightmap_u_vs", True)
    data.set_editor_property("remove_degenerates", True)
    data.set_editor_property("normal_import_method", unreal.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS)
    data.set_editor_property("convert_scene", True)
    data.set_editor_property("force_front_x_axis", False)
    data.set_editor_property("import_uniform_scale", 1.0)
    return options


# -- master materials -------------------------------------------------------------------

def _master(root, blend, defaults):
    """The master material for one blend mode, built (or rebuilt when its version is old):
    base colour = map x colour, opacity = map alpha x Opacity, roughness and metal from the R and G
    of a data map x their numbers, a normal map, light given off = map x colour. Every map has a
    neutral default, so an instance only sets what its Blender material has."""
    names = {"opaque": "M_Unix", "masked": "M_Unix_Masked", "translucent": "M_Unix_Translucent"}
    path = f"{root}/{names[blend]}"
    library = unreal.EditorAssetLibrary
    material = library.load_asset(path) if library.does_asset_exist(path) else None
    if material is not None and library.get_metadata_tag(material, "UnixMaster") == MASTER_VERSION:
        return material
    edit = unreal.MaterialEditingLibrary
    if material is None:
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(names[blend], root, unreal.Material,
                                                                          unreal.MaterialFactoryNew())
    else:
        edit.delete_all_material_expressions(material)
    material.set_editor_property("blend_mode", {"opaque": unreal.BlendMode.BLEND_OPAQUE,
                                                "masked": unreal.BlendMode.BLEND_MASKED,
                                                "translucent": unreal.BlendMode.BLEND_TRANSLUCENT}[blend])
    material.set_editor_property("two_sided", blend != "opaque")      # cut-out leaves and glass panes are seen from both sides
    if blend == "translucent":
        material.set_editor_property("translucency_lighting_mode",
                                     unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)

    def node(kind, x, y, **props):
        made = edit.create_material_expression(material, kind, x, y)
        for key, value in props.items():
            made.set_editor_property(key, value)
        return made

    def link(a, out, b, into):
        edit.connect_material_expressions(a, out, b, into)

    # UVs: Blender's Mapping node (tiling and shift), as scale in RG and offset in BA
    uv_params = node(unreal.MaterialExpressionVectorParameter, -1400, 0, parameter_name="UVTransform",
                     default_value=unreal.LinearColor(1.0, 1.0, 0.0, 0.0))
    coords = node(unreal.MaterialExpressionTextureCoordinate, -1400, -200)
    scale = node(unreal.MaterialExpressionAppendVector, -1200, -100)
    link(uv_params, "R", scale, "A")
    link(uv_params, "G", scale, "B")
    shift = node(unreal.MaterialExpressionAppendVector, -1200, 100)
    link(uv_params, "B", shift, "A")
    link(uv_params, "A", shift, "B")
    scaled = node(unreal.MaterialExpressionMultiply, -1050, -100)
    link(coords, "", scaled, "A")
    link(scale, "", scaled, "B")
    uv = node(unreal.MaterialExpressionAdd, -900, 0)
    link(scaled, "", uv, "A")
    link(shift, "", uv, "B")

    def sampler(name, y, texture, kind):
        made = node(unreal.MaterialExpressionTextureSampleParameter2D, -700, y, parameter_name=name, texture=texture,
                    sampler_type=kind)
        link(uv, "", made, "UVs")
        return made

    def scalar(name, y, value):
        return node(unreal.MaterialExpressionScalarParameter, -450, y, parameter_name=name, default_value=value)

    def times(a, out_a, b, out_b, y):
        made = node(unreal.MaterialExpressionMultiply, -250, y)
        link(a, out_a, made, "A")
        link(b, out_b, made, "B")
        return made

    colour_map = sampler("BaseColorMap", -600, defaults["color"], unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    colour = node(unreal.MaterialExpressionVectorParameter, -450, -700, parameter_name="BaseColor",
                  default_value=unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
    edit.connect_material_property(times(colour_map, "RGB", colour, "", -650), "", unreal.MaterialProperty.MP_BASE_COLOR)
    if blend != "opaque":
        opacity = times(colour_map, "A", scalar("Opacity", -500, 1.0), "", -500)
        edit.connect_material_property(opacity, "", unreal.MaterialProperty.MP_OPACITY_MASK if blend == "masked"
                                       else unreal.MaterialProperty.MP_OPACITY)
    data_map = sampler("MaskMap", -200, defaults["data"], unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
    edit.connect_material_property(times(data_map, "R", scalar("Roughness", -250, 0.5), "", -250), "",
                                   unreal.MaterialProperty.MP_ROUGHNESS)
    edit.connect_material_property(times(data_map, "G", scalar("Metallic", -100, 0.0), "", -100), "",
                                   unreal.MaterialProperty.MP_METALLIC)
    normal_map = sampler("NormalMap", 150, defaults["normal"], unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    edit.connect_material_property(normal_map, "RGB", unreal.MaterialProperty.MP_NORMAL)
    light_map = sampler("EmissiveMap", 450, defaults["color"], unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    light = node(unreal.MaterialExpressionVectorParameter, -450, 450, parameter_name="EmissiveColor",
                 default_value=unreal.LinearColor(0.0, 0.0, 0.0, 1.0))
    edit.connect_material_property(times(light_map, "RGB", light, "", 450), "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    edit.recompile_material(material)
    library.set_metadata_tag(material, "UnixMaster", MASTER_VERSION)
    library.save_loaded_asset(material, False)
    return material


def _defaults(root, folder, manifest):
    """Neutral pictures for the masters' maps: white colour, white data, a flat normal."""
    path = f"{root}/Defaults"
    library = unreal.EditorAssetLibrary
    wanted = {"color": "T_UnixWhite", "data": "T_UnixWhiteData", "normal": "T_UnixFlatNormal"}
    tasks = [_task(os.path.join(folder, manifest["defaults"][kind]), path, name)
             for kind, name in wanted.items() if not library.does_asset_exist(f"{path}/{name}")]
    _import(tasks)
    out = {}
    for kind, name in wanted.items():
        texture = library.load_asset(f"{path}/{name}")
        if tasks:
            _texture_settings(texture, kind)
            library.save_loaded_asset(texture, False)
        out[kind] = texture
    return out


def _texture_settings(texture, kind):
    if kind == "normal":
        texture.set_editor_property("srgb", False)
        texture.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
    elif kind == "data":
        texture.set_editor_property("srgb", False)
        texture.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_DEFAULT)
    else:
        texture.set_editor_property("srgb", True)


# -- the send ---------------------------------------------------------------------------

def build(manifest_path):
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    folder = os.path.dirname(manifest_path)
    asset = manifest["asset"]
    notes = []
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    mount, level = _mount(world)
    root = f"/{mount}/Unix"
    content = f"{root}/{asset}"
    library = unreal.EditorAssetLibrary

    defaults = _defaults(root, folder, manifest)
    masters = {}

    # pictures, then meshes: one import call each, for what is not already there as it was sent
    textures, kept = {}, {"textures": 0, "meshes": 0, "materials": 0}
    for entry in manifest["textures"]:
        texture = _fresh(f"{content}/Textures/{entry['name']}", entry.get("digest"))
        if texture is not None:
            textures[entry["key"]] = texture
    fresh = [t for t in manifest["textures"] if t["key"] not in textures]
    _import([_task(os.path.join(folder, t["file"]), f"{content}/Textures", t["name"]) for t in fresh])
    for entry in fresh:
        texture = library.load_asset(f"{content}/Textures/{entry['name']}")
        if texture is None:
            notes.append(f"picture {entry['name']} did not import")
            continue
        _texture_settings(texture, entry["kind"])
        if entry.get("flip_green"):
            texture.set_editor_property("flip_green_channel", True)
        _remember(texture, entry.get("digest"))
        textures[entry["key"]] = texture
    kept["textures"] = len(manifest["textures"]) - len(fresh)
    meshes = {}
    for entry in manifest["meshes"]:
        mesh = _fresh(f"{content}/Meshes/{entry['name']}", entry.get("digest"))
        if mesh is not None:
            meshes[entry["key"]] = mesh
    fresh = [m for m in manifest["meshes"] if m["key"] not in meshes]
    options = _fbx_options()
    _import([_task(os.path.join(folder, m["file"]), f"{content}/Meshes", m["name"], options) for m in fresh])
    for entry in fresh:
        mesh = library.load_asset(f"{content}/Meshes/{entry['name']}")
        if mesh is None:
            notes.append(f"mesh {entry['name']} did not import")
            continue
        _remember(mesh, entry.get("digest"))
        meshes[entry["key"]] = mesh
    kept["meshes"] = len(manifest["meshes"]) - len(fresh)

    # one instance of a master per Blender material
    edit = unreal.MaterialEditingLibrary
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    materials = {}
    for entry in manifest["materials"]:
        blend = entry["blend"]
        if blend not in masters:
            masters[blend] = _master(root, blend, defaults)
        path = f"{content}/Materials/{entry['name']}"
        made_from = hashlib.sha1(json.dumps([MASTER_VERSION, entry["blend"], entry["vectors"], entry["scalars"],
                                             {name: textures[key].get_path_name() for name, key in entry["textures"].items()
                                              if key in textures}], sort_keys=True).encode()).hexdigest()
        instance = _fresh(path, made_from)
        if instance is not None and instance.get_editor_property("parent") == masters[blend]:
            materials[entry["key"]] = instance
            kept["materials"] += 1
            continue
        instance = library.load_asset(path) if library.does_asset_exist(path) else None
        if instance is None:
            instance = tools.create_asset(entry["name"], f"{content}/Materials", unreal.MaterialInstanceConstant,
                                          unreal.MaterialInstanceConstantFactoryNew())
        edit.set_material_instance_parent(instance, masters[blend])
        edit.clear_all_material_instance_parameters(instance)
        for name, value in entry["vectors"].items():
            edit.set_material_instance_vector_parameter_value(instance, name, _color(value))
        for name, value in entry["scalars"].items():
            edit.set_material_instance_scalar_parameter_value(instance, name, float(value))
        for name, key in entry["textures"].items():
            if key in textures:
                edit.set_material_instance_texture_parameter_value(instance, name, textures[key])
        edit.update_material_instance(instance)
        _remember(instance, made_from)
        materials[entry["key"]] = instance

    # the level: what an earlier send of this asset put there goes, the new actors take its place
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    mark = unreal.Name(f"UnixAsset:{asset}")
    old = [a for a in actors.get_all_level_actors() if mark in list(a.tags)]
    for actor in old:
        actors.destroy_actor(actor)
    folder_path = f"Unix/{asset}"
    counts = {"meshes": 0, "lights": 0, "cameras": 0}
    lamps = []

    def place(cls, item):
        actor = actors.spawn_actor_from_class(cls, _vector(item["location"]), unreal.Rotator(0.0, 0.0, 0.0))
        actor.set_actor_transform(_transform(item), False, True)
        actor.set_actor_label(item["name"])
        actor.set_folder_path(folder_path)
        actor.set_editor_property("tags", [unreal.Name(TAG), mark])
        return actor

    for item in manifest["actors"]:
        kind = item["kind"]
        if kind == "mesh":
            mesh = meshes.get(item["mesh"])
            if mesh is None:
                continue
            actor = place(unreal.StaticMeshActor, item)
            component = actor.static_mesh_component
            component.set_static_mesh(mesh)
            component.set_editor_property("cast_shadow_as_two_sided", True)     # as in Blender, a back face blocks light too
            component.set_cast_shadow(bool(item.get("shadows", True)))
            for slot, key in enumerate(item["materials"]):
                if key in materials:
                    component.set_material_by_name(unreal.Name(f"UnixSlot{slot}"), materials[key])
            counts["meshes"] += 1
        elif kind == "light":
            cls = {"point": unreal.PointLight, "spot": unreal.SpotLight, "sun": unreal.DirectionalLight,
                   "area": unreal.RectLight}[item["type"]]
            actor = place(cls, item)
            light = actor.get_component_by_class(unreal.LightComponent)
            light.set_mobility(unreal.ComponentMobility.MOVABLE)      # lit at once, no lighting build needed
            if item["type"] != "sun":
                light.set_editor_property("intensity_units", unreal.LightUnits.CANDELAS)
                light.set_attenuation_radius(float(item["radius"]))
            light.set_intensity(float(item["intensity"]))
            light.set_light_color(_color(item["color"]), True)        # linear in, as Blender keeps it
            light.set_cast_shadows(bool(item.get("shadows", True)))
            if item["type"] != "sun" and item.get("shadows", True):
                lamps.append((float(item["intensity"]), _vector(item["location"]), float(item["radius"]), light))
            if item["type"] in ("point", "spot"):
                light.set_source_radius(float(item["source_radius"]))
            if item["type"] == "spot":
                light.set_outer_cone_angle(float(item["outer"]))
                light.set_inner_cone_angle(float(item["inner"]))
            if item["type"] == "area":
                light.set_source_width(float(item["width"]))
                light.set_source_height(float(item["height"]))
            if item["type"] == "sun":
                light.set_editor_property("light_source_angle", float(item["angle"]))
            counts["lights"] += 1
        elif kind == "camera":
            actor = place(unreal.CameraActor, item)
            camera = actor.camera_component
            camera.set_constraint_aspect_ratio(False)
            camera.set_editor_property("aspect_ratio", float(item["aspect"]))
            if item.get("ortho"):
                camera.set_projection_mode(unreal.CameraProjectionMode.ORTHOGRAPHIC)
                camera.set_ortho_width(float(item["ortho"]))
            else:
                camera.set_field_of_view(float(item["fov"]))
            counts["cameras"] += 1

    sky = manifest.get("sky")
    if sky:
        _sky_light(sky, folder, content, place, actors, mark, notes)

    shadowless = _budget_shadows(lamps)
    if shadowless:
        notes.append(f"The project uses forward shading, which shadows at most {FORWARD_SHADOWS} lights that overlap: "
                     f"the {shadowless} weakest of the overlapping lamps light without shadows.")

    library.save_directory(content, True, True)
    return {"ok": True, "level": level, "content": content, "masters": sorted(masters), "replaced": len(old),
            "imported": {"meshes": len(meshes) - kept["meshes"], "textures": len(textures) - kept["textures"],
                         "materials": len(materials) - kept["materials"]}, "unchanged": kept,
            "spawned": counts, "notes": notes}


def _sky_light(sky, folder, content, place, actors, mark, notes):
    """Blender's world as a Movable sky light: it lights the shade at once, without a lighting build.
    A level uses one sky light, so a level that has its own keeps it."""
    own = [a for a in actors.get_all_level_actors() if isinstance(a, unreal.SkyLight) and mark not in list(a.tags)]
    if own:
        mobility = str(own[0].light_component.mobility).split(".")[-1].split(":")[0].strip("<>").lower()
        notes.append(f"The level has its own sky light ({own[0].get_actor_label()}, {mobility}), and a level uses one, so "
                     f"Blender's world was not sent as a second."
                     + (" Being Static it lights only after Build > Build Lighting; until then the shade is darker than "
                        "in Blender." if mobility == "static" else ""))
        return
    path = f"{content}/Sky/{sky['name']}"
    cube = _fresh(path, sky["digest"])
    if cube is None:
        task = _task(os.path.join(folder, sky["file"]), f"{content}/Sky", sky["name"])
        task.set_editor_property("factory", unreal.TextureFactory())    # the classic importer makes a 2:1 HDR a cube map
        _import([task])
        cube = unreal.EditorAssetLibrary.load_asset(path)
        if not isinstance(cube, unreal.TextureCube):
            notes.append("Blender's world could not be made into a sky picture in Unreal, so it was not sent.")
            return
        _remember(cube, sky["digest"])
    actor = place(unreal.SkyLight, {"name": "Unix_World", "location": [0.0, 0.0, 0.0], "quat": [0.0, 0.0, 0.0, 1.0]})
    light = actor.light_component
    light.set_mobility(unreal.ComponentMobility.MOVABLE)
    light.set_editor_property("source_type", unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
    light.set_editor_property("cubemap", cube)
    light.set_editor_property("lower_hemisphere_is_black", False)      # the picture's lower half is Blender's ground light
    light.set_intensity(1.0)                                            # the picture is already on the lights' scale
    light.recapture_sky()


def _budget_shadows(lamps):
    """Forward shading cannot shadow more than FORWARD_SHADOWS movable lights over one spot; past
    that it drops shadows on its own and marks the lights in red. Choose instead: the strongest keep
    them, a lamp whose reach overlaps that many shadowed ones already lights without. Returns how
    many lost them."""
    if not unreal.SystemLibrary.get_console_variable_int_value("r.ForwardShading"):
        return 0
    kept, dropped = [], 0
    for intensity, at, radius, light in sorted(lamps, key=lambda lamp: -lamp[0]):
        overlapping = sum(1 for other_at, other_radius in kept if (at - other_at).length() < radius + other_radius)
        if overlapping >= FORWARD_SHADOWS:
            light.set_cast_shadows(False)
            dropped += 1
        else:
            kept.append((at, radius))
    return dropped


def main(manifest_path):
    """Build, and write the report next to the manifest whatever happens: Blender reads it there."""
    try:
        report = build(manifest_path)
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-3000:]}
    with open(os.path.join(os.path.dirname(manifest_path), "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    return report


if __name__ == "unix_build":
    main(MANIFEST)  # noqa: F821  (given by the command that runs this file)
