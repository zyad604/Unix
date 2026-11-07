---
name: asset-marking
domain: asset_library
when: The user wants objects, collections or materials to appear in the Asset Browser with tags and a thumbnail.
---

# Asset Marking
Finished datablocks marked as assets, with searchable metadata and a preview.

## Do
- Mark only finished work: scale applied, origin at the base or pivot, named for what it is. The asset name is the datablock name.
- Mark what the user will drag in. One object: mark the object. A multi-part asset: put the parts in one collection and mark the collection. A reusable surface: mark the material.
- `id.asset_mark()` is safe to call again; `id.asset_data` is `None` until then. `id.asset_clear()` undoes it.
- Fill `description` (one sentence with the real size in meters) and `author`. Add 3 to 6 lower-case tags: kind, material, style. `tags.new(name, skip_if_exists=True)` keeps re-runs clean.
- Call `id.asset_generate_preview()` after the look is final. It renders a 128 px thumbnail; the first one in a session takes several seconds. Do not loop it over hundreds of assets in one call.
- Catalogs are defined in a `blender_assets.cats.txt` file in the asset library folder. A script cannot write one here. Set `asset_data.catalog_id` only when the user supplies a catalog UUID; otherwise rely on tags.
- Marked assets show under Current File at once. Other files see them only after the user saves this file inside a registered asset library folder; say so, you cannot save it.

## Code
```python
spec = {"Stool": ("object", ["furniture", "seat", "wood"], "Three-legged oak stool, 0.45 m high."),
        "Oak_Varnished": ("material", ["wood", "oak", "glossy"], "Varnished oak, roughness 0.35.")}
marked = {}
for name, (kind, tags, text) in spec.items():
    block = (D.objects if kind == "object" else D.materials).get(name)
    if block is None:
        marked[name] = "missing"
        continue
    block.asset_mark()
    meta = block.asset_data
    meta.description, meta.author = text, "Unix"
    for tag in tags:
        meta.tags.new(tag, skip_if_exists=True)
    block.asset_generate_preview()
    marked[name] = {"tags": sorted(t.name for t in meta.tags),
                    "preview_px": list(block.preview.image_size) if block.preview else None}
result = marked
```

## Avoid
- Marking every part of an asset separately: the browser fills with legs and screws instead of one stool.
- Generating the preview before materials and smoothing are final: the thumbnail shows the grey blockout for good.
- Inventing a `catalog_id`: an unknown UUID shows the asset under "Unassigned".

Check: `run_python` returns each asset with its tags and a `preview_px` that is not null, and `[o.name for o in D.objects if o.asset_data]` lists exactly the intended objects.
