# 从 cad-ai-agent 借鉴：图腾演进计划

> 状态：计划稿（只读调研，未改任何代码）
> 分支：`feat/windows-support`（HEAD `d3b4962`）
> 参考项目：`/workspace/ref/cad-ai-agent`（Apache-2.0，主许可文件 `LICENSE`；`NOTICE` 只存在于其自带的 `.audit-harness/NOTICE` 子目录，不能代表整个仓库的版权所有方）
> 本文件里引用的每一条路径、函数名均来自实际读过的源文件，文末附自检说明。

---

## 0. 先看结论

1. **图腾不需要“重做”的，是改图管线本身。** 图腾已经有：LLM 输出的 JSON「方案」、`app/apply.py` 的半内存预览、确认后写 `.studio/work.dxf` 与快照、撤回、`app/placement.py` 的几何校验与吸附、`app/init_proj.py` 的后台初始化三步骤进度。cad-ai-agent 的大半模块（pydantic 模型层、EditPlan→ChangeSet→EditEngine、`EditHistory`、`EditPreviewBuilder`、GCS 会话存储、`odafc.readfile` 直接转）是**另一套架构**，整段搬过来只会与图腾现有的“方案字典”格式打架。
2. **值得借的**是四类很窄的东西：① 结构化校验产物的形状（`PlanValidationStatus` / `blockers` / `warnings` 这类词汇，而不是它的 pydantic 类）；② 对 ODA File Converter 的**预检与失败处理思路**；③ 合规/算量里“规则可配置 + 逐条证据”的思路；④ 预览里 before/after 与 destructive 标记的展示思路。
3. **Phase 1 选“DWG→DXF 转换抽象”**：把现有的 `find_dwg2dxf()` / `convert_dwg()` 抽成 `app/convert/` 包，ODA 优先、dwg2dxf 兜底、路径可由环境变量指定，并顺手把 README 里“v1 不写入 DXF”和“只提 dwg2dxf”两句改准确。理由：风险最低、不碰数据格式、不依赖真实图纸和二进制即可验证（可 mock），且直接解决“DWG 上传成功率”这个对线上其他用户最痛的问题。

---

## 1. 来源与现状盘点（对照真实代码）

以下每条都是在本分支上实际读到的内容；行号以 HEAD `d3b4962` 为准，后续改动后可能漂移，以函数名为准。

### 1.1 图腾：方案 → 预览 → 应用 已经是完整闭环

| 环节 | 图腾现状 | 位置 |
|---|---|---|
| 方案 JSON 格式 | 由 `SYSTEM_PROMPT` 里的那一段字面量约定：`mode`、`inserts`、`deletes`、`replaces`、`moves`、`pipes`、`texts`、`do_not_touch`。没有类型化的 schema 类。 | `app/prompts.py` 的 `SYSTEM_PROMPT` |
| 解析 | `maas.extract_assistant_payload()` 尽力从模型文本里抠出 JSON 对象，失败返回 `None`；`prompts.is_edit_plan()` / `plan_has_edits()` 判断是不是带改动的方案。 | `app/maas.py`、`app/prompts.py` |
| 校验 | `placement.check_plan()` 返回 issue 列表，每项 `{"code","severity","message","op","index","block","xy"}`，severity 为 `error` / `warn`。 | `app/placement.py` |
| 校验→回喂 | `api_chat` 生成 `run()` 之后，`_check_plan_coords()` → `_error_issues()` → `build_repair_message()` 最多回喂 `MAX_PLAN_REPAIRS = 1` 次。 | `app/main.py`、`app/prompts.py` |
| 预览 | `apply.render_plan_preview()`：在内存文档上跑 `apply_mutations()`，只渲染“改动附近取景窗口”，并打 `STAMP_PREVIEW = "方案预览，尚未写入工作图"`；不写盘、不建快照。 | `app/apply.py`、`app/placement.py` 的 `context_bbox()` |
| 确认应用 | `apply.apply_plan()`：`ensure_work_copy()` → 快照 `revisions/rev-NNNN.dxf.gz` → `apply_mutations()` → `_save_doc()` 写 `.studio/work.dxf` → `_write_session_source()` 写回会话源 DXF。 | `app/apply.py` |
| 撤回 | `apply.undo_last()` 从快照恢复，最后一步撤回时删掉工作副本、恢复会话 DXF。最多保留 `MAX_REVISIONS = 5` 步。 | `app/apply.py` |
| HTTP 入口 | `POST /api/projects/{project_id}/apply`、`POST /api/projects/{project_id}/undo`、`GET /api/projects/{project_id}/apply`。 | `app/main.py` |
| 写入锁 | `projects.project_write_lock()`（进程内 threading.Lock），通过 `apply._with_project_write_lock` 包住 `apply_plan` / `undo_last`。 | `app/projects.py`、`app/apply.py` |

注意一个与 README 冲突的事实：**应用/撤回确实会写 DXF**（`.studio/work.dxf` 和会话的 `out/source.dxf`），而 `README.md` 第 3 行仍写着“v1 只出方案，不写入正式 DXF / DWG”。这句话在 Phase 1 里改掉（见 §5）。

### 1.2 图腾：DWG → DXF 现状

| 函数 | 现状 | 位置 |
|---|---|---|
| `find_dwg2dxf()` | 先读 `CAD_DWG2DXF` 环境变量，再依次试 4 个硬编码 Linux 路径和 `shutil.which("dwg2dxf")`；Windows 上再补 `_windows_dwg2dxf_candidates()` 的 `%LOCALAPPDATA%\Programs` 与 `C:\Program Files\DWG` 等目录，以及 `_which_where()`。**没有 ODA 分支。** | `app/init_proj.py` |
| `convert_dwg(src, dest)` | 只用 dwg2dxf。把源文件拷进 `dest.parent/.convert-tmp`，先试 `-y -o out.dxf src`，不成再试 `-y src`，以“有没有产物文件”判断成功（因为 dwg2dxf 可能 exit 0 却打印 `WRITE ERROR`）；成功后规范化编码，失败抛中文 `ValueError`。 | `app/init_proj.py` |
| 调用方 | `_ensure_dxf()`（由 `_prepare()` 调用，属于初始化 `save` 步骤）：源是 `.dwg` 时 `convert_dwg(src, out/source.dxf)`。 | `app/init_proj.py` |
| 上传 | `create_session_from_upload()` 只收 `.dwg/.dxf`，落盘到 `source/`，`deliverable = "out/source.dxf"`，然后 `start(proj)` 后台跑初始化。 | `app/init_proj.py`、`/api/projects/upload` 在 `app/main.py` |
| 已知隐患 | ① 失败信息只有 dwg2dxf 的最后一行输出，用户看不出“服务器没装转换器”还是“这份 DWG 坏了”。② `CAD_DWG2DXF` 只在 README 出现过（第 58 行），`.env.example` 里没有。③ 规范化那一步读的是**已经落到 dest 的**原始转换稿（失败时保留原始转换稿，这一点是安全的），但没有记录“用了哪个工具、输出是哪个 DXF 版本”。 |

已知样本图的相关事实（`known-indexes/wanshangcheng/INDEX.md`、`known-indexes/xueshengshitang/INDEX.md`）：两套样本 DWG 都是 LibreDWG 0.14 `dwg2dxf` 转的，写出**版本不一致**（AC1018 / AC1015）、**编码不一致**（GBK / UTF-8），且**INSERT 的属性（ATTRIB）基本读不到**。这说明转换器的选择会实实在在改变索引质量——这是把转换器抽象出来、明确记录“用了哪个工具、什么版本、什么编码”的直接理由。

### 1.3 图腾：跨平台与部署现状

- 字体：`app/config.py` 的 `_pick_cjk_font()` 先看 `CAD_CJK_FONT` / `CAD_CJK_FONT_BOLD`，其次 Windows 字体（`msyh.ttc` 等），最后 Linux Noto CJK；`macOS` 目前**没有默认候选**。`app/cad_render.py` 顶部用 `font_manager.addfont(config.CJK_FONT)` 注册。
- 文件锁：`app/filelock.py` 在 POSIX 用 `flock`，Windows 用 `msvcrt.locking`。`app/archive_queue.py` 的 `start_worker()` 用它在 `archive-queue/worker.lock` 上选主——**仍是单进程语义**。
- 原子写：`app/atomicio.py` 的 `replace_with_retry()` / `write_text_atomic()` / `read_text_tolerant()` 处理 Windows 共享冲突。注意它**没有 `fsync`**；`apply._write_snapshot()`、`apply._restore_snapshot()`、`apply.save_history()` 直接 `write_text` + `replace`，没走 `atomicio`。
- 部署：`deploy/cad-studio.service`（`uvicorn --workers 1`）、`deploy/nginx-cad.visitworld.me.conf`（`client_max_body_size 80m`，与 `MAX_UPLOAD_BYTES = 80MB` 对齐）、`deploy/pack.sh`、`deploy/rsync-exclude`。仓库里**没有 Dockerfile / docker-compose / Makefile / pyproject / tests 目录 / docs 目录**；所有自测都是 `scripts/test_*.py` 独立脚本。
- 另有一个隐患：`app/config.py` 顶部 `load_dotenv()` 之后还会尝试读 `/home/box/agent-data/secrets/*.env`——这是开发机的个人路径，放到 Linux 服务器和 Windows 上都是无意义的探测，属于部署考虑里的清理项（不在 Phase 1 强行改）。

