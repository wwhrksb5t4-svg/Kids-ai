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
scene.cycles.samples = 32
scene.cycles.use_denoising = False
scene.render.resolution_x = 768
scene.render.resolution_y = 432
scene.render.resolution_percentage = 100
scene.render.fps = 12
scene.frame_start = 1
scene.frame_end = 24
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = os.path.join(output, "frame-")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.38, 0.67, 0.95, 1)
scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.65
scene.view_settings.view_transform = "Standard"
scene.view_settings.look = "Medium High Contrast"


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Roughness"].default_value = 0.55
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
cream = material("cream", (1.0, 0.91, 0.73))
coral = material("coral", (0.90, 0.30, 0.24))
darkblue = material("dark blue", (0.05, 0.15, 0.30))
lightpink = material("cheeks", (0.80, 0.32, 0.31))
hill = material("hills", (0.26, 0.59, 0.29))


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


def cube(name, location, scale, mat, bevel=0.08):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new("rounded edges", "BEVEL")
        mod.width = bevel
        mod.segments = 3
        obj.modifiers.new("weighted normals", "WEIGHTED_NORMAL")
    return obj


def smile(parent):
    curve = bpy.data.curves.new("smile curve", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 12
    curve.bevel_depth = 0.018
    curve.bevel_resolution = 3
    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(2)
    for point, position in zip(spline.bezier_points, ((-0.14, -0.342, 1.84), (0, -0.36, 1.77), (0.14, -0.342, 1.84))):
        point.co = position
        point.handle_left_type = "AUTO"
        point.handle_right_type = "AUTO"
    obj = bpy.data.objects.new("smile", curve)
    scene.collection.objects.link(obj)
    obj.parent = parent
    obj.data.materials.append(hair)


root = bpy.data.objects.new("character", None)
scene.collection.objects.link(root)
ball("body", (0, 0, 1.13), (0.44, 0.29, 0.51), shirt, root)
ball("collar", (0, -0.13, 1.49), (0.25, 0.19, 0.10), cream, root)
ball("shirt badge", (0, -0.284, 1.18), (0.12, 0.025, 0.12), yellow, root)
ball("badge dot", (0, -0.307, 1.18), (0.045, 0.012, 0.045), cream, root)
ball("neck", (0, 0, 1.53), (0.16, 0.14, 0.18), skin, root)
ball("head", (0, 0, 1.98), (0.48, 0.36, 0.49), skin, root)
ball("hair cap", (0, 0.04, 2.28), (0.49, 0.36, 0.24), hair, root)
for x in (-0.23, -0.08, 0.08, 0.23):
    ball("hair fringe", (x, -0.27, 2.27), (0.13, 0.11, 0.13), hair, root)
ball("nose", (0, -0.358, 1.94), (0.068, 0.075, 0.06), skin, root)
smile(root)
for side in (-1, 1):
    ball("ear", (side * 0.47, 0, 1.96), (0.12, 0.10, 0.16), skin, root)
    ball("cheek", (side * 0.30, -0.292, 1.86), (0.10, 0.025, 0.055), lightpink, root)
    ball("eye", (side * 0.17, -0.325, 2.06), (0.105, 0.065, 0.13), white, root)
    ball("iris", (side * 0.17, -0.382, 2.05), (0.058, 0.03, 0.069), darkblue, root)
    ball("pupil", (side * 0.17, -0.408, 2.05), (0.037, 0.02, 0.048), black, root)
    ball("eye shine", (side * 0.17 - 0.018, -0.428, 2.075), (0.017, 0.01, 0.02), white, root)
    leg = bpy.data.objects.new(f"hip-{side}", None)
    scene.collection.objects.link(leg)
    leg.parent = root
    leg.location = (side * 0.22, 0, 0.84)
    ball("leg", (0, 0, -0.22), (0.16, 0.18, 0.33), pants, leg)
    ball("shoe", (0, -0.10, -0.69), (0.19, 0.29, 0.14), coral, leg)
    ball("shoe sole", (0, -0.10, -0.78), (0.19, 0.29, 0.065), cream, leg)
    for frame, angle in ((1, -0.09), (7, 0.10), (13, -0.09), (19, 0.10), (25, -0.09)):
        leg.rotation_euler[0] = angle * side
        leg.keyframe_insert(data_path="rotation_euler", frame=frame)
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
for x in (-4.2, 0, 4.2):
    ball("distant hill", (x, 4.8, 0.0), (3, 0.7, 1.1), hill)
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
for x, z in ((-2.1, 2.85), (1.9, 2.95)):
    for dx in (-0.35, 0, 0.35):
        ball("cloud", (x + dx, 3, z), (0.5, 0.25, 0.24), white)
if variant == 0:
    for x in (-2.2, 2.2):
        for n in range(4):
            cube("fence post", (x + n * 0.35, 1.4, 0.40), (0.12, 0.13, 0.8), cream, 0.04)
        cube("fence rail", (x + 0.52, 1.4, 0.42), (1.2, 0.1, 0.12), cream, 0.03)
    ball("toy ball", (-1.25, -0.35, 0.19), (0.22, 0.22, 0.22), coral)
elif variant == 1:
    cube("playhouse", (2.1, 2.0, 0.65), (1.2, 0.7, 1.3), cream)
    bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=1, depth=0.65, location=(2.1, 2.0, 1.62), rotation=(0, 0, math.pi / 4))
    roof = bpy.context.object
    roof.name = "roof"
    roof.scale = (1.05, 0.72, 1)
    roof.data.materials.append(coral)
    ball("door", (2.1, 1.62, 0.38), (0.24, 0.06, 0.38), blue)
    cube("window", (2.48, 1.61, 0.87), (0.30, 0.06, 0.30), blue, 0.03)
else:
    for x, z, color in ((-2.15, 2.25, pink), (-1.65, 2.70, yellow), (2.20, 2.35, blue)):
        ball("balloon", (x, 1.0, z), (0.24, 0.18, 0.32), color)
        cube("balloon string", (x, 1.0, z - 0.55), (0.018, 0.018, 0.52), white, 0.005)
    ball("toy ball", (1.55, -0.25, 0.24), (0.25, 0.25, 0.25), yellow)
bpy.ops.object.light_add(type="AREA", location=(0, -4, 6))
bpy.context.object.data.energy = 800
bpy.context.object.data.shape = "DISK"
bpy.context.object.data.size = 5
bpy.ops.object.camera_add(location=((0, 0.9, -0.8)[variant], -7, 1.45), rotation=(math.radians(90), 0, 0))
camera = bpy.context.object
camera.data.type = "ORTHO"
camera.data.ortho_scale = (5.8, 5.4, 5.4)[variant]
scene.camera = camera
bpy.ops.render.render(animation=True)
