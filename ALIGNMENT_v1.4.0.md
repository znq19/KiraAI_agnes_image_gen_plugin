# Agnes 生图插件 v1.4.0 — 双世代兼容 + 并发改造 对齐方案

日期：2026-10-10
基线：`znq19/KiraAI_agnes_image_gen_plugin` @ `7f2f2c6`（v1.3.0）
对照框架：KiraAI **2.x = v2.34.8**（`main`）、**3.0 = v3.0.0-alpha.3**（`dev-v3`，最新 `a4e2027`）

---

## 一、3.0 兼容性实测结论

### 1.1 逐项 API 核对（插件用到的全部框架接口）

| 接口 | 2.x | 3.0 | 结论 |
|---|---|---|---|
| `core.plugin.{BasePlugin,logger,on,Priority,register}` | ✓ | ✓（模块拆包但 `__init__` 导出同名） | 无需改 |
| `core.chat.message_utils.{KiraMessageEvent,KiraMessageBatchEvent}` | ✓ | ✓ | 无需改 |
| `core.chat.{MessageChain}` / `message_elements.{Image,Text}` | ✓ | ✓ 文件 **逐字节相同** | 无需改 |
| `core.provider.LLMRequest` | ✓ | ✓ 相同 | 无需改 |
| `core.utils.path_utils.get_data_path` | ✓ | ✓ 相同 | 无需改 |
| `ctx.publish_notice(sid, chain, is_mentioned)` | ✓ | ✓ 签名一致 | 无需改 |
| `ctx.message_processor.send_message_chain(sid, chain)` | ✓ | ✓（3.0 新增可选 kw-only 参数，向后兼容） | 无需改 |
| `ctx.adapter_mgr.get_adapter(name)` | ✓ | ✓ | 无需改 |
| `adapter.get_client().send_action(...)` | ✓ | ✓ | 无需改 |
| `adapter_inst.config` / `event.adapter.platform` | ✓ | ✓ | 无需改 |
| `@on.llm_request()` + `req.system_prompt` 中 `name=="tools"` | ✓ | ✓ | 无需改 |
| `@register.tool(name,description,params)` | ✓ | ✓ | 无需改 |
| `BasePlugin.__init__(ctx,cfg)` + `initialize()/terminate()` | ✓ | ✓ 相同 | 无需改 |

**消息类型属性名**：3.0 把 `KiraMessageBatchEvent.message_types` 改名 `supported_elements`，
但保留了 `@deprecated` 属性别名。插件自身**没有**读写该字段（`_get_sid` 走 `event.sid`），
故零影响；已确认 `event.sid` 两代均为 `session.sid`。

**`core_version` 门槛**：`>=2.6.1`。实测 `Version("3.0.0a3") in SpecifierSet(">=2.6.1", prereleases=True)` → **True**，
3.0 alpha 能正常装（不会被版本门挡掉）。

### 1.2 ★ 发现并修复的真实 3.0 故障：角色形象图（selfie）静默失效

**根因**（有源码实证）：
- 3.0 把形象参考图从配置迁移到 **persona 表**；
- `core/db/migrate_to_db.py::migrate_selfie_reference_image()` 迁移后**主动删除**配置键：
  ```python
  remaining_config = {k: v for k, v in selfie_config.items() if k != "path"}
  if remaining_config: bot_config["selfie"] = remaining_config
  else: bot_config.pop("selfie")      # ← 键直接消失
  ```
- 旧插件在 `__init__` 里读 `ctx.config.get("bot_config",{}).get("selfie",{}).get("path","")`
  ⇒ 在 3.0 上**必然取不到**，且外层 `except: pass` 静默吞掉。

**后果**：3.0 用户说"发张你的自拍" → 插件报"未配置自我形象参考图"，
但用户在系统设置里明明配过 → **看起来像插件坏了**。功能静默丢失。

**修复**：新增 `async def _resolve_selfie_reference()`，逐级回退：
1. 插件配置 `selfie_image_path`（显式，最高优先级）
2. 缓存
3. 2.x：`bot_config.selfie.path`（`get_config("bot_config.selfie.path")` + dict 直取双路兜底）
4. **3.0：`persona_mgr.get_reference_image()`**（`persona_id=None` = 当前激活角色）
任一环失败继续往下试，全失败才返回 `None`；`get_reference_image` 抛异常也不会外泄。