### 1.4 cad-ai-agent 的结构（实际读过、与本次相关的部分）

| 模块 | 它做什么 | 对本计划的意义 |
|---|---|---|
| `src/cad_dxf_agent/core/converter.py` | `convert_to_dxf()` 分派 dxf/dwg/pdf。**dwg 分支**（`_convert_dwg()`）：`odafc.is_installed()` 不成立就返回“DWG 转换不可用——请导出 DXF”的错误结果；成立就 `odafc.readfile()` + `doc.saveas()`。没有 dwg2dxf 兜底。PDF 分支用 PyMuPDF/pdfplumber 抽矢量。`_ACDB_MAP` / `_ODAFC_MAP` 是版本映射表。 | 借鉴“先预检、失败给明确错误”的骨架；**不借** PDF 分支（图腾定位是 DWG/DXF）。 |
| `src/cad_dxf_agent/core/converter.py` 的 `ConversionResult` | dataclass：`source_path / source_format / output_path / success / warnings / error`。 | 借鉴**形状**：转换结果应当是带 `warnings`、带“用了哪个工具”的结构体，而不是裸 `Path` 或直接抛异常。 |
| `src/cad_dxf_agent/settings.py` | `oda_path: str \| None = os.getenv("CAD_ODA_PATH")`（注释说“auto-detected if None”）。 | 借鉴 `CAD_ODA_PATH` 这个环境变量名与“显式路径优先、否则自动发现”的思路。注意：**它的注释与 ezdxf 1.4.5 的实际行为不符**（见 §4.2），照抄会踩坑。 |
| `src/cad_dxf_agent/models/ops_schema.py` | `OpType`（move/edit_text/delete/add_block/rotate/copy/scale/mirror/add_line…）与 `EditOperation(op_type, target_handle, target_layer, target_space, params)`、`ChangeSet`。 | 这是**按实体 handle 编辑**的模型，和图腾“按块名 + 世界坐标编辑”的方案模型不同。借概念（显式枚举 + params），不借类。 |
| `src/cad_dxf_agent/models/plan_schema.py` | `EditPlan` / `EditAction`，带 `PlanValidationStatus`（valid / valid_with_warnings / blocked / needs_clarification）、每个 action 的 `validation.blockers/warnings`、`approval_requirements`、`requires_approval`。 | 借**词汇**：图腾的 issue 只有 error/warn 两级，可以补一个“需要澄清”的语义（见 Phase 3）。 |
| `src/cad_dxf_agent/core/plan_validator.py` | `PlanValidator.validate()`：保护图层（`RuleConfig.protected_layers`）、动作与实体类型兼容、低置信度、缺证据、批量动作要求审批。 | 借**保护图层**这一条（图腾目前没有概念，改图可以落到任意图层上）。 |
| `src/cad_dxf_agent/core/apply_pipeline.py` | `ApplyPipeline.apply()`：先查 blocked / needs_clarification / approval，再 plan→ChangeSet→`EditEngine`→保存→插修订说明→汇总 `ApplyResult`。保存的是**新的输出路径**。 | 借“应用前复查一次状态、应用后返回结构化结果”，图腾的 `apply_plan()` 返回值已接近。 |
| `src/cad_dxf_agent/core/edit_history.py` | `EditHistory`：内存快照栈 + undo/redo。 | **不借**：内存态、无持久化，和图腾“跨请求、跨重启、落盘快照”的 `apply.undo_last()` 不是一回事。 |
| `src/cad_dxf_agent/core/preview_builder.py` / `models/preview_schema.py` | `EditPreviewBuilder.build()` 生成逐动作 `before_state` / `after_state`、`is_destructive`、风险等级；blocked 时直接返回 blocked 预览。 | 借**展示思路**：前端逐条列出“改什么、是否破坏性、有没有警告”。图腾现在只有 `ok_count/failed_count/results`。 |
| `src/cad_dxf_agent/core/compliance_rules.py` / `models/compliance_schema.py` | 确定性合规检查：`ComplianceFinding(rule_id, category, severity, title, measured_value, required_value, unit, entity_handles)`、`ComplianceProfile` + `ComplianceThresholds`；内置 ADA / IBC / residential 三档，阈值单位是**英寸**。 | 借**结果形状**与“阈值可配置”。阈值本身是美制规范，中国项目要换成 GB 系列（见 Phase 5）。 |
| `src/cad_dxf_agent/core/takeoff_engine.py` / `models/takeoff_schema.py` | `generate_takeoff()`：按块统计数量、按图层累计线长（单位 `drawing_units`）、按区域算面积、按图层计数。**没有图例比例、没有单位换算、没有管径/规格。** | 只能当“按块名计数”的参考。诚实说法：它对给排水专业的算量**不够用**。 |
| `src/cad_dxf_agent/core/session_store.py` | `SessionStore` ABC + 内存 / GCS 实现，TTL 2 小时，会话里有 `original_path / working_path / edited_path` 三份路径。 | 借“original / working / edited 三分路径”的命名；**不借** GCS 存储（图腾是单机 Linux + 本地盘）。 |
| `src/cad_dxf_agent/core/revision_notes.py` | 应用后往 `AI_REV_NOTES` 图层写一条确定性修订说明（不用 LLM）。 | 图腾已有 `apply.excerpt_working()` 生成“工作图已确认改动”文本回喂模型，但不写进图。可作为 Phase 3 的一个可选小项。 |
| `src/cad_dxf_agent/Dockerfile` | 只装 `web/backend/requirements.txt`，**不含 ODA**。 | 参考它“ODA 不塞进镜像”的取舍。 |
| `tests/unit/test_converter.py` | `TestDwgConversionWithOda` 用 `monkeypatch.setattr(odafc, "is_installed", ...)` 与 `readfile` 模拟 ODA。 | 直接借鉴测试手法（mock odafc），与 Phase 1 的测试策略一致。 |

---

## 2. 借什么、不借什么（逐项对照图腾已有代码）

### 2.1 类型化 / 结构化编辑操作 schema

- **图腾已有**：`SYSTEM_PROMPT` 约定 + `plan_has_edits()` 的字符串键检查 + `apply_mutations()` 里按 `inserts/deletes/replaces/moves/texts/pipes` 的循环。
- **差距**：键名拼写错误、坐标不是二元数组、`rot` 是字符串这类问题，目前要么被 `apply_mutations()` 静默判成“格式不对”，要么等到 `placement.check_plan()` 才以 `missing_xy` 警告出现。
- **借法（Phase 3）**：新增 `app/planschema.py`，用 **Python `dataclasses`** 而不是 pydantic——图腾 `requirements.txt` 没有 pydantic（`app/main.py` 里用到的 `BaseModel` 是 FastAPI 传递依赖，不宜把它当成编辑方案的校验工具），而 dataclass + 手写 `from_plan()` 只有两三百行，好测。
  - 输出形状对齐 `plan_validator.py` 的语义：`normalized_plan`、按 `(op, index)` 归位的 `blockers`（对应现在的 error）与 `warnings`、`needs_clarification` 标志。
  - **不搬** `EditOperation.target_handle`：图腾按块名 + 世界坐标定位（`apply.find_symbol()` 支持底层块内嵌套 INSERT），改成 handle 编辑等于重做整个 `apply.py`。

### 2.2 计划 → 预览 → 应用

