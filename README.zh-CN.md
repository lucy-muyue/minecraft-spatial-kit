# Minecraft Spatial Kit

**Minecraft Spatial Kit** 从本地 Minecraft Java 世界文件中索引有限区域、查询精确方块、渲染空间预览，并导出供 [mc-builder](https://github.com/lucy-muyue/mc-builder) 使用的蓝图 JSON。它是离线文件工具，不连接游戏服务器，也不放置方块。

[English README](README.md) · [Agent skill](skills/minecraft-spatial/SKILL.md) · [索引指南](docs/index.md) · [渲染指南](docs/rendering.md) · [渲染取舍](docs/rendering-decisions.md) · [验证记录](docs/validation.md) · [Agent 设计与施工交接](docs/workflow.md)

Agent 可直接阅读 [`skills/minecraft-spatial/`](skills/minecraft-spatial/)，或将整个目录复制到自己的 skill 目录。服务端安装和配置属于 [mc-builder](https://github.com/lucy-muyue/mc-builder)，见其 [README](https://github.com/lucy-muyue/mc-builder/blob/main/README.md) 与 [AGENT_BUILDING_GUIDE](https://github.com/lucy-muyue/mc-builder/blob/main/AGENT_BUILDING_GUIDE.md)。Minecraft Spatial Kit 只导出本地文件，不安装服务端工作者或修改服务端配置。

## 预览合成亭子

![合成亭子的空间预览](docs/assets/demo.png)

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mc-spatial demo --out ./pavilion-preview
```

示例会生成 `scene.json`、`mc-builder-blueprint.json`、`model.glb`、四张 PNG 视图、三张剖面图和 `render_manifest.json`。亭子是虚构设计，包含入口、底 slab 屋顶与直线楼梯；示例坐标均为编造值。

## 勘察世界区域

Minecraft 方块坐标约定为 **X 向东、Y 向上、Z 向南**。每个坐标还属于明确的维度。范围端点均包含在导出区域中。

1. **索引当前要查看的区域。** 开始新的勘察且关注范围改变时，为新范围建索引或刷新。世界文件未变时可以复用数据库；索引会使用源文件哈希识别输入是否变化。需要强制重读时加 `--force`。详见[索引指南](docs/index.md)。

   ```bash
   mc-spatial index --world /path/to/world --db ./world-index.sqlite \
     --dimension minecraft:overworld --min -32 48 -32 --max 31 96 31
   ```

   自定义高度的维度用 `--dimension-bounds MIN_Y MAX_Y` 指定包含端点的实际垂直范围。以上数值仅示范命令参数，不代表真实地点。

2. **先导出较大范围。**

   ```bash
   mc-spatial export --db ./world-index.sqlite --dimension minecraft:overworld \
     --min -32 48 -32 --max 31 96 31 --out ./scene.json
   mc-spatial render --scene ./scene.json --out ./scene-preview
   ```

   打开 `views/iso.png` 理解高度和形状，打开 `views/top.png` 看平面占地；再查看 `sections/x_mid.png`、`sections/y_mid.png`、`sections/z_mid.png` 的中心剖面。`render_manifest.json` 会列出覆盖情况和几何简化项。

3. **根据疑问缩小范围。** 对入口、支撑或路线导出更小的范围，或者调整 Y 范围查看另一层，再重新渲染。对关键点执行 query，检查精确方块和状态：

   ```bash
   mc-spatial export --db ./world-index.sqlite --dimension minecraft:overworld \
     --min -8 48 -8 --max 8 72 8 --out ./scene-focus.json
   mc-spatial render --scene ./scene-focus.json --out ./scene-focus
   mc-spatial query --db ./world-index.sqlite --dimension minecraft:overworld --pos 0 64 0
   ```

   建立索引时也可用 `--center X Y Z --radius RX RY RZ` 代替 `--min/--max`。超出索引范围或位于缺失区块的位置是未知。源文件变化要等下一次 `index` 刷新时才会发现；query 只读取上次索引结果，不会监控世界。**未知不等于空气。**

输出文件用途如下：

| 文件 | 用途 |
|---|---|
| `scene.json` | 可移植的有限空间数据，含维度、包含端点的范围、palette、方块坐标、未知区块和来源。 |
| `model.glb` | 用于查看的静态空间模型，不是完整游戏模型。 |
| `views/{iso,top,north,east}.png` | 形状、俯视占地和侧面的概览图。 |
| `sections/{x_mid,y_mid,z_mid}.png` | 穿过导出范围中心的剖面图。 |
| `render_manifest.json` | 来源/覆盖、渲染方法、近似表示或省略的特性。 |
| `mc-builder-blueprint.json` | 设计方块操作，不含现场快照，也不等于施工授权。 |

阅读[渲染指南](docs/rendering.md)了解图像与 manifest；[渲染取舍](docs/rendering-decisions.md)介绍几何实现的限制。

## 设计建筑并导出蓝图

Agent 可根据用户给出的尺寸、布局和材料生成 mc-builder operations JSON，然后运行：

```bash
mc-spatial blueprint --input ./building-plan.json --out ./building-review
```

检查图像和 manifest，根据反馈修改同一份计划，再用 `--overwrite` 重新生成预览。导出会保留确切方块状态、包含端点的坐标和维度。`hollow` 只放置长方体外壳，不会清除已有方块；空气目标会被拒绝。

生成的 JSON 使用 [mc-builder](https://github.com/lucy-muyue/mc-builder) 蓝图格式。材料白名单、维度限制和场地保护由用户本机的预览校验。如果用户已授权施工，继续按 `preview_build → prepare_build → start_build → status/readback` 使用 mc-builder；Minecraft Spatial Kit 在生成审核文件后结束。详见[工作流指南](docs/workflow.md)。

合成亭子使用 `minecraft:oak_slab`；用户本机的 mc-builder 配置需要启用该材料，才能通过其预览校验。

## 安装与开发

项目支持 Python 3.10+，依赖 `nbtlib`、NumPy、Pillow 和 trimesh。可配合 `requirements-lock.txt` 固定本地依赖版本。CI 只使用合成数据：

```bash
python -m unittest discover -s tests -v
```

请勿提交私有存档的世界目录、数据库、region、精确地点坐标、截图或生成模型。公开示例和 CI 数据均应由合成场景生成。