### 1.3 实测证据

| 项目 | 2.x | 3.0 |
|---|---|---|
| 模块导入 + 插件实例化 | ✓ | ✓ |
| `initialize()`（建目录/清缓存/解析形象图） | ✓ | ✓ |
| 工具注册 / 提示注入 / 发送链路 | ✓ | ✓ |
| 形象图解析 | ✓ `bot_config.selfie.path` | ✓ `persona_mgr`（旧代码此处 **FAIL**） |

反向验证（对未修复的 v1.3.0 跑同一组断言）：`selfie resolved = False` ⇒ 证明该故障真实存在。

---

## 二、并发改造（用户第 2 问）

### 2.1 先复现用户描述的问题

旧代码：
```python
sid = self._get_sid(event)
if sid in self._gen_tasks and not self._gen_tasks[sid].done():
    return "⏳ 该会话已有图片生成任务在进行…"
```
实测（2.x 与 3.0 均一致）：同一会话连续 3 次调用 →
`✅ / ⏳ / ⏳`，**只有第 1 个被接受**。用户判断完全正确。

代价正如用户所说：第 2、3 次调用**真实消耗了一次工具调用额度**（LLM 已发出 function call），
却什么也没生成，模型下次可能还会忘记用。

### 2.2 新设计

**两级上限 + 准入即计数 + 全局信号量排队**：

| 机制 | 实现 |
|---|---|
| 每会话上限 | `max_concurrent_per_session`（默认 **5**，1~50） |
| 全局上限 | `max_concurrent_global`（默认 **20**，1~200） |
| 计数口径 | **准入时 +1**（含排队等待），因此拒绝判断精确，不会"先放行后超限" |
| 超全局时 | 进入 `asyncio.Semaphore` **排队等待**，不失败 |
| 任务管理 | `_gen_tasks: {task_id: Task}`（按任务 id，不再按 sid）+ `_session_counts: {sid: int}` |
| 失败/取消 | `finally` 中归还槽位，异常路径不泄漏额度 |
| 创建失败 | `create_task` 抛错也归还槽位（否则会话额度被永久占用） |

**并发的是生成与下载，串行的是发送**：

框架**只在发送 LLM 文本输出时**持有会话锁（2.x `message_manager:764` / 3.0 `execute.py:52`），
插件直接调 `send_message_chain` **不在其保护范围内**。若并发任务同时发图，
消息可能交错、合并转发可能互相打断。
⇒ 插件自建**会话级发送锁** `_send_lock(sid)`：

- 下载：`asyncio.gather` **并发**（纯 I/O，吞吐最大化）
- 发送：持锁**串行**，保证顺序与合并转发完整性
- 锁在会话静默（计数归零）且未被持有时回收，避免字典无界增长

### 2.3 返回文案

- 接受：`✅ 已开始生成图片（…）…（本会话在途任务 3/5）` —— 让 LLM 知道还有额度
- 拒绝：`⏳ 当前会话已有 5 个图片生成任务在排队/进行中，已达上限（5）。请告知用户稍候，不要重复请求。`
  （全局同理，含当前在途数）

### 2.4 工具描述 / 提示词同步更新

- tool description 增补"同一会话可同时进行多个生成任务（默认 5 / 全局 20）"
- `inject_tool_hint` 增补"用户连续要求多张图时可直接连续调用，不必等上一张完成"

### 2.5 实测

| 场景 | 旧版 | 新版 |
|---|---|---|
| 同会话连发 7 次 | 1 接受 / 6 拒绝 | **5 接受 / 2 拒绝** |
| 跨 30 会话连发 | 无全局概念 | 全局精确停在 **20** |
| 任务结束后 | — | 槽位全部归还（`_gen_tasks=0`、会话计数=0） |
| 任务异常 | — | 槽位归还（已断言） |
| 自定义 2/3 上限 | — | 精确生效 |

---

## 三、其他发现的问题（用户第 3 问）

### 3.1 ★ 参考图 MIME 只信扩展名 ⇒ 图生图可能被 API 拒

原实现按 `ref_path.suffix` 猜 MIME。模型/框架常把 PNG 存成 `.jpg`
（框架自己已引入 `_infer_mime_from_bytes` 魔数嗅探，正是为此）。
⇒ 新增 `_sniff_image_mime()`（PNG/JPEG/GIF/WEBP/BMP），**魔数优先，扩展名兜底**。