- **图腾已有**：见 §1.1，三阶段**已经存在**，且预览用的是同一套 `apply_mutations()`（注释原文：“预览与正式写入走同一套吸附，所见即所得”）。
- **不借**：`EditPlan → plan_to_changeset() → EditEngine` 这条转换链。它是为了兼容 cad-ai-agent 自己的旧 ChangeSet 而存在的胶水层，图腾没有 ChangeSet，搬过来只会多一次转换。
- **借法（Phase 3/4）**：只借**展示与状态词**：
  - `render_plan_preview()` 已返回 `results`，补充逐项 `before` / `after` 摘要与 `destructive` 标记（删除、覆盖 wipeout 算破坏性）；
  - 对 `blocked` / `needs_clarification` 的方案，预览直接返回“被拦下”而不是渲染一张看起来没问题的图（`preview_builder.build()` 的做法是对的）。

### 2.3 另存 / 版本管理：永不覆盖原始上传

- **图腾已有**：上传原件 `source/<文件>`，转换/交付稿 `out/source.dxf`（`init_proj.create_session_from_upload()`）；应用时先 `_ensure_original_snapshot()` 存 `revisions/original.dxf.gz`，再按 `rev-NNNN.dxf.gz` 快照；`apply.status()` 返回 `official_untouched`、`source_written` 两个字段；`/download?source=original|official|work` 三档下载。这已经**强于** cad-ai-agent 的 `SessionStore` 三路径。
- **差距（真实存在的小坑）**：
  1. `apply._write_session_source()` 会把应用后的工作图写回会话的 `official_dxf(proj)`（即 `out/source.dxf`）。它不是“原始上传”（原件在 `source/` 不动），但名字叫 official，容易误读；`official_untouched` 在应用后变 `False`，语义是“会话源已被写过”。
  2. `apply.save_history()` 用 `tmp.replace(path)`，`_write_snapshot()` / `_restore_snapshot()` 同样，未走 `atomicio.replace_with_retry()`，Windows 上与其他进程同时读 `revisions.json` 时有共享冲突风险。
  3. 每次应用的快照写入到历史追加之间没有 `fsync`，断电后可能出现 `revisions.json` 有记录、快照文件缺失（`undo_last()` 已能识别并报“找不到这一步的快照”，不会写坏，但体验差）。
- **借法（Phase 4，小改动）**：统一改用 `atomicio`；把 `source_written` 的文案改成“会话源已按方案更新，原始上传未改动”。**不**引入 GCS / 多版本库。

### 2.4 校验 / 合规式检查

- **图腾已有**：`placement.check_plan()` 的 8 种 code（`out_of_region`、`outside_room`、`over_wall`、`overlap_symbol`、`duplicate`、`missing_xy`、`missing_size`、`missing_block`）与 `snap_xy()` 的三种吸附。
- **不做**：另起一套 placement 检查——cad-ai-agent 没有任何几何落地检查可以借用。
- **借法（Phase 5，可选）**：新模块（如 `app/compliance.py`）输出与 `placement._issue()` **同构**的 dict（`code / severity / message / op / index / block / xy`），额外带 `rule_id / measured / required / unit / profile`。这样 `_geometry_hint()`、`build_repair_message()`、前端渲染都能直接复用。阈值走配置（如 `CAD_RULES_PROFILE`），参考 `ComplianceThresholds` 的“可配置档位”，但**阈值要换成中国规范/项目约定**，不能照抄英寸制的 ADA/IBC 数字。

### 2.5 服务端 DWG→DXF 转换（ODA 优先，dwg2dxf 兜底）

- **图腾已有**：`find_dwg2dxf()` / `convert_dwg()`，只认 dwg2dxf。
- **cad-ai-agent 已有**：ODA 优先、预检、失败明确报错；但**没有兜底**，也没有真正的路径发现（`CAD_ODA_PATH` 在它的设置里只是被读进来，`converter.py` 并未使用）。
- **借法（Phase 1，即本计划主体）**：统一成一个可测模块，先 ODA 后 dwg2dxf，结果带 `tool` 字段。详见 §5。

### 2.6 QTO / 工程量（只在不吹的前提下）

- **图腾已有**：索引里已有 `equipment`（按块名统计的设备记录，见 `init_proj.build_first_index()` 里的 `indexing.equipment_from_inserts()`）、`rooms`、`blocks`（`indexing.build_catalog()`）。
- **cad-ai-agent 能提供的**：按块名数数量、按图层量线长。**它没有**图例/比例换算、管径规格、按系统（给水/排水/雨水）分项。
- **结论**：**不要**把“工程量”做成图腾的卖点。如果以后要做，只做“某图幅/某房间内，按块名分组计数 + 按图层合计长度（注明单位是图纸单位，未换算）”，并且明确标注“仅供核对，不能用于报价”。放在 Phase 6 之后，且默认关闭。本计划**不承诺** QTO。

---

## 3. 实施阶段总览（小步、可独立验证）

| 阶段 | 名称 | 主要改动 | 依赖真实图纸 | 依赖 ODA/dwg2dxf | 风险 | 可否独立回滚 |
|---|---|---|---|---|---|---|
| **1** | **DWG→DXF 转换抽象 + README 校正** | 新包 `app/convert/`；`init_proj.convert_dwg()` 变为薄包装；README/.env.example 更正 | 否 | 否（测试 mock/跳过） | 低 | 是（恢复 `convert_dwg` 函数体） |
| 2 | 转换可观测性与上传前预检 | `/api/health` 增加转换器状态；`convert` 结果记入 `init.json`；上传错误文案分流 | 否 | 否 | 低 | 是 |
| 3 | 方案结构校验（`app/planschema.py`） | 归一化 + blockers/warnings/needs_clarification + 保护图层 + 替换不丢数据；接入 `_check_plan_coords` 之前；**已完成（未提交）** | 否（自造 DXF） | 否 | 中 | 是 |
| 4 | 预览/应用结果与版本写入收口 | before/after + destructive；`atomicio`；文案修正 | 否 | 否 | 低 | 是 |
| 5 | 合规式检查骨架（可选） | `app/compliance.py`，先做 1–2 条通用规则 | 部分 | 否 | 中 | 是 |
| 6 | 本地部署补充说明 | macOS 字体候选、**本地**可选 Dockerfile、`deploy/` 不动 | 否 | 否 | 低 | 是 |
| 7 | （不默认开启）工程量原型 | 仅在 Phase 1–5 稳定后再评估 | 是 | 否 | 高 | 不承诺 |

每个阶段都应满足：改动可单独评审、单独回滚；测试是一段能直接跑的 `scripts/test_*.py` 风格脚本（仓库没有 pytest 基础设施，**Phase 1 不要顺手引入 pytest**）。

---

## 4. 关键设计细节

### 4.1 Phase 1 目标结构

```
app/convert/
  __init__.py        公开 API：find_converter() / convert_dwg() / converter_status()
  base.py            ConversionResult、ConvertError、工具通用工具（探测、运行子进程、产物校验）
  dwg2dxf.py         dwg2dxf 实现（从 init_proj.py 迁移，调用参数与判成功方式不变）
  oda.py             ODA File Converter 实现（odafc 优先，CLI 兜底）
```

- 新模块**不 import `config`、不 import `cad_render`**。ODA 的安装路径通过显式参数或环境变量读取，便于在任意目录下单测。
- 保持与图腾现有写法一致：`from __future__ import annotations`、模块 docstring 用中文说明“为什么”、纯函数优先。
- `app/init_proj.py` 只保留兼容入口：

```python
def find_dwg2dxf() -> Optional[str]:
    from .convert import dwg2dxf
    return dwg2dxf.find()          # 旧调用点保持可用

def convert_dwg(src: Path, dest: Path) -> Path:
    from .convert import convert_dwg as _convert
    return Path(_convert(src, dest).output_path)
```

  `find_dwg2dxf` / `convert_dwg` / `_windows_dwg2dxf_candidates` / `_which_where` 在 `app/init_proj.py` 之外**没有其它调用者**（已 grep `app/`、`scripts/` 确认；`app/apply.py` 里也没有同名的本地函数），线上行为由 `_ensure_dxf()` 一个调用点决定，改动面很小。

### 4.2 ODA 的真实行为（依据 `ezdxf==1.4.5` 源码，已在临时环境核实）

`ezdxf.addons.odafc` 的公开函数：`is_installed()`、`readfile()`、`convert()`、`export_dwg()`、`get_win_exec_path()`、`get_unix_exec_path()`、`dwg_version()`。关键事实：

