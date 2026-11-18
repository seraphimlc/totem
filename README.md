# 图腾 · 改图工作台

用自然语言提出改图方案，在当前图纸的局部预览上叠笔迹。

方案本身只做预览、不落盘；用户点「确认」后才写入会话的工作图 `.studio/work.dxf`，并同步更新该会话的 DXF（`out/source.dxf`，用于下载与预览），可以用「撤回」逐步恢复。**上传的原始文件始终保留在 `source/` 下，不会被覆盖。**

线上计划地址：https://cad.visitworld.me

## 本地运行

```bash
cd /workspace/cad-nl/studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# 填写 MAAS_KEY、ACCESS_USERNAME、ACCESS_PASSWORD、SESSION_SECRET，权限改为 600
chmod 600 .env
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8780
```

也可执行 `scripts/run-dev.sh`。浏览器打开 http://127.0.0.1:8780 ，用 `.env` 里的用户名和密码进入。

本地开发会按下面的目录发现项目（不会移动它们）：

- 学生食堂 → `/workspace/cad-nl/projects/学生食堂`，交付图 `out/学生食堂-1F装饰平面-卫生间已改.dxf`
- 万商城3号楼给排水 → `/workspace/cad-nl/demo-t53`，交付图 `out/万商城3号楼-给排水施工图-演示改图.dxf`

对话写在各项目自己的 `chat.json` 里，不写进 `INDEX.md` / `index.json`。

## Windows 本地运行

在 Windows 11 + Python 3.12 上（PowerShell，仓库根目录）：

1. 建虚拟环境并装依赖（会自动从 `.env.example` 复制一份 `.env`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-venv.ps1

# 或手动：
C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

2. 编辑 `.env`，至少填写 `ACCESS_USERNAME`、`ACCESS_PASSWORD`、`SESSION_SECRET`（缺任意一项时登录不可用，`/api/health` 仍可访问）。不要把 `.env` 提交进 git。

3. 启动：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-dev.ps1
# 或
scripts\run-dev.bat
```

浏览器打开 http://127.0.0.1:8780 。脚本默认绑定 `127.0.0.1:8780`，可用 `HOST` / `PORT` 环境变量覆盖。

Windows 说明：

- 中文字体自动选 `C:\Windows\Fonts\msyh.ttc` 等系统字体；也可用 `CAD_CJK_FONT` / `CAD_CJK_FONT_BOLD` 指定字体文件。Linux 仍用 Noto CJK。
- 队列锁在 Windows 上走 `msvcrt.locking`（见 `app/filelock.py`），语义与 Linux 的 `flock` 一致，仍然只支持单进程 worker。
- DWG 需要转换器，服务器会先找 ODA File Converter，再找 dwg2dxf（LibreDWG）。本机装了 ODA 时用 `CAD_ODA_PATH` 指向 `ODAFileConverter.exe`（Windows 上一般必填，ezdxf 不会自己读这个变量）；装了 dwg2dxf 时用 `CAD_DWG2DXF` 指向可执行文件。两者都没有时只能上传 DXF，上传 DWG 会提示先在 AutoCAD 里另存为 DXF。详见下文「DWG 转 DXF」。
- 组件库 / 配置查找支持环境变量覆盖：`CAD_LIBRARY_INDEX`（索引 JSON）、`CAD_LIBRARY_DXF_DIR`（图块 DXF 目录），之后才回落到 Linux 生产路径和项目内 `var/`。
- 自造 DXF 的冒烟测试不依赖真实图纸：`.venv\Scripts\python.exe scripts\test_preview_sanitize.py`、`.venv\Scripts\python.exe scripts\test_index_sqlite.py`、`.venv\Scripts\python.exe scripts\test_convert.py`（DWG 转换层，没装 ODA / dwg2dxf 也能跑）。依赖真实样本图的 `scripts\test_apply_preview.py`、`scripts\test_scene.py`、`scripts\session_tests.py` 在没有样本时会打印 `SKIP` 并以 0 退出，可用 `TUTENG_DXF_FIXTURE`、`TUTENG_CANTEEN_DXF`、`TUTENG_MALL_DXF`、`TUTENG_ROOT` 指向样本。

## 环境变量

见 `.env.example`。不要把 `.env` 或 `.access_password` 提交进 git。

| 变量 | 作用 |
|---|---|
| `MAAS_BASE` | OpenAI 兼容接口，默认 `https://api.xytoken.xyb2b.com/v1` |
| `MAAS_KEY` | 只放在服务器上，前端看不到，也没有填写密钥的界面 |
| `ACCESS_USERNAME` / `ACCESS_PASSWORD` | v1 共用一个登录账号 |
| `SESSION_SECRET` | 签发登录 Cookie |
| `SESSION_SECURE` | 上 HTTPS 后设为 `1` |
| `HOST` / `PORT` | 本地可绑定 `0.0.0.0:8780`；生产由 systemd 绑 `127.0.0.1:8780` |
| `CAD_DATA_DIR` | 生产上项目目录的父路径（下面仍用「学生食堂」「万商城3号楼给排水」这两个子目录名） |
| `CAD_PROJECT_XUESHENGSHITANG` / `CAD_PROJECT_WANSHANGCHENG` | 可选，绝对路径覆盖，不必移动原目录 |
| `CAD_DWG_CONVERTER` | 可选，`auto`（默认）/ `oda` / `dwg2dxf` / `none`，决定 DWG 用哪个转换器 |
| `CAD_ODA_PATH` | 可选，ODA File Converter 可执行文件绝对路径 |
| `CAD_ODA_TIMEOUT` | 可选，单次 ODA 转换超时（秒），默认 `180` |
| `CAD_DWG2DXF` | 可选，dwg2dxf 可执行文件绝对路径 |
| `CAD_DWG2DXF_TIMEOUT` | 可选，单次 dwg2dxf 超时（秒），默认 `180` |
| `CAD_DWG_CONVERT_TMPDIR` | 可选，DWG 转换中间目录；默认是输出文件旁的 `.convert-tmp` |
| `CAD_DWG_CONVERT_BUDGET` | 可选，一次上传里所有转换器合计的时间上限（秒），默认 `270` |