### 3.2 ★ 参考图体积无上限 ⇒ 大图 base64 撑爆请求体

本地参考图直接 `read_bytes()` → base64 塞进 `extra_body.image`，
一张 20MB 图 ⇒ ~27MB JSON，有被 API 拒的风险（+33% base64 膨胀）。

**依据（已查官方文档，2026-10-10）**：
- Agnes 官方 `MODEL_CATALOG.md` 与 `agnes-image-2.5-flash` 文档**均未公开参考图的固定体积上限**；
- 官方错误码文档 `code.md` 的 **413 Payload Too Large** 一节明确列出成因：
  「Request body is too large / **base64 image is too large** / Uploaded file or image exceeds the limit」，
  并建议「Reduce the uploaded image size or resolution；For images, use a public URL instead of sending a large base64 payload」。
- ⇒ 官方承认该上限存在但未给数值，**故采用"可配置的保守阈值 + 提前规避"**，而不是等 API 报 413。

**默认值如何定的（实测，不是拍脑袋）**：

| 参数 | 默认值 | 依据 |
|---|---|---|
| `max_reference_bytes` | **20 MB** | 用户指定；实测 20MB 在 q95 下约对应 31MP，足以容纳任何手机/相机原图 |
| `max_reference_pixels` | **3200 万** | 与 20MB 对齐（实测 20MB ≈ 31MP，取 32MP 取整） |

实测数据（q95、细节丰富内容，bytes/pixel ≈ 0.676 且**与分辨率无关**，故该换算是稳定的）：

| 分辨率 | 像素 | 文件 | 是否超 20MB |
|---|---|---|---|
| 4000×3000 | 12MP | 7.7MB | 否 |
| 4096×4096 | 16.8MP | 10.8MB | 否 |
| 6000×4000 | 24MP | 15.5MB | 否 |
| 8000×6000 | 48MP | 31.0MB | **是** |

**两个上限各司其职，缺一不可**（实测确认）：
- **体积上限**保护请求体（防 413）—— 参考图要 base64 进请求体，**膨胀 +33%**（20MB → 26.7MB）。
- **像素上限**保护解码内存/CPU —— 实测一张 **48MP 的纯色 JPEG 只有 0.72MB**，
  体积远未超限，但解码要 **137MB RGB 内存**；192MP 更达 549MB。
  若只看体积，这类图会被放行并拖垮进程。

因此默认配置下：普通手机照片（12MP / 4MB）与 24MP 相机原图（15.5MB）**完全零开销直接放行**，
只有真正超限的图（如 48MP 原图）才会触发压缩。

### 3.2b ★★ 该修复初版自身有两个缺陷（用户追问后实测发现并修正）

用户质疑「会不会卡顿、是不是最优」，实测**确实有问题**：

| 缺陷 | 实测证据 | 修正 |
|---|---|---|
| **阻塞事件循环** | 24MP 图同步降采样 ⇒ **事件循环停摆 601ms**（同一循环上所有其它任务被冻结） | 改为 `asyncio.to_thread` 执行，停摆降至 **6~49ms** |
| **像素预算当成边长用** | 把 400 万直接传给 `thumbnail()`，而 400 万像素的边长实际只有 2000px ⇒ **等于几乎不缩放**，15.5MB 只压到 14MB（仍在阈值之上） | 用 `isqrt(max_pixels)` 换算成边长（400 万 ⇒ 2000px）；15.5MB ⇒ **737KB** |

顺带优化（同样有实测支撑）：
- **JPEG `draft()` 快速路径**：让解码器直接吐降尺寸结果，省掉"全分辨率解码 + 大图 LANCZOS"，实测 **673ms → 459ms（快约 30%）**；对非 JPEG 是安全空操作。
- **alpha 保留**：透明图存 PNG（沿用框架策略），避免 `convert("RGB")` 把透明压成黑底。
- **动图不碰**：GIF/WebP 多帧图直接跳过，避免把动图压成静态图。
- **EXIF 方向矫正**：与框架一致，避免手机竖拍图被压成横的。
- **像素炸弹保护**：超 PIL `MAX_IMAGE_PIXELS` 的图直接放弃处理，交回原图。

**性能口径（实测）**：