1. **路径发现**：`_get_odafc_path(system)` 在非 Windows 上先看 `unix_exec_path`（来自 `ezdxf.options["odafc-addon"]["unix_exec_path"]`，即 `~/.config/ezdxf/ezdxf.ini`），否则 `shutil.which("ODAFileConverter")`；Windows 上 `shutil.which` 找不到才用 `win_exec_path`（`ezdxf.options["odafc-addon"]["win_exec_path"]`）。`odafc.is_installed()` 用的是**同一套**判定。
2. **所以** `CAD_ODA_PATH` 不能只是“读进变量”。要真正生效，必须二选一：
   - 在调用前写到 `ezdxf.options["odafc-addon"]["win_exec_path"/"unix_exec_path"]`（会改全局 options，需在启动时设一次）；
   - 或者干脆不走 `odafc`，自己按 CLI 直接调用（见 4.3），路径完全自己掌控。
   本计划推荐 **两者都做**：设置 `ezdxf.options` 后调用 `odafc`；`odafc` 失败再按 4.3 的 CLI 方式直接跑一次。
3. **`odafc.readfile(src)` 在内部用 `tempfile.TemporaryDirectory(prefix="odafc_")`**，输出到系统临时目录；Linux 上 `_run_with_no_gui()` 会 **优先起 Xvfb**（有 `Xvfb` 就起，没有就要求 `os.environ["DISPLAY"]` 存在，否则 `KeyError`）。所以 headless 的 Linux 服务器上**必须装 `xvfb`**（或提供一个 `DISPLAY`），否则 ODA 这条路会抛异常。
4. **Linux 上 ODA 进程总是以“崩溃”结束**，`_odafc_failed()` 对此做了特判（只认 stderr 不是空且不是 `"Quit (core dumped)"` 才算失败）。**不能**把返回码当成功判据——以产物文件是否存在、能否被 ezdxf 打开为准。dwg2dxf 也有类似问题（现有注释已写明“可能 exit 0 却打印 WRITE ERROR”）。
5. `odafc.readfile()` 会把源 DWG 的版本作为输出 DXF 版本（`_detect_version()`），所以 ODA 产出的可能是 AC1018 之类的版本，**不会自动变成 AC1021**。`known-indexes/wanshangcheng/INDEX.md` 记录的样本就是“源 DWG 为 AC1018、LibreDWG 0.14 dwg2dxf 写出的也是 AC1018”；换用 ODA 后输出版本可能不同，`known.match_known_drawing()` 按文件哈希匹配不受影响，但会话里 `proj["dxf_version"]` 这类元数据可能不再准确——所以 Phase 1 只在结果与日志里记录实际版本，不去改它。
6. `odafc.readfile()` 的中间产物在**系统临时目录**。DWG 允许传到 80MB（实际样本 2–6MB），若服务器 `/tmp` 是 tmpfs 或分区很小，大图可能出问题；Phase 1 自带的 CLI 路径改用 `CAD_DWG_CONVERT_TMPDIR`，走 `odafc` 的路径保持其内部默认。
7. `odafc.export_dwg()` 存在——**Phase 1 不使用**。DWG 导出涉及授权与坐标系/版本回归，不在本计划承诺范围内。

### 4.3 自行调用 ODA 的 CLI 方式

参考 `odafc._odafc_arguments()` 与 `README.md` 里的官方文档地址：`ODAFileConverter "Input Folder" "Output Folder" version type recurse audit [filter]`。

Phase 1 的自有实现：

1. 把源 DWG 以**原文件名**复制到一个临时输入目录（ODA 按输入目录批量处理，输出文件名 = 输入文件名换后缀）。
2. 运行 `[exe, in_dir, out_dir, "ACAD2018", "DXF", "0", "0", "*.DWG"]`，单次超时默认 `CAD_ODA_TIMEOUT=180` 秒并由总预算封顶（见 §7.7），`capture_output=True`。
3. 在 `out_dir` 中找**大小写都匹配**的 `.dxf`（Windows 文件名大小写不敏感），`ezdxf.readfile()` 能打开且实体数 > 0 才算成功。
4. 输出先写临时文件，再 `atomicio.replace_with_retry()` 落到 `dest`。
5. Linux 上委托给虚拟显示，三种都算「有显示」：`xvfb-run` 存在（CLI 命令加 `xvfb-run -a` 前缀）、`Xvfb` 二进制存在（ezdxf 的 `odafc._linux_dummy_display()` 是**直接起 Xvfb** 的，不走 xvfb-run，所以只 `apt install xvfb` 的服务器不该被跳过）、或 `DISPLAY` 已设置。判定集中在 `oda.virtual_display()` / `has_display()`。**不自造屏幕、不 vendoring Xvfb。**

### 4.4 转换器发现顺序与判定（`find_converter()`）

```
1. ODA：
   a. CAD_ODA_PATH 指定且可执行          → "oda"(explicit)
   b. 平台默认位置（见下）中存在且可执行   → "oda"(discovered)
   c. 不走路径，仅当 odafc.is_installed() → "oda"(odafc)
2. dwg2dxf：
   a. CAD_DWG2DXF 指定且可执行            → "dwg2dxf"(explicit)
   b. 现有 Linux 路径 + shutil.which + Windows 候选 → "dwg2dxf"(discovered)
3. 都没有 → ("none", 空)
```

平台默认位置（只做**存在性检查**，不做递归扫描，避免启动变慢）：

- Linux（服务器）：`/usr/bin/ODAFileConverter`、`/usr/local/bin/ODAFileConverter`、`/opt/cad-tools/ODAFileConverter*`、`/opt/ODAFileConverter*`；以及 Docker/rootfs 场景被显式用 `CAD_ODA_PATH` 指过来的 `lib/.../ODAFileConverter`（**需要用户显式给出**）。
- macOS（本地开发）：`/Applications/ODAFileConverter*.app/Contents/MacOS/ODAFileConverter`。注意 ezdxf 的 `is_installed()` 在 Darwin 上只查 `which`，所以 macOS 上**必须**用 `CAD_ODA_PATH`，这一条要写进 README。
- Windows（本地开发）：`%ProgramFiles%\ODA\ODAFileConverter*\ODAFileConverter.exe`、`%ProgramFiles(x86)%\ODA\...`、以及 `CAD_ODA_PATH`。

环境变量（新增，全部可选，写在 `.env.example`）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `CAD_DWG_CONVERTER` | `auto` / `oda` / `dwg2dxf` / `none`，强制指定或关闭 DWG 转换 | `auto` |
| `CAD_ODA_PATH` | ODA 可执行文件绝对路径（Windows/macOS 基本必填） | 空 |
| `CAD_ODA_TIMEOUT` | 秒 | `180` |
| `CAD_DWG2DXF` | 已存在，保留 | 空 |
| `CAD_DWG2DXF_TIMEOUT` | 秒 | `180` |
| `CAD_DWG_CONVERT_TMPDIR` | 转换中间目录（默认放到目标 `out/` 下的 `.convert-tmp`） | 空 |

### 4.5 转换失败后的用户提示（按原因分流）

| 情况 | 提示（中文，给用户看） |
|---|---|
| 没有任何转换器 | “这台服务器现在不能直接读 DWG。请在 CAD 里另存为 DXF（建议 AC1021）后再上传。”——Phase 1 只在服务端日志里标明“转换器未安装”；Phase 2 才在 `/api/health` 暴露 `kind`（该端点**公开、无需登录**，只能放种类，不能放路径） |
| 有转换器但执行失败 | “DWG 转换失败（%s）。请改传 DXF，或检查文件是否完好。”保留现在 `convert_dwg()` 的句式，错误里带上**工具名** |
| 产物无实体 | “转换出来的 DXF 是空的，原文件可能损坏。” |
| 超时 | “DWG 转换超时（超过 %d 秒），请精简文件或改传 DXF。” |
| ODA 失败但 dwg2dxf 成功 | 不改提示，只记 `warnings`（“ODA 失败，已用 dwg2dxf：……”） |

### 4.6 编码规范化保持现状

现有 `convert_dwg()` 在转换后：
1. `ezdxf.readfile(dest)`
2. `cad_render.sanitize_doc_text_backgrounds(doc)`（修 LibreDWG 写坏的文字底纹）
3. `doc.saveas(norm_tmp, encoding="utf-8")` 再替换

这一步**必须保留**，而且 ODA 的产物也要走一遍（cad-ai-agent 没有这一步）。但为了不让新包依赖 `cad_render`（会把 matplotlib 拖进单测），Phase 1 的 `normalize()` 接受一个**可选的 sanitizer 回调**；`init_proj` 或调用方传入 `cad_render.sanitize_doc_text_backgrounds`。回调用 `importlib` 在函数内部延迟导入也可以，二选一，**测试时传 `None` 即可**。

