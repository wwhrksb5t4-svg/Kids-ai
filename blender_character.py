"""Render a short, original looping 3D character animation in background Blender."""
import math
import os
import sys

import bpy


output = sys.argv[sys.argv.index("--") + 1]
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 16
scene.cycles.use_denoising = False
scene.render.resolution_x = 512
scene.render.resolution_y = 288
scene.render.resolution_percentage = 100
scene.render.fps = 12
scene.frame_start = 1
scene.frame_end = 24
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = os.path.join(output, "frame-")
scene.world.color = (0.32, 0.32, 0.32)


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


def ball(name, location, scale, mat, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    obj.data.materials.append(mat)
    if parent:
        obj.parent = parent
    return obj


ball("body", (0, 0, 1.15), (0.43, 0.25, 0.55), shirt)
ball("head", (0, 0, 1.98), (0.45, 0.35, 0.48), skin)
ball("hair", (0, 0.02, 2.28), (0.46, 0.36, 0.23), hair)
ball("mouth", (0, -0.346, 1.82), (0.12, 0.025, 0.035), black)
for side in (-1, 1):
    ball("eye", (side * 0.17, -0.317, 2.05), (0.10, 0.065, 0.12), white)
    ball("pupil", (side * 0.17, -0.378, 2.05), (0.046, 0.03, 0.055), black)
    ball("leg", (side * 0.22, 0, 0.53), (0.16, 0.18, 0.42), pants)
    ball("shoe", (side * 0.22, -0.08, 0.14), (0.19, 0.29, 0.14), white)
    pivot = bpy.data.objects.new(f"shoulder-{side}", None)
    scene.collection.objects.link(pivot)
    pivot.location = (side * 0.40, 0, 1.55)
    arm = ball("arm", (0, 0, 0), (0.13, 0.13, 0.35), shirt, pivot)
    arm.location = (side * 0.12, 0, -0.23)
    hand = ball("hand", (0, 0, 0), (0.14, 0.14, 0.14), skin, pivot)
    hand.location = (side * 0.20, 0, -0.49)
    for frame, angle in ((1, -0.45), (7, 0.50), (13, -0.45), (19, 0.50), (25, -0.45)):
        pivot.rotation_euler[1] = angle * side
        pivot.keyframe_insert(data_path="rotation_euler", frame=frame)

bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, 0))
bpy.context.object.data.materials.append(ground)
bpy.ops.object.light_add(type="AREA", location=(0, -4, 6))
bpy.context.object.data.energy = 800
bpy.context.object.data.shape = "DISK"
bpy.context.object.data.size = 5
bpy.ops.object.camera_add(location=(0, -7, 1.35), rotation=(math.radians(90), 0, 0))
camera = bpy.context.object
camera.data.type = "ORTHO"
camera.data.ortho_scale = 5.4
scene.camera = camera
bpy.ops.render.render(animation=True)