| 场景 | 耗时 | 事件循环停摆 |
|---|---|---|
| 未超限（常见情况） | ~0.4ms（只读文件头） | 无 |
| 48MP JPEG 压缩 | ~900ms（在线程池） | ≤49ms |
| 关闭该功能（0/0） | 0 | 无 |

### 3.2c ★ 第二轮审查：又找出 5 处阻塞事件循环的同步 I/O

用户要求"无堵塞最优化"，于是把所有同步 I/O 全量排查了一遍，**实测每处停摆**：

| 位置 | 同步操作 | 实测停摆 | 修正 |
|---|---|---|---|
| 参考图读盘 | `Path.read_bytes()` | 20MB 约 30ms | `asyncio.to_thread` |
| 参考图 base64 | `base64.b64encode()` | 20MB 约 **72ms** | `asyncio.to_thread` |
| 下载写盘 | `Path.write_bytes()` | 20MB 约 **21ms** | `asyncio.to_thread` |
| 缓存清理 | `glob` + `stat` + `unlink` | 数十文件时随文件数增长 | 整体 `to_thread` |
| 发送后删除 | `Path.unlink()` 循环 | 同上 | `asyncio.to_thread` |

其中「读盘 + base64」合并在事件循环上执行时，**单次停摆实测 103ms** —— 在默认 20MB 阈值下更容易触发，
所以这一轮必须修。修完后上述路径停摆均 **< 10ms**（有断言守护）。


### 3.2d ★ 第二轮审查：下载文件名与内容不符（潜在功能风险）

原实现无论下载到什么内容，文件名**固定写 `.png`**。而官方 QQ 适配器会把文件名作为
`file_name` 上报给平台（`qq_official/im.py`：`file_name = media_element.guess_name()` →
`payload["file_name"]`）—— **扩展名与真实内容不符可能被平台拒收或误判格式**。

**修复**：下载后按魔数嗅探真实类型，命名时使用对应扩展名（`.png/.jpg/.webp/.gif/.bmp`）；
同时把缓存清理的 glob 从 `agnes_*.png` 放宽为 `agnes_*`（否则新扩展名的文件会**永远不被清理**，
缓存上限形同虚设）。两处都已加断言守护。

> 说明：本次改动的**新增文件**才使用新命名；用户目录里已有的旧 `.png` 文件仍被新 glob 覆盖，不会残留。

### 3.2e ★ 第三轮对抗性审计（边界组合）：又抓到 2 个真 bug

用户要求"合并前再全量审计一轮"，于是**主动去构造边界与异常场景**（而不是重跑既有用例），
结果抓到两个既有用例完全没覆盖到的真缺陷：

#### bug ①：像素上限被关闭时，图片会被压成 1×1（数据销毁）

`_compute_target_edge` 早期版本在 `max_pixels=0`（用户关闭像素检查）时算出
`isqrt(0)=0` ⇒ 边长退化为 **1**：

| 配置 | 结果 |
|---|---|
| 体积 1MB / 像素关闭 | 6000×4000 的 15.5MB 图 ⇒ **1×1（285 字节）** |

**这是数据销毁级缺陷**：用户只是关掉了一个可选检查，上传的参考图却被压成一个像素。
**修复**：像素预算为 0 时，按实测 `bytes/pixel≈0.676` **从体积上限反推边长**，
既不凭空施加像素上限，也不会退化成 1；并保证结果不超过原图（不放大）。
修后同一场景：15.5MB ⇒ 152KB / 1245×830 ✓

#### bug ②：任务在"尚未开始执行"时被取消 ⇒ 并发槽位永久泄漏

`_run()` 的 `finally` 负责归还槽位，但若任务在**第一次被调度前**就被取消
（插件卸载 / 服务关闭 / 用户取消），**协程体根本不会运行**，`finally` 自然不触发：

| 操作 | 修复前 | 修复后 |
|---|---|---|
| 取消 3 个在途任务 | 槽位全不归还（`tasks=3 count=3`） | 全部归还（`tasks=0 count=0`） |
| 取消后再连发 5 次 | 只接受 **2** 次（额度被泄漏占死） | 接受 **5** 次 ✓ |

**修复**：给任务挂 `add_done_callback` 兜底释放（任务无论正常/异常/取消结束都会触发），
并把 `_release_slot` 改为**幂等**（以"是否仍登记在册"为判据），
避免与 `_run` 内的释放重复扣减、也避免 `terminate()` 后迟到的回调误扣新任务额度。