模型只有 `Tifa-6.1-sol`（默认）和 `Tifa-6-astra`。Sol 若拒绝图片会自动改成纯文字再请求。Astra 会带上当前局部预览。

叠图标签依赖本机字体 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`（Debian / Ubuntu 包装 `fonts-noto-cjk`）。预览渲染用 ezdxf + matplotlib。

## DWG 转 DXF

上传 `.dwg` 时，服务器按 `CAD_DWG_CONVERTER` 决定工具链（默认 `auto`）：

1. **ODA File Converter**（推荐）。先用 ezdxf 的 `odafc` 封装，失败再按 ODA 的 CLI 约定直接调用；两段都只看输出目录里有没有能打开的 DXF，不看返回码。
2. **dwg2dxf**（LibreDWG，GPLv3）。没装 ODA、或 `auto` 下 ODA 失败时兜底。

两条路都失败、或一个都没装时，上传 DWG 会提示先在 AutoCAD 里另存为 DXF（建议 AC1021）。设 `CAD_DWG_CONVERTER=none` 可以彻底关闭 DWG 转换。转换结果会统一规范成 UTF-8；原始上传文件不会被覆盖。

- **上传前先看有没有转换器**：一个转换器都没有时，`.dwg` 上传会**立刻**返回 400 和上面那句中文，不会建会话目录、不落盘；DXF 上传不受影响。
- **状态里能看见用了哪个转换器**：转换成功后，会话的 `init.json`（以及 `/api/projects/{id}/init`）多一个可选字段
  `convert: {"tool": "oda"|"dwg2dxf", "how": "cli"|"odafc"|"explicit"|"discovered", "dxfversion": "AC1032", "warnings": [...]}`，
  只记种类与版本，**不含任何路径**；前端不依赖它，没有这个字段时照常显示。
- **公开健康检查**：`GET /api/health`（无需登录，只读）在原有字段外多一个
  `dwg: {"converter": "oda"|"dwg2dxf"|"none", "how": "explicit"|"discovered"|""}`，只暴露种类和发现方式，
  不含路径、工具清单或环境变量；结果缓存 30 秒，只看文件是否可执行、不起子进程。
  运维自查：`curl -s http://127.0.0.1:8780/api/health | python -m json.tool`。