另外：转换结果的编码（GBK / UTF-8）由 `known.detect_dxf_encoding()` 在索引步骤另行探测，Phase 1 不改变它，只把“工具名 + 输出 DXF 版本 + 字节数”写进日志和结果对象，便于排查“为什么同一条需求在两张样图上表现不同”。

---

## 5. Phase 1 scope（可直接开工的最小范围）

### 5.1 为什么是它（候选排序）

| 候选 | 价值 | 风险 | 可验证性（无样本图、无二进制） | 结论 |
|---|---|---|---|---|
| **A. 转换器抽象** | 高：DWG 是上传里最常失败的一类；线上其他用户上传 DWG 现在的成功率取决于服务器碰巧装了什么 | 低：只动一个调用点，签名不变 | 高：全部逻辑可用 mock 子进程/临时 DXF 验证 | **Phase 1** |
| B. 类型化编辑操作 schema | 中高：减少静默的格式错误 | 中：会触发 `apply_mutations`、`placement`、前端三处联动；归一化规则一旦太严会拦下原本能用的方案 | 高（自造 DXF 即可） | Phase 3 |
| C. 合规检查 | 中：对“审图”场景有吸引力 | 中高：规则阈值要专业输入，错了会误导用户 | 低（无样本图难以判断规则合理性） | Phase 5（可选） |

**实施状态（已落地，未提交）**：`app/convert/{__init__,base,oda,dwg2dxf}.py`、`app/init_proj.py` 的薄包装、
`scripts/test_convert.py`、`README.md`、`.env.example`。与原计划的差异只有两处，都是为了不超时或不动用户目录：

- 新增 `CAD_DWG_CONVERT_BUDGET`（默认 `270` 秒）作为一次请求的总预算；单次工具超时取「剩余预算」与该工具超时的较小值（§7.7）。
- `CAD_DWG_CONVERT_TMPDIR` 是**运维的目录**，里面有别人的文件：dwg2dxf 与 ODA 一样，只在它下面 `mkdtemp` 一个唯一子目录，结束时只删这个子目录；**绝不** rmtree 或清空该目录本身（早期实现曾整目录清空，属于数据丢失，已修复并加了回归测试）。它必须已经存在，打错时报 `bad_tmpdir` 而不是猜路径。默认行为仍是「目标旁的 `.convert-tmp`，用完即删」，与改动前一致。
- ODA 的官方过滤器 `*.DWG` 在 Linux 上大小写敏感，而上传几乎都是小写 `.dwg`；拷进 ODA 输入目录时统一改成大写 `.DWG`（`oda._oda_input_name()`，主文件名只去掉会破坏 glob 的字符）。
- `ezdxf.addons.odafc` 的真实调用链要求 DWG 文件头可识别（`_detect_version()` 会解析 DWG 头）；头不可读时 odafc 直接失败，由 CLI 兜底——这正是保留两段式的原因。
- 自测：`python scripts/test_convert.py`，本机无 ODA / dwg2dxf 时 104 项 PASS、1 项 SKIP，退出码 0（覆盖用户 tmpdir 不被删、ODA 输入拷贝为大写 `.DWG`、Xvfb-only 服务器算有显示）。

### 5.2 改动清单（文件级）

新增：

- `app/convert/__init__.py`：导出 `convert_dwg()`、`find_converter()`、`converter_status()`、`ConvertError`、`ConversionResult`。
- `app/convert/base.py`：`ConversionResult`（dataclass：`ok`、`tool`、`tool_path`、`output_path`、`dxfversion`、`warnings`、`error_code`、`duration_ms`）、`ConvertError`、`_run()`（统一 `subprocess.run` 的编码/超时/错误捕获）、`_is_executable()`、`_looks_like_dxf()`（大小写不敏感后缀 + `ezdxf.readfile` 能开 + 实体数 > 0）。
- `app/convert/dwg2dxf.py`：`find()`、`convert(src, dest)`。**函数体从 `app/init_proj.convert_dwg()` 迁移**（保持 `-y -o` 然后 `-y` 两次尝试、以产物判成功），只是把报错改成带 `tool` 的 `ConvertError`。
- `app/convert/oda.py`：`find()`、`configure_ezdxf_options(path)`、`convert(src, dest)`。先 `odafc`（`configure_ezdxf_options` 之后再 `odafc.is_installed()`），失败再按 §4.3 直接 CLI。
- `scripts/test_convert.py`：自测脚本（见 5.4）。

修改：

- `app/init_proj.py`：
  - `convert_dwg()` 变为薄包装（委托 `app.convert.convert_dwg`），`find_dwg2dxf()` 委托 `app.convert.dwg2dxf.find()`；
  - `_windows_dwg2dxf_candidates()`、`_which_where()` 随实现迁到 `app/convert/dwg2dxf.py`（如需保留旧名，在 `init_proj` 留别名）；
  - `_ensure_dxf()` 不变（仍调用 `convert_dwg(src, dest)`）；
  - 失败时把 `ConversionResult.error_code` 记录到日志（不改变 `init.json` 结构）。
- `README.md`：
  - 第 3 行“v1 只出方案，不写入正式 DXF / DWG”改为实际行为：方案先只做预览；用户确认后写入会话工作图 `.studio/work.dxf` 并同步会话 DXF，可撤回；原始上传文件不会被覆盖；
  - “Windows 本地运行”一节第 3 条改写为：DWG 需要 ODA File Converter（推荐，`CAD_ODA_PATH` 指向 `ODAFileConverter.exe`）或 dwg2dxf（`CAD_DWG2DXF`）；
  - “部署概要”第 5 步补一条 DWG 相关安装说明（服务器装 ODA 或可选 dwg2dxf、headless 需要 `xvfb`）。
- `.env.example`：新增 §4.4 的变量，注释标明“全部可选”。
- 迁走后 `app/init_proj.py` 里两处局部 `import subprocess`（`_which_where()` 与 `convert_dwg()` 内各一处）随之删除；`os`（`find_dwg2dxf()` 用 `os.getenv` / `os.name`）、`shutil`（`_ensure_dxf()`、`_prepare()` 的 `shutil.copy2`）在移走工具函数后可能不再被用到——用 `python -m pyflakes app/init_proj.py` 或直接 grep 确认再删，避免留下未使用的 import。

**明确不做**（Phase 1 边界）：

- 不改 `init.json` 的 JSON 结构、不改前端、不加新 HTTP 接口、不动 `app/main.py`。
- 不改 `placement` / `apply` / `prompts`。
- 不引入 pydantic、pytest、pyproject、Dockerfile、LICENSE。
- 不删 `deploy/` 里任何东西；不改 `deploy/pack.sh` 的 `*.dwg` 过滤（转换中间稿都在目标目录的 `.convert-tmp` 下，且用完即删）。
- 不实现 DWG 导出。

### 5.3 验收标准

1. `python scripts/test_convert.py` 在**没有 ODA、没有 dwg2dxf、没有任何真实图纸**的机器上全部通过（ODA/dwg2dxf 相关用例 mock 或 SKIP，**SKIP 也必须以 0 退出**，与现有 `scripts/test_apply_preview.py` 等脚本的约定一致）。
2. 在已装 dwg2dxf 的机器上（如开发机 `/opt/cad-tools/prefix/bin/dwg2dxf`），用同一份 DWG 分别在改动前、改动后各跑一次初始化，产物 DXF 可用、`out/source.dxf` 落盘、对用户的提示文字与改动前**一致**（工具选择固定为 dwg2dxf 时，结果不应有任何行为差异）。
3. 设置 `CAD_DWG_CONVERTER=none` 时，上传 DWG 的报错文案保持不变。
4. `find_dwg2dxf()` 在 Linux 上仍能找到 `CAD_DWG2DXF` 与 `shutil.which("dwg2dxf")`；在 Windows 上仍能找到 `%LOCALAPPDATA%\Programs\**\dwg2dxf.exe`。
5. `README` 中不再出现“v1 不写入 DXF”的描述，且 `.env.example` 的变量名与 `app/convert` 读取的完全一致。
6. `python scripts/test_preview_sanitize.py`、`python scripts/test_index_sqlite.py` 仍通过（回归）。

### 5.4 测试策略（`scripts/test_convert.py`）

沿用 `scripts/test_placement.py` 的写法：`from __future__ import annotations`、`sys.path.insert`、`_ok(name, cond, detail)`、`main()` 里逐个跑、失败抛 `AssertionError`、`ALL PASSED` 退出 0。

必须覆盖的用例（不依赖真实二进制）：