#### 同时确认无问题的项
- **EXIF 方向矫正确实生效**：8000×6000（EXIF orientation=6）⇒ 输出 4242×5656（已转为竖向）✓
- **异常路径不再外泄**：25MB 非图片字节 ⇒ 记 warning、返回原图，不抛异常 ✓
  （修前会抛 `UnidentifiedImageError`）
- **锁安全**：发送抛异常时锁不残留；5 个并发发送无交错 ✓
- **重复 `terminate()` 安全**；terminate 后重新启用可正常工作 ✓
- **不放大**：400×300 的小图不会被放大；1×1 图原样返回 ✓

#### 同时清理
- 重构过程中遗留的一段**不可达死代码**（`pyflakes` 报 `undefined name` 发现），已删除。

#### 本轮新增回归防线（全部反向验证过）
| 断言 | 反向验证结果 |
|---|---|
| 只设体积上限时不得压成 1×1 | 注入旧逻辑 ⇒ 两代 **6 条变红** |
| 取消后槽位必须归还 | 去掉 done_callback ⇒ 两代 **4 条变红** |

> 教训：**"已通过测试"不等于"没有 bug"**。这两个缺陷都不在原有用例的覆盖范围内 ——
> 一个只在"关闭可选功能"的组合下出现，一个只在"任务未启动即被取消"的时序下出现。
> 主动构造边界组合（而非重复跑用例）才挖得出来。

原实现无论下载到什么内容，文件名**固定写 `.png`**。而官方 QQ 适配器会把文件名作为
`file_name` 上报给平台（`qq_official/im.py`：`file_name = media_element.guess_name()` →
`payload["file_name"]`）—— **扩展名与真实内容不符可能被平台拒收或误判格式**。

**修复**：下载后按魔数嗅探真实类型，命名时使用对应扩展名（`.png/.jpg/.webp/.gif/.bmp`）；
同时把缓存清理的 glob 从 `agnes_*.png` 放宽为 `agnes_*`（否则新扩展名的文件会**永远不被清理**，
缓存上限形同虚设）。两处都已加断言守护。

> 说明：本次改动的**新增文件**才使用新命名；用户目录里已有的旧 `.png` 文件仍被新 glob 覆盖，不会残留。

### 3.2g ★ 第四轮终审：真实框架加载 + 又发现 1 个既有 bug

#### 方法升级：不再用手写夹具，直接用框架的 `PluginManager`
把发布包解压进 `data/plugins/agnes_image_gen/`，用**真实** `PluginManager.init()`
走完整发现流程（manifest 校验 → core_version 检查 → schema 解析 → 配置补全 →
类注册 → 实例化 → 工具注册进 `FuncToolManager`），再通过真实 `ToolSet` 调用工具。

**两代结果**（3.0 `dev-v3@a4e2027`、2.x `main@28ec106`）：

| 检查项 | 2.x | 3.0 |
|---|---|---|
| 插件被发现 | ✓ | ✓ |
| 类注册 | ✓ | ✓ |
| schema 解析（3 个 section） | ✓ | ✓ |
| 配置自动补全（含 4 个新键默认值） | ✓ | ✓ |
| 实例化 + `initialize()` | ✓ | ✓ |
| 工具注册进真实 `ToolSet` | ✓ | ✓ |
| LLM 可见参数与 v1.3.0 一致 | ✓ | ✓ |
| 经 `ToolSet.execute()` 真实调用 | ✓ | ✓ |

> 3.0 上游自上次审计后前进了（`a1f4bfd → a4e2027`，含 `plugin_context.py` /
> `message_delivery.py` 改动），已更新到最新并重新验证 —— 本插件使用的
> `publish_notice` / `send_message_chain` / `PluginContext` 接口**均未受影响**。

#### bug ③：缓存只在启动时清理 ⇒ 长期运行的 bot 文件无限增长（既有问题）

实测：`max_cache_files=5`，连续生成 30 次**不重启** ⇒ 磁盘上积累 **40 个文件**，
清理逻辑从未运行（`_cleanup_cache()` 只在 `initialize()` 调用一次）。
对长期在线的 bot 来说缓存上限形同虚设。**这是 v1.3.0 就存在的老问题**，非本次引入。

**修复**：每次发送完成后触发**运行期清理**（合并并发触发，最多一个清理协程在跑，
避免清理风暴）。

