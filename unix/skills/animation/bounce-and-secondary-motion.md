---
name: bounce-and-secondary-motion
domain: animation
when: Something falls, bounces, lands, jumps or needs weight - or a part should lag and settle after the main motion.
---

# Bounce and secondary motion
Motion that reads as having weight: timing from gravity, spacing from easing, overlap on the loose parts.

## Do
- Timing from physics: fall time = sqrt(2 x height / 9.81) s. At 24 fps a 1 m drop is 11 frames, 2 m is 15.
- Each bounce reaches k x the last height and takes sqrt(k) x the last time. k: rubber ball 0.6-0.75, basketball 0.55, wooden crate 0.1-0.2. Stop below 2 cm.
- Spacing: key Z only at apexes and contacts. Apex key `QUAD` + `EASE_IN` (speeds up falling), contact key `QUAD` + `EASE_OUT` (slows rising). That is the exact parabola.
- Contact height is half the object's height above the floor, not 0. Contacts land on whole frames.
- Horizontal travel: one `LINEAR` curve from first to last frame. Do not ease it per bounce.
- Squash and stretch, soft objects only: on the contact frame scale Z 0.7-0.85 and X, Y by 1/sqrt(Z) to keep volume; stretch Z 1.1-1.2 one frame either side; 1.0 at the apex. Lower Z by half height x (1 - scale Z) so the base stays on the floor.
- Secondary parts (antenna, lid, tail): repeat the parent's key poses 2-4 frames later, overshoot 10-20 %, settle in 6-12 frames.

## Code
```python
ball, drop, k = D.objects["Cube"], 1.5, 0.6             # object, drop height in m, rebound ratio
r = ball.dimensions.z / 2                               # centre height when resting on the floor
fps = C.scene.render.fps / C.scene.render.fps_base

def fcurves(idb):                                       # slotted actions (4.4+), legacy fallback
    ad = idb.animation_data
    act = ad.action
    if not hasattr(act, "layers"):
        return list(act.fcurves)
    bags = [s.channelbag(ad.action_slot) for l in act.layers for s in l.strips]
    return [fc for bag in bags if bag for fc in bag.fcurves]

frame, top, keys = 1, r + drop, []
while top - r > 0.02:
    fall = max(1, round(math.sqrt(2 * (top - r) / 9.81) * fps))
    keys += [(frame, top, 'EASE_IN'), (frame + fall, r, 'EASE_OUT')]
    top = r + (top - r) * k
    frame += fall + max(1, round(fall * math.sqrt(k)))
for f, z, ease in keys:
    ball.location.z = z
    ball.keyframe_insert("location", index=2, frame=f)
zc = next(fc for fc in fcurves(ball) if fc.data_path == "location" and fc.array_index == 2)
for kp, (f, z, ease) in zip(zc.keyframe_points, keys):
    kp.interpolation, kp.easing = 'QUAD', ease
zc.update()
C.scene.frame_set(C.scene.frame_current)                # re-evaluate the pose for the current frame
result = {"bounces": len(keys) // 2, "contacts": [f for f, z, e in keys if z == r], "rest_z": r}
```

## Avoid
- Bezier keys at contact: the curve rounds off and the object floats down to a soft landing instead of hitting.
- Equal time for every bounce: it reads as a mechanical yo-yo, not gravity.
- Squashing a rigid object, or squashing about the centre: the base lifts off the floor.

Check: `look` (front view) on a contact frame shows the object touching the floor, no gap and no sinking; in `run_python` the Z curve halfway through the first fall equals `top - 0.25 x drop`.