1. **纯逻辑**：`ConversionResult` 字段齐全；`_looks_like_dxf()` 对 `.DXF` 大写后缀、空文件、非 DXF 字节分别给出正确判断（用 `ezdxf.new("AC1021")` 现造一张图）。
2. **发现顺序（mock `shutil.which` 与 `os.access`）**：`CAD_DWG_CONVERTER=oda` 但没有 ODA → 报 `no_oda`；`CAD_DWG_CONVERTER=dwg2dxf` → 只走 dwg2dxf；`auto` 且两者都在 → 先 ODA。
3. **ODA 的 odafc 路径（mock `odafc.is_installed` / `odafc.readfile`）**：照搬 `tests/unit/test_converter.py` 的 `TestDwgConversionWithOda` 手法；`readfile` 抛异常时**必须**回落到 CLI 或 dwg2dxf，而不是直接失败。
4. **ODA CLI 路径（mock `app.convert.base._run`）**：模拟“返回码非 0 但输出目录里有一个能打开的 DXF” → 判成功（对应 4.2 第 4 点）；模拟“返回码 0 但输出目录为空” → 判失败并尝试 dwg2dxf。
5. **dwg2dxf 路径（mock `_run`）**：`-y -o` 无产物、`-y` 有产物 → 取后者；两者都无产物 → `ConvertError`，且报文包含工具名与最后一行 stderr。
6. **端到端（临时目录，若本机真有任一转换器则跑，否则 SKIP）**：造一个 1KB 的假 `.dwg`，期望失败，但**异常类型与文案**符合 4.5。
7. **兼容性**：`app.init_proj.convert_dwg` 与 `app.convert.convert_dwg` 在同样的 mock 下产出同样的 `dest`。

### 5.5 回滚方式

单个 PR 内即可回滚：`app/convert/` 是新增目录，`init_proj.convert_dwg()` 的旧函数体可在同一 PR 的 `diff` 里原样恢复；README 与 `.env.example` 的改动是纯文档。

---

## 6. 后续阶段细化（每阶段仍保持可独立验证）

### Phase 2：转换可观测性与上传前预检

**状态：已完成（2026-11）。** 改动：`app/convert/__init__.py` 新增 `health_info()` / `invalidate_health_cache()`（30 秒缓存，只返回 `converter`/`how`）；`app/main.py` 的 `health()` 增加 `dwg` 字段；`init_proj.convert_dwg(..., out=)` 用可选出参带出 `ConversionResult`，`_ensure_dxf()` 成功后写 `st["convert"] = {tool, how, dxfversion, warnings}` 并立即 `write_status`，`public_status()` 透出该字段；`create_session_from_upload()` 在 `.dwg` 且 `find_converter().kind == "none"` 时、建目录之前就抛 `ValueError(convert.MSG_NO_TOOL)`。测试集中在 `scripts/test_phase2.py`（TestClient + 临时 `CAD_DATA_DIR`），详见本文末尾「Phase 2 验收」。

- `app/main.py` 的 `health()` 增加 `"dwg": {"converter": "oda" | "dwg2dxf" | "none", "how": "explicit" | "discovered"}`。只暴露工具种类与发现方式。**`/api/health` 是公开端点**（`app/main.py` 的 `require_session_on_api` 中间件放行 `_PUBLIC_API = {"/api/health", "/api/login", "/api/logout"}`），任何未登录的人都能读到，因此**绝不能**把可执行文件路径、版本字符串里的安装目录、或 `converter_status()` 的原始内容放进去。完整路径只进服务端日志，`converter_status()` 返回完整信息仅供日志使用。
- `init_proj` 在 `_ensure_dxf()` 成功后把 `{"tool", "dxfversion", "warnings"}` 写进 `init.json` 的 `save` 步骤 message，前端「准备」面板可直接显示“DWG 已用 ODA 转成 DXF”。
- 上传时若转换器种类为 `"none"`（即 `converter_status()` 返回的 `kind` 字段等于 `"none"`，**不是**用 `"none"` 当字典键去取值）且后缀是 `.dwg`，直接返回 400 并给出现有提示，而不是等到后台初始化才失败。

### Phase 3：方案结构校验 `app/planschema.py`

**状态：已完成（未提交，分支 `feat/windows-support`，基于 `449532f`）。**

- 输入：`maas.extract_assistant_payload()` 的 dict；输出 `PlanResult`：归一化后的 `plan` + `blockers` + `warnings` + `needs_clarification` + `status`（`valid` / `valid_with_warnings` / `blocked` / `needs_clarification`）。issue 复用 `placement._issue()` 的键：`{"code","severity","message","op","index","block","xy"}`，severity 只有 `error`（blocker）/ `warn`（warning）。
- 规则（`app/planschema.py`）：
  - 未知键（方案顶层、条目内）丢弃并 warning（不再拦截）；`xy` 与 `to` 同时给且不一致时按 `xy`，warning。
  - 坐标必须正好 `[x, y]` 两个有限实数：布尔、`NaN`/`Infinity`、字符串、三元组、对象、超过 1e9 都是 blocker；`rot` 不能是字符串/布尔/NaN（可超出 0–360，写入端仍取模）；`scale` 必须为正；`block` 非空；`mode` 只能是 `answer/critique/plan/reject`；`const_width`/`height` 不能为负。
  - 坏条目会从归一化后的数组里剔除并记 blocker；即使调用方不看 blockers，也写不进坏条目。
  - `needs_clarification`：整份方案不是对象、`plan` 模式却没有改动、删除只给块名没给位置（图上有同名块时无法确定删哪一处）。这些情况提示词没有规定会自动补坐标，所以直接问用户而不是回喂模型。
  - **保护图层**：`图框/图签/标题/TITLE/BORDER/SEAL`（不区分大小写的子串匹配）。条目自己指名保护层 → blocker；目标 INSERT 的**实际**图层（含嵌套 insert 的图层继承、layer 0 继承）、块内文字、嵌套插入、以及 **INSERT 自带的 ATTRIB** 落在保护层 → 写入时拦下（父块的删/移/换整块不动）。
  - **layer 0 继承只走 INSERT 链**：0 层实体显示在引用它的 INSERT 的图层上，逐层沿插入链上溯；块记录 / BLOCK 头的图层不参与继承。模型空间里自己画在 **0 层**的 INSERT 是合法的显示图层，照常可删/移/换；只有块缺失、xref、成环、嵌套过深、内容读不了、镜像插入才按「判不清」保护。
  - 合法旧方案原样兼容：值不变、不产生 warning（有回归测试逐字段比对）。
- 接入点：
  - `app/main.py` 的 chat 在**坐标修复循环之前**先归一化；有 blocker 时**不再**回喂模型修坐标，直接把 `needs_clarification` 的中文说明作为 reply，并把消息标成 `mode=clarify`、`plan=None`；
  - 方案写入图面时附带 `plan["schema"]`（status 与 issues）；warning 同时并入 `plan["issues"]` 并在 reply 末尾加一句「只作提醒」。
  - `apply.apply_plan()` / `apply.render_plan_preview()` / `scene.preview_plan_scene()` / `apply.apply_mutations()` **共用** `apply.validate_for_apply()`：结构（planschema）+ 坐标（placement）。`apply_mutations` 自己也会跑结构校验，调用方传 `issues=[]` 也绕不过去；被拦下时抛 `apply.PlanBlockedError`（`ValueError` 子类，带 `result`/`codes`）。`apply_plan` 在**建工作副本/快照之前**先用只读方式校验一遍，保护层/共享块目标不会留下任何文件。
  - 写图前的整条预检（`apply.block_content_issues` → `mutation_blocked`）保留真实的 `protected_layer` / `nested_not_supported` 原因码与中文说明（不再退化成 `not_applied`「这一项没有写入」）。
  - 新块 / 库块一律先在隔离副本里试插并通过 `_guard_new_block`，通过后才往本图导入并插入；守卫失败或插入失败时本图原样（不残留试插实体，也不删掉原有的块定义）。
  - `apply_mutations()` 的替换改为「先插新块，再删/覆盖旧块」：新块插不上时旧块原样保留；旧块没被隐藏成功时把新块撤回。嵌套在共享块里的目标一律不改（`nested_not_supported`），不再用覆盖假装已替换。
  - 歧义：同名块、第二个候选与最近候选差 < 300mm（`apply.AMBIGUITY_MM`）→ `ambiguous_target`，不猜；改文字从「相差 < 1mm 才算歧义」统一为同一阈值，并在「精确」与「全/半角放宽」两种口径指向不同实体时判歧义。
  - `text_edits` 的目标（含 block 继承来的实际图层）在保护层上 → 不改。移动前先校验坐标与旋转角。