服务器安装说明（Debian / Ubuntu）：

- ODA：从 Open Design Alliance 下载免费版 ODA File Converter 自行安装，然后在 `/opt/cad-studio/.env` 里写 `CAD_ODA_PATH=/opt/.../ODAFileConverter`。它是带界面的程序，无桌面的服务器需要 `apt install xvfb`（`odafc` 会自己起 Xvfb；没有 Xvfb 时要求存在 `DISPLAY`），否则 ODA 这条路会被跳过。
- dwg2dxf：`apt install libredwg`（或自行编译后放到 `/opt/cad-tools/prefix/bin/dwg2dxf`），再用 `CAD_DWG2DXF` 指向它。LibreDWG 是 GPLv3，本仓库与 `deploy/` 都不打包它，只按路径调用。
- 自检：`python -c "from ezdxf.addons import odafc; print(odafc.is_installed())"`，以及 `.venv/bin/python scripts/test_convert.py`（不需要真实图纸）。
- 转换超时受 `CAD_DWG_CONVERT_BUDGET`（默认 270 秒）封顶，落在 Nginx 对 `/api/` 的 `proxy_read_timeout 300s` 之内。

## 改文字（text_edits）

「把『女卫』改成『女卫（无障碍）』」这类要求不是删旧建新，而是**原地改**那一条 TEXT/MTEXT：

- 按可见文字**逐字**匹配 `from_text`（标点算数，子串不算）；只改当前分区里的那一处，多处又分不清就让模型补位置。
- 字高、图层、样式、旋转、宽度因子、对齐点、颜色都不动；结果里的 `handle` 还是原来那条。
- 写入后再按同一口径复核（只规整空白、标点逐字），对不上就报失败、不落笔。
- TEXT 存不了换行，换行写成单个空格；MTEXT 换行写成 `\P`，空行（`\P\P`）保留。
- MTEXT 如果原来是**多段逐字排版**（每段不同颜色/字体，如 `\A1;{\C1;红}\C2;蓝；绿`），
  改字后**拍平成一段**：保留开头的段落码（如 `\A1;`）和第一个字体，正文只按段落换行，
  不再逐段上色。要保留逐段颜色请改完在 CAD 里手动重排。

## 方案校验与保护图层（Phase 3）

模型给出的改图方案在**动图之前**先过一遍结构校验（`app/planschema.py`）。预览、确认应用、
只能看见当前图的图面预览（`/scene/preview`）和直接调用 `apply.apply_mutations()` 都走同一套检查，
调用方传什么参数都绕不过去：

- **阻断项（blocker，不写图）**：坐标缺失、不是正好两个有限数字（布尔、`NaN`/`Infinity`、
  字符串、`[1]`/`[1,2,3]`）、`rot` 传字符串或布尔、`scale` 非正、块名为空、文字没内容、
  管线缺端点、改文字缺原文字或坐标、方案数组不是数组、`mode` 不在 `answer/critique/plan/reject`。
  这些都不会以「写入时失败」的面目出现，也不会先删旧图块再发现新块插不上。
- **需要澄清（needs_clarification，直接问用户）**：整份方案不是对象、`plan` 模式却没有任何改动、
  删除只给了块名没给位置（图上同名块常有几处）。chat 里遇到这些**不再回喂模型修坐标**，直接
  用中文说明缺什么；这类方案不会生成预览，也不能确认应用。
- **仅提示（warning，照常预览/应用）**：方案里多余的字段（如模型自创的 `colour`）会被丢掉，
  并在回复末尾加一句「…只作提醒，不影响这次改图」；`xy` 与 `to` 同时给且不一致时按 `xy` 处理。
- **保护图层**：图层名包含 `图框`、`图签`、`标题`、`TITLE`、`BORDER`、`SEAL`（不区分大小写，
  所以「A1图框」「title-block」也算）的视为保护图层。按**实际图层**判断：条目自己指名保护层、
  目标 INSERT 的显示图层在保护层、块里任何内容（嵌套插入、块内文字、**INSERT 自己的 ATTRIB**）
  落在保护层，都会被拦下并带中文原因；删/移/换这种父块会把整块（包括那些属性）一起带走，所以
  整块都不动。改文字落到保护层上同样不改。图块定义永远不改。
