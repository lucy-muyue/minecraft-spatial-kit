# Synthetic mod resource example

This fictional `spatial_demo` pack is authored for Minecraft Spatial Kit. It contains no Minecraft or third-party assets. The scene contains one stateful block; the blockstate chooses a rotated model, and that model inherits its compact shape and crystal texture from a parent while overriding the metal texture.

Render it from the repository root:

```bash
mc-spatial render --scene ./examples/synthetic_mod_scene.json \
  --resource-pack ./examples/synthetic_mod_resource_pack \
  --out ./synthetic-mod-preview
```

Read `render_manifest.json` and check resource status and reasons. A resolved file only means that an input was found; it does not mean Minecraft's full appearance or behavior was reproduced.