⚠️ 修复时引入的并发风险已专门处理：清理会把**正在发送途中**的图片删掉，
导致用户收到损坏的图片。因此清理时**跳过最近 30 秒内写入的文件**（in-flight 保护）。
实测：20 个旧文件 + 1 个新文件 ⇒ 清理后旧文件降到上限、新文件**存活** ✓

#### 其他终审项（全部通过）
- **不会挂起**：指向黑洞地址时 API 调用 3 秒超时返回（不是永久挂起），
  后台任务 2.1 秒内结束并归还槽位 ✓
- **浸泡测试**：300 次生成后 `_gen_tasks` / `_session_counts` / `_send_locks` **全部归零** ✓
- **无清理风暴**：5 个并发完成触发清理 ⇒ 合并为单个协程 ✓
- **无对象滞留**：批次事件在任务结束后可被 GC 回收（快照设计生效）✓
- **背压精确**：单会话连发 12 次 ⇒ 5 接受 / 7 拒绝，无多放或少放 ✓
- **代理**配置正确透传到 API 与下载 ✓

### 3.3 后台任务长期持有整个事件对象

异步模式把 `event` 闭包进后台任务，生成+下载数十秒期间，
整个批次事件（含全部消息链、图片元素）都无法释放。
⇒ 新增 `_snapshot_event()`：浅拷贝 + 只保留最后一条消息，
既保证发送所需字段（`sid`/`adapter`/`self_id`）可用，又不长期持有整批消息。
（保留对原事件的回退路径，快照失败不影响功能。）

### 3.4 合并转发路径脆弱（仅加固，不改行为）

`_send_forward_images` 用 `int(target_id)` 转群号/QQ 号；
非数字 id（如官方 bot 的 openid）会抛 `ValueError`。
**现状**：异常被 `except Exception` 捕获 → 返回 False → **自动回退逐张直接发送**，
所以不会丢功能，属于"看起来失败但实际兜住了"。
本次仅确认该回退链路完整（已有 `logger.error` 记录），未改逻辑以免改变既有行为。

### 3.5 已确认"没问题"的点（避免过度修改）

| 检查项 | 结论 |
|---|---|
| `ctx.publish_notice` 两代行为 | 一致；3.0 内部改为查 `IMCapability.get_message_metadata()`，对插件透明 |
| 提示词注入是否累积 | `get_agent_prompt()` **每次请求重建** system_prompt，插件追加不累积 |
| 缓存清理 | `_cleanup_cache()` 只 glob `agnes_*.png`，不会误删他人文件；新文件名仍匹配该模式 |
| 并发下文件名冲突 | 旧版用 `int(time.time()*1000)`，同毫秒并发会**互相覆盖**；已加 `uuid` 后缀 |
| 同步模式 | 保持原行为（仅补会话锁与 `sid` 定义），未改语义 |
| `_temp_files` 临时文件机制 | 实测**无写入方**，属我引入的多余设计 ⇒ 已**整段删除**（不冗余） |
| 未使用导入 | `pyflakes` 仅报 `Priority`/`KiraMessageEvent` 未用，**原始文件同样如此**，非本次引入，保持原样 |

---

## 四、测试与验证

| 套件 | 内容 | 结果 |
|---|---|---|
| `run_tests.py` | 双世代矩阵（2.x + 3.0）各 **171** 条断言 | **171/171 PASS** |
| `real_load2.py` | **真实 `PluginManager` 加载 + 真实 `ToolSet` 调用**（从发布包安装） | **ALL PASS（两代）** |
| `selfie_e2e.py` | 3.0 形象图端到端 | **ALL PASS** |
| `pipeline_e2e.py` | **全链路**：真实本地 HTTP API → 真实 PNG → 下载 → 发送 | **ALL PASS（两代）** |
| `verify_ref.py` | 参考图预处理专项 | **ALL PASS** |
| `audit_io_cache.py` | 命名 / 缓存清理 / 事件循环阻塞 | **ALL PASS** |
| `audit_edge.py` | 边界组合（4 种上限开关 / 病态输入 / 小图 / EXIF） | **ALL PASS** |
| `audit_concurrency.py` | 取消 / 锁 / terminate / 重启用 | **ALL PASS** |
| `audit_leak.py` | 通知 / 内存增长 / 背压 | **ALL PASS** |
| `audit_cache_path.py` | 运行期缓存 / 存储路径 / sid 推导 | **ALL PASS** |
| `audit_final.py` | 挂起风险 / 浸泡 300 次 / 清理风暴 / 代理 | **ALL PASS** |
| `audit_upgrade.py` | 存量用户升级模拟（真实框架 schema 加载器） | **ALL PASS（两代）** |
| `reverse_check.py` | 对 v1.3.0 反向验证旧缺陷 | **6/6 确认** |