- **layer 0 继承只走 INSERT 链**：实体自己画在 0 层时，显示在**引用它的那个 INSERT** 的图层上，
  一层层沿插入链往上找（嵌套插入的 0 层跟它所在块被插入的那层走；ATTRIB 跟它所属的 INSERT 走）。
  块记录 / BLOCK 头上写的「图层」不参与继承，也不参与判断。**普通 0 层插入照常可改**：模型空间里
  自己画在 0 层的 INSERT，0 层就是它的显示图层，删除 / 移动 / 替换都按 0 层写入，不会被当成「判不清」；
  真正判不清的只有块缺失、外部参照、引用成环或嵌套过深、内容读不了、镜像插入，这些一律按保护处理。
- **歧义**：同一分区里同名块有好几处、模型给的点分不出是哪一处（与最近、次近两处距离差
  < 300mm）时拒绝，不按距离猜；改文字时若「逐字精确」与「全/半角放宽」两种口径指向不同实体
  （相距 < 300mm）也视为歧义。
- **替换不丢数据**：替换图块先插新块，成功后删/覆盖旧块；新块插不上（库里和图里都没有、坐标越界）
  时旧块原样保留，结果里写明「原图块保留」。

已知不支持（**会被拒绝或如实报失败，不会声称已改**）：

- 嵌套在共享底图块里的符号：内部约定不改块定义，模型空间也无法可靠盖住共享块里的图元，
  一律不改，返回 `nested_not_supported`：「…在共享图块「…」内部，这个位置不支持修改，这次没有动它」。
- 图块**内部**的文字（不在模型空间、也不是顶层 insert 的属性）：`text_edits` 只改模型空间里的
  TEXT/MTEXT 和顶层 INSERT 的属性。
- ATTRIB 的 0 层继承按「所属 INSERT」算，不按 ezdxf `RenderContext.resolve_layer(ATTRIB)` 的口径
  （后者不解析 ATTRIB 的 FROM/ATTDEF 链，会返回原样 0）。
- 未做任何合规/规范检查，也不按图层名做其它业务规则。

`app/apply.py` 里连带的小修正：删除顶层 INSERT 走 `msp.delete_entity(entity)`（ezdxf 1.4.5 的公开
签名只收实体），删不掉时再用 `destroy()` 兜底并如实报失败；移动前先校验目标坐标与旋转角，失败时
那一处不动。新块 / 库块一律先在隔离副本里试插、过保护检查，通过后才导入本图，失败时本图原样。

## 部署概要

不要在本仓库里改生产机。建议步骤：

1. 在 DNS 把 `cad.visitworld.me` 的 A 记录指到生产机。
2. 在本机运行 `deploy/pack.sh`，得到不含密钥、不含源 DWG / PDF / 图库的精简目录。
3. 将精简目录同步到 `/opt/cad-studio/current/`。项目数据可放在 `current/data/`，或单独放在 `/opt/cad-studio/data/` 并设置 `CAD_DATA_DIR`。
4. 写入 `/opt/cad-studio/.env`，权限 `600`，至少包含 `MAAS_*`、`ACCESS_*`、`SESSION_SECRET`、`CAD_DATA_DIR`。生产绑定已写在 unit 里：`127.0.0.1:8780`。上 HTTPS 后设 `SESSION_SECURE=1`。
5. 在 `/opt/cad-studio/current` 建立 `.venv` 并 `pip install -r requirements.txt`。系统安装 `fonts-noto-cjk`。需要直接接收 DWG 时，再按「DWG 转 DXF」一节装 ODA File Converter（配 `xvfb`）或 dwg2dxf；都不装就只能上传 DXF。
6. 安装 `deploy/cad-studio.service`（默认用户 `cad`，需先建该用户）。先放 `deploy/nginx-cad.visitworld.me.conf`（只开 80，ACME 目录 `/opt/visitworld-deploy/acme`）。证书签发后再换成 `nginx-cad.visitworld.me.tls.conf.example`。
7. 启动服务并 `nginx -t && reload`。

生产磁盘较紧，打包脚本不会带上 `source.dwg`、组件库、转换中间稿、PDF、zip 或大量旧预览图。

## 设计

界面说明见 `DESIGN.md`。
