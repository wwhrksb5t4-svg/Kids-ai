"""Render a short, original looping 3D character animation in background Blender."""
import math
import os
import sys

import bpy


args = sys.argv[sys.argv.index("--") + 1:]
output = args[0]
variant = int(args[1]) if len(args) > 1 else 0
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 24
scene.cycles.use_denoising = False
scene.render.resolution_x = 640
scene.render.resolution_y = 360
scene.render.resolution_percentage = 100
scene.render.fps = 12
scene.frame_start = 1
scene.frame_end = 24
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = os.path.join(output, "frame-")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.45, 0.72, 0.95, 1)
scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.8


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    return mat


skin = material("skin", (0.69, 0.38, 0.22))
hair = material("hair", (0.10, 0.055, 0.035))
shirt = material("shirt", (0.03, 0.42, 0.56))
pants = material("pants", (0.13, 0.16, 0.32))
white = material("white", (0.96, 0.96, 0.90))
black = material("black", (0.035, 0.025, 0.03))
ground = material("ground", (0.42, 0.70, 0.31))
leaf = material("leaves", (0.13, 0.53, 0.23))
wood = material("wood", (0.43, 0.22, 0.09))
pink = material("pink", (0.95, 0.30, 0.45))
yellow = material("yellow", (1.0, 0.75, 0.12))
blue = material("blue", (0.10, 0.55, 0.95))


def ball(name, location, scale, mat, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    obj.data.materials.append(mat)
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    if parent:
        obj.parent = parent
    return obj


root = bpy.data.objects.new("character", None)
scene.collection.objects.link(root)
ball("body", (0, 0, 1.15), (0.43, 0.25, 0.55), shirt, root)
ball("shirt badge", (0, -0.247, 1.2), (0.12, 0.025, 0.12), yellow, root)
ball("head", (0, 0, 1.98), (0.45, 0.35, 0.48), skin, root)
ball("hair", (0, 0.02, 2.28), (0.46, 0.36, 0.23), hair, root)
ball("nose", (0, -0.35, 1.93), (0.065, 0.07, 0.06), skin, root)
ball("smile", (0, -0.337, 1.81), (0.14, 0.035, 0.045), black, root)
for side in (-1, 1):
    ball("eye", (side * 0.17, -0.317, 2.05), (0.10, 0.065, 0.12), white, root)
    ball("pupil", (side * 0.17, -0.378, 2.05), (0.046, 0.03, 0.055), black, root)
    ball("leg", (side * 0.22, 0, 0.53), (0.16, 0.18, 0.42), pants, root)
    ball("shoe", (side * 0.22, -0.08, 0.14), (0.19, 0.29, 0.14), white, root)
    pivot = bpy.data.objects.new(f"shoulder-{side}", None)
    scene.collection.objects.link(pivot)
    pivot.parent = root
    pivot.location = (side * 0.40, 0, 1.55)
    arm = ball("arm", (0, 0, 0), (0.13, 0.13, 0.35), shirt, pivot)
    arm.location = (side * 0.12, 0, -0.23)
    hand = ball("hand", (0, 0, 0), (0.14, 0.14, 0.14), skin, pivot)
    hand.location = (side * 0.20, 0, -0.49)
    for frame, angle in ((1, -0.35), (7, 0.65), (13, -0.35), (19, 0.65), (25, -0.35)):
        pivot.rotation_euler[1] = angle * side
        pivot.keyframe_insert(data_path="rotation_euler", frame=frame)

for frame, tilt, height in ((1, -0.07, 0), (7, 0.06, 0.10), (13, 0.07, 0),
                            (19, -0.06, 0.10), (25, -0.07, 0)):
    root.rotation_euler[1] = tilt
    root.location.z = height
    root.keyframe_insert(data_path="rotation_euler", frame=frame)
    root.keyframe_insert(data_path="location", frame=frame)

ball("lawn", (0, 1, -0.42), (10, 7, 0.45), ground)
for x in (-3.0, 2.8):
    ball("tree trunk", (x, 1.8, 0.9), (0.19, 0.19, 0.95), wood)
    for dx, dz in ((0, 2.2), (-0.5, 1.9), (0.5, 1.9)):
        ball("tree crown", (x + dx, 1.8, dz), (0.65, 0.48, 0.65), leaf)
for x, y, color in ((-2.0, -0.1, pink), (1.9, -0.2, yellow), (2.6, 0.3, pink), (-2.8, 0.1, blue)):
    ball("flower stem", (x, y, 0.16), (0.04, 0.04, 0.16), leaf)
    for angle in range(5):
        a = angle * 2 * math.pi / 5
        ball("petal", (x + 0.13 * math.cos(a), y - 0.03, 0.35 + 0.13 * math.sin(a)),
             (0.10, 0.05, 0.10), color)
    ball("flower center", (x, y - 0.08, 0.35), (0.07, 0.04, 0.07), yellow)
for x, z in ((-2.1, 3.2), (1.9, 3.35)):
    for dx in (-0.35, 0, 0.35):
        ball("cloud", (x + dx, 3, z), (0.5, 0.25, 0.24), white)
bpy.ops.object.light_add(type="AREA", location=(0, -4, 6))
bpy.context.object.data.energy = 800
bpy.context.object.data.shape = "DISK"
bpy.context.object.data.size = 5
bpy.ops.object.camera_add(location=((0, 0.9, -0.8)[variant], -7, 1.45), rotation=(math.radians(90), 0, 0))
camera = bpy.context.object
camera.data.type = "ORTHO"
camera.data.ortho_scale = (5.8, 5.0, 5.0)[variant]
scene.camera = camera
bpy.ops.render.render(animation=True)