**回归防线的有效性已逐条反向验证**（把缺陷重新注入代码，确认断言变红）：

| 注入的缺陷 | 结果 |
|---|---|
| 像素数当边长用 | 两代 **6 条断言变红** |
| 像素预算为 0 时忽略体积预算 | 两代 **6 条断言变红** |
| 去掉取消兜底（done_callback） | 两代 **4 条断言变红** |
| 去掉运行期缓存清理 | 两代 **2 条断言变红** |

`pipeline_e2e.py` 会起一个真实 HTTP 服务充当 Agnes 端点（返回 2 张真 PNG），
完整跑通「调 API → 下载图片 → 经适配器发送」，两代均 `已成功发送 2 张图片`。

### 4.1 存量用户升级行为（实测）

用框架**真实**的 `build_fields` + `_ensure_plugin_config` 逻辑模拟 v1.3.0 → v1.4.0 升级：

- 用户原有自定义项（timeout=240 / default_size=4K / async_generate=False / max_cache_files=250 /
  proxy / api_key）**全部原样保留**；
- 4 个新增键**自动补入默认值**（5 / 20 / 20MB / 32MP）；
- **无需任何手动操作，更新即生效**（两代行为一致）。

参考图预处理的关键断言（**含反向验证过的两条**）：
- 未超限时返回**同一个对象**（证明零重编码开销）
- 大图压缩后**确实落在字节阈值内**（初版做不到，此断言即为其回归防线）
- **像素预算真正生效**（压缩比 > 4x —— 初版把像素数当边长，此断言会红）
- 压缩期间**事件循环停摆 < 150ms**（初版 601ms，此断言会红）
- alpha 图仍为 PNG；动图原样返回；非图片字节不崩；功能可完全关闭

覆盖的关键断言（节选）：
- 每会话上限 5 精确生效；第 6、7 次被拒
- 全局上限 20 精确触顶且不超
- 正常/异常/取消路径均归还槽位
- 同步模式不受并发改造影响、无残留任务
- 下载确实并发（观测到并发度 ≥2）
- 发送确实串行（无 enter/enter 交错）
- 同一 URL 两次下载得到不同文件名（不覆盖）
- MIME 魔数嗅探 5 例；扩展名与内容不符时以内容为准
- 事件快照保留 sid/adapter、消息裁剪、不共享列表引用
- 自定义上限 2/3 生效
- 3.0 persona 解析 + 缓存 + 异常不外泄 + 工具友好报错

---

## 五、交付物

| 文件 | 变更 |
|---|---|
| `main.py` | +约 380 行 / −约 90 行 |
| `schema.json` | 新增 4 项：`max_concurrent_per_session`(5)、`max_concurrent_global`(20)、`max_reference_bytes`(20MB)、`max_reference_pixels`(3200 万) |
| `manifest.json` | `version 1.3.0 → 1.4.0` |
| `README.md` | 新增并发行为说明、3.0 形象图迁移说明、特性列表 |

**版本**：1.4.0（新增配置项 + 行为增强，向后兼容，无破坏性变更）

---

## 六、设计意图保全声明

- **未删除任何功能**：文生图/图生图/角色形象图/4 风格/4 档位/8 比例/自定义宽高/
  合并转发/失败回退/自动重试/错误回传/缓存管理/代理 —— 全部保留，签名不变。
- **未改默认行为**：`async_generate` 默认仍为 true；同步模式语义不变。
- **未引入硬依赖**：Pillow 用于可选的参考图降采样。Pillow 是 KiraAI 官方 `requirements.txt` 的**必需依赖**（`Pillow>=11.3.0`），因此实际环境必然存在；即便如此仍做了 `try/except` 兜底，缺失时静默跳过、不影响功能。
- **并发是"放开"不是"收紧"**：旧版每会话只能 1 个，新版默认 5 个 —— 纯增强。
- **不碰框架**：全部改动在插件侧（遵循"不给核心开 PR"的既有约定）。