- 测试：`scripts/test_phase3.py`（全部自造 DXF，走真实入口：`apply_plan`、`render_plan_preview`、`apply_mutations`、`/chat`、`/apply`）。原计划里的 `scripts/test_planschema.py` 合并进了这个文件，因为一半用例需要 DXF 才能验证实际图层与预览/应用一致。
- **不支持（如实拒绝或报失败，不声称已改）**：
  - 共享图块内部的符号：不改块定义，模型空间也无法可靠盖住共享块图元，一律拒绝（`nested_not_supported`）；摘录里对应的提示是「改法=不支持」，提示词也不再教模型在模型空间覆盖；
  - 图块内部的文字：`text_edits` 只改模型空间 TEXT/MTEXT 与顶层 INSERT 的属性；
  - ATTRIB 的 0 层继承按「所属 INSERT」算，不按 ezdxf `RenderContext.resolve_layer(ATTRIB)` 的口径（后者不解析 ATTRIB 的 FROM/ATTDEF 链）；
  - 不做任何合规/规范规则。
- 与原计划的偏差：保护图层除了看计划里的 `layer` 字段，还按目标实体的实际图层（含嵌套/定义继承）判断，这是本阶段的主要新增；`needs_clarification` 的触发条件收窄到「说不清改哪个」这一类（方案不是对象、plan 模式无改动、删除缺位置），其余结构问题按 blocker 处理并给中文说明。

### Phase 4：预览/应用结果与版本写入收口

- `render_plan_preview()` 返回的 `results` 增加 `before`/`after` 摘要与 `destructive`；`web/app.js` 在确认卡片上按条展示（只加展示，不改交互流程）。
- `apply.save_history()` / `_write_snapshot()` / `_restore_snapshot()` 改用 `atomicio`；对 `revisions.json` 加 `fsync`（小改动，单独验证：断电模拟太难，以单测验证“替换失败时不留半截文件”为准）。
- `_write_session_source()` 的注释与 `status()` 的 `source_written` 文案改为“会话源已按方案更新，原始上传未改动”。

### Phase 5（可选）：合规式检查骨架

- 先只做两条与图腾业务直接相关的规则：① 新增设备与已有同类设备的净距（可复用 `placement.symbol_world_bbox()`）；② 管线与墙线的最小距离。阈值从配置读，默认给“只警告”不“阻断”。
- 输出同构 issue + 扩展字段；前端复用现有 `issues` 渲染。**不**照搬 ADA/IBC。

### Phase 6：本地部署补充（不碰 `deploy/`）

- `app/config.py` 的 `_pick_cjk_font()` 增加 macOS 候选（`/System/Library/Fonts/PingFang.ttc`、`/System/Library/Fonts/Hiragino Sans GB.ttc`），`CAD_CJK_FONT` 覆盖优先不变。
- 可选：在仓库根加**仅本地**用的 `Dockerfile.dev`（不基于 LibreDWG、不装 ODA，仅用于在任意机器上跑 DXF-only 工作台）。**不**替换 `systemd` 部署，**不**改 Nginx 配置。

### Phase 7（不承诺）：工程量原型

只有在 1–5 稳定、并且有真实业务方提出需求时再评估；届时只做“按块名计数 + 按图层长度（图纸单位）”，并在 UI 明示“未换算、不可用于报价”。

---

## 7. 跨平台与 Linux 服务器部署

### 7.1 路径与发现

- 所有可执行文件路径都走“环境变量 → 平台默认位置 → `PATH`”三级，**Phase 1 之后新增的每个工具都必须遵循这个顺序**（ODA、dwg2dxf 已如此；以后若引入 `fc-cache` 之类的辅助程序也一样）。
- 路径一律用 `pathlib.Path`，Windows 上 `os.access(path, os.X_OK)` 对 `.exe` 返回 True，沿用现有写法即可；macOS 的 `.app` bundle 需要显式 `CAD_ODA_PATH`。

### 7.2 字体

- Linux：`fonts-noto-cjk`（README 与 `pack.sh` 已提示），路径 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`。
- Windows：`app/config.py` 已内置 `msyh.ttc` 等候选。
- macOS：目前无默认候选，需 `CAD_CJK_FONT`；Phase 6 补默认值。
- 切记：用户上传的 DWG 若用了系统没有的字体，ezdxf 渲染会用替代字形——这是**渲染**问题，不是转换问题，不应在转换器里“修”。

### 7.3 文件锁与多进程

- `filelock.acquire()` 只保护 `archive-queue/worker.lock`；`project_write_lock()` 是进程内锁。`deploy/cad-studio.service` 固定 `--workers 1`，这是**正确的前提**，任何阶段的改动都不得打破它（例如“换成 gunicorn 多 worker”不在计划内）。
- Phase 1 的 ODA/dwg2dxf 子进程没有共享状态，不涉及锁；但转换输出必须走临时文件 + `atomicio.replace_with_retry()`，避免 Windows 上另一个请求正在读 `out/source.dxf` 时替换失败。

### 7.4 临时目录

- ezdxf 的 `odafc` 用 `tempfile.TemporaryDirectory()`（受 `TMPDIR` / `TEMP` 影响）。服务器上 `/tmp` 若挂 `noexec` 不影响（只写文件），但**磁盘小**；建议在部署说明里写：`CAD_DWG_CONVERT_TMPDIR` 指向数据盘。Phase 1 实现自有 CLI 调用时使用该变量。

### 7.5 Docker / systemd

- 现状：只有 `deploy/cad-studio.service`（systemd）+ Nginx + `pack.sh`。**Phase 1 不改部署文件。**
- 若以后要容器化（**不在本计划范围内**）：ODA File Converter 的 Linux 版本需要 GUI 依赖，官方建议用 ODA 提供的 AppImage 并配 `xvfb`；LibreDWG 是 **GPLv3**，其 `dwg2dxf` 若放进镜像会带来许可证传染风险。因此容器方案里**默认只装 ODA**，dwg2dxf 只在宿主机可选安装、以 `CAD_DWG2DXF` 指入，**不在镜像里 vendoring**。
- 没有 ODA 的镜像里，`CAD_DWG_CONVERTER=none` 可以让工作台明确地只接受 DXF。

### 7.6 ODA 安装注意事项

- 仅服务器管理员安装；安装后把路径写进 `/opt/cad-studio/.env` 的 `CAD_ODA_PATH`，权限 600。
- Linux 服务需 `xvfb`（`apt install xvfb`），否则 `odafc` 在无 `DISPLAY` 环境下抛 `KeyError`。
- 首次安装后执行 README 里的预检：`python -c "from ezdxf.addons import odafc; print(odafc.is_installed())"`（Phase 2 会把它变成 `/api/health` 字段）。
- ODA 的许可条款由其发行方定义，本项目不打包、不 vendoring。

### 7.7 公开服务的安全边界（现状，不在 Phase 1 改）

- 单账号（`ACCESS_USERNAME` / `ACCESS_PASSWORD`），所有会话对同一账号可见——**当前设计如此**。
- 上传 80MB、Nginx `client_max_body_size 80m` 两边一致。
- 超时的对照关系要看清：`deploy/nginx-cad.visitworld.me.conf`（TLS 版 `nginx-cad.visitworld.me.tls.conf.example` 相同）里 `location /api/` 是 `proxy_read_timeout 300s`，`location /` 是 `180s`。上传接口走 `/api/`，所以 180s 的转换超时落在 **300s 的 API 超时之内**（不是“小于 180s”）。Phase 1 把转换超时设为 ≤ 180s（`CAD_ODA_TIMEOUT` / `CAD_DWG2DXF_TIMEOUT`，默认各 `180`），单个转换器失败后最多再换一个工具重试一次：最坏情况「odafc 超时 + CLI 超时 + dwg2dxf 两轮超时」会接近或超过 300s。因此 Phase 1 的实现以**总预算**（`CAD_DWG_CONVERT_BUDGET`，默认 `270` 秒）封顶：每次子进程超时取「剩余预算」与「该工具超时」的较小值，预算耗尽即放弃后续工具，保证请求在 Nginx 300s 之内返回。
- `app/config.py` 顶部读取 `/home/box/agent-data/secrets/*.env` 是开发机个人路径，公开部署时应移除或改为可选路径——列为 Phase 6 的清理项，**不在 Phase 1**。

---

## 8. 署名与许可证（若后续阶段复制代码）

- cad-ai-agent 主许可证为 Apache-2.0（`/workspace/ref/cad-ai-agent/LICENSE`）。其仓库**根目录没有 `NOTICE`**，同名文件位于 `.audit-harness/NOTICE` 且属于另一款产品，因此**不能**在计划或代码里写死其版权人。
- **本计划只借鉴设计思路，不复制代码。** 若某阶段决定复制（例如照搬 `odafc` 的测试 mock 手法、或 `_odafc_arguments()` 的参数顺序），必须：
  1. 在复制处加注释：`# 改编自 cad-ai-agent <相对路径>（Apache-2.0）`；
  2. 在仓库根建 `NOTICE`（**不是 LICENSE**），写明“本产品包含改编自 cad-ai-agent 的代码，Apache License 2.0，原版权人以其仓库 LICENSE 署名信息为准”，并原样保留 Apache-2.0 第 4(d) 条要求的声明；
  3. 不复制其 `LICENSE` 文本作为本仓库的许可——本仓库是否有 LICENSE 由所有者决定（本计划不改）。
- ezdxf（MIT）已是图腾依赖，`odafc` 的调用不需额外署名，但注释里注明“行为依据 ezdxf 1.4.5 源码”有助于以后升级时排查。
- LibreDWG（GPLv3）：只在**可选**的宿主机安装、通过子进程调用，不在仓库或镜像中 vendoring。

---

## 9. 必须尊重的已知约束（本计划不修复）

1. **分支状态**：`feat/windows-support` 比 `main` 领先 **6** 个提交（`e1a89b5`、`00d312e`、`b5b6d4c`、`2989f41`、`de294b3`、`d3b4962`），落后 **1** 个提交——`main` 的 `a1630f9`「识别图面文字，并修复对话摘录与图幅下拉」。`git diff HEAD...FETCH_HEAD --stat` 显示它改了 `app/indexing.py`（+756）、`app/init_proj.py`、`app/maas.py`、`app/projects.py`、`app/prompts.py`、`web/index.html`、`web/styles.css` 等。**不 merge main。**

   核对过实际 diff（`git diff d3b4962 FETCH_HEAD -- app/init_proj.py`），结论与“可能相邻”的猜测不同：

   - `a1630f9` 在 `app/init_proj.py` 里**只加 4 行**，全部在 `build_first_index()` 内部：`indexing.locate_texts(texts, sheets)`、`indexing.attach_labels_nearby(rooms, texts)`、`indexing.assign_dining_furniture(equipment, rooms)`，以及索引字典里新增的 `"texts": texts` 键。这一区域与 Phase 1 改的 `_windows_dwg2dxf_candidates()` / `_which_where()` / `find_dwg2dxf()` / `convert_dwg()` / `_ensure_dxf()` **完全不重叠**，Phase 1 制造冲突的风险很低。
   - 分支与 main 在 `convert_dwg()` 上的差异**不来自** `a1630f9`，而来自本分支的 `b5b6d4c`「修复 DWG 转换调用与产物保护」（以及 `2989f41` 引入的 `atomicio`）。`b5b6d4c` 把 main 的写法换成了更安全的版本：`-y -o` 传的是**输出文件**而不是目录、以“产物文件存在且非空”判成功、规范化时先 `saveas` 到 scratch 文件再替换 dest。
   - **Phase 1 必须保留本分支的更安全行为，不得退回 main 的 `doc.saveas(str(dest))`**——对正在写的目标文件直接 `saveas`，一旦失败就会把 dest 截断/写坏。main 的 GBK 回退读取（`ezdxf.readfile(dest, encoding="gbk")`）只能作为**以后合并 main 时的考量**，本阶段不合并；若将来加 GBK 重试，也必须经临时文件、成功后再替换 dest。
   - `a1630f9` 对 `app/prompts.py` 的改动会影响 Phase 3 的接入点，届时需要知道它已存在于 main。
2. **README 与实际不一致**：README 说“v1 不写正式 DXF/DWG”，而 `apply.apply_plan()` 写 `.studio/work.dxf` 并写回会话源 DXF，`undo_last()` 会恢复。Phase 1 只改**这句话**使其准确，不扩大承诺。
3. **单登录账号、单进程 `filelock`**：不设计多用户、不改锁模型。
4. **没有 LICENSE 文件**，也不要新增；这是所有者的决定。
5. **Phase 1 不设计多用户系统，也不做 Docker 重写。**

---

## 10. 开放问题（需要所有者决定，计划里不预设答案）

1. 服务器是否允许安装 ODA File Converter？若不允许，Phase 1 的收益主要在“dwg2dxf 的发现/兜底/错误分流”。
2. 转换后的 DXF 是否统一成 AC1021 / UTF-8？现状是“版本跟着转换器走、只把编码统一成 UTF-8”，两张样本图的版本分别是 AC1018 与 AC1015。统一版本不改变 `known.match_known_drawing()`（它按**上传文件**的 sha256 匹配，与转换稿无关），但会改变图面表现（如实体被降级/升级），**需要样本图与授权后才做**。
3. 是否要做 DWG 导出（`odafc.export_dwg()`）？目前**不在任何阶段**。
4. Phase 3 的“保护图层”名单以中文图层名为准（`图框`、`图签`、`标题`…），需要拿真实图纸确认命名习惯。

---

## 11. 路径自检

本文件引用到的图腾路径（写文件后逐条 `test -e` 验证）：

`README.md`、`DESIGN.md`、`.env.example`、`requirements.txt`、`app/config.py`、`app/main.py`、`app/init_proj.py`、`app/apply.py`、`app/placement.py`、`app/prompts.py`、`app/maas.py`、`app/projects.py`、`app/atomicio.py`、`app/filelock.py`、`app/archive_queue.py`、`app/library.py`、`app/cad_render.py`、`app/known.py`、`deploy/cad-studio.service`、`deploy/nginx-cad.visitworld.me.conf`、`deploy/pack.sh`、`deploy/rsync-exclude`、`scripts/test_placement.py`、`scripts/test_preview_sanitize.py`、`scripts/test_index_sqlite.py`、`scripts/test_apply_preview.py`、`scripts/run-dev.ps1`、`scripts/setup-venv.ps1`、`known-indexes/wanshangcheng/INDEX.md`、`known-indexes/xueshengshitang/INDEX.md`。

已创建的路径：`app/convert/__init__.py`、`app/convert/base.py`、`app/convert/dwg2dxf.py`、`app/convert/oda.py`、`scripts/test_convert.py`（Phase 1）；`app/planschema.py`、`scripts/test_phase3.py`（Phase 3，原计划的 `scripts/test_planschema.py` 并入后者）。

本文件引用到的 cad-ai-agent 路径：

`LICENSE`、`README.md`、`Dockerfile`、`pyproject.toml`、`src/cad_dxf_agent/settings.py`、`src/cad_dxf_agent/core/converter.py`、`src/cad_dxf_agent/core/plan_validator.py`、`src/cad_dxf_agent/core/plan_converter.py`、`src/cad_dxf_agent/core/apply_pipeline.py`、`src/cad_dxf_agent/core/edit_history.py`、`src/cad_dxf_agent/core/preview_builder.py`、`src/cad_dxf_agent/core/compliance_rules.py`、`src/cad_dxf_agent/core/takeoff_engine.py`、`src/cad_dxf_agent/core/session_store.py`、`src/cad_dxf_agent/core/revision_notes.py`、`src/cad_dxf_agent/models/ops_schema.py`、`src/cad_dxf_agent/models/plan_schema.py`、`src/cad_dxf_agent/models/preview_schema.py`、`src/cad_dxf_agent/models/compliance_schema.py`、`src/cad_dxf_agent/models/takeoff_schema.py`、`tests/unit/test_converter.py`。

### Phase 2 验收

`scripts/test_phase2.py`（纯自造，不依赖真实 DWG/ODA）：

- `/api/health` 带 `dwg.converter` / `dwg.how`，原有键都在；payload 里没有 `/`、`\`，也不出现转换器路径与 `converter_status` 才有的字段；同一 30 秒窗口内只查找一次（缓存生效）。
- 转换成功后 `init.json` 的 `convert` 记录 `tool/how/dxfversion/warnings`，没有路径，公开状态透出；`convert_dwg()` 签名与返回值不变，老文案（`MSG_NO_TOOL` / `MSG_FAILED % …`）不变。
- 无转换器时 `.dwg` 上传：`ValueError(MSG_NO_TOOL)`、不建 `sessions/` 目录、不写 `sessions.json`；HTTP 走 `/api/projects/upload` 返回 400 与同一句中文。
- 无转换器时 `.dxf` 上传照常建会话、写 source、启动初始化、`init.json` 不带 `convert`。
