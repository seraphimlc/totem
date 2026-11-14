# 图腾 · 改图工作台

用自然语言提出改图方案，在当前图纸的局部预览上叠笔迹。v1 只出方案，不写入正式 DXF / DWG。

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
- 没有 `dwg2dxf`（LibreDWG / ODA 转换器）时只能上传 DXF。若本机装了 dwg2dxf，用 `CAD_DWG2DXF` 指向可执行文件；否则上传 DWG 会提示先在 AutoCAD 里另存为 DXF。
- 组件库 / 配置查找支持环境变量覆盖：`CAD_LIBRARY_INDEX`（索引 JSON）、`CAD_LIBRARY_DXF_DIR`（图块 DXF 目录），之后才回落到 Linux 生产路径和项目内 `var/`。
- 自造 DXF 的冒烟测试不依赖真实图纸：`.venv\Scripts\python.exe scripts\test_preview_sanitize.py`、`.venv\Scripts\python.exe scripts\test_index_sqlite.py`。依赖真实样本图的 `scripts\test_apply_preview.py`、`scripts\test_scene.py`、`scripts\session_tests.py` 在没有样本时会打印 `SKIP` 并以 0 退出，可用 `TUTENG_DXF_FIXTURE`、`TUTENG_CANTEEN_DXF`、`TUTENG_MALL_DXF`、`TUTENG_ROOT` 指向样本。

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

模型只有 `Tifa-6.1-sol`（默认）和 `Tifa-6-astra`。Sol 若拒绝图片会自动改成纯文字再请求。Astra 会带上当前局部预览。

叠图标签依赖本机字体 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`（Debian / Ubuntu 包装 `fonts-noto-cjk`）。预览渲染用 ezdxf + matplotlib。

## 部署概要

不要在本仓库里改生产机。建议步骤：

1. 在 DNS 把 `cad.visitworld.me` 的 A 记录指到生产机。
2. 在本机运行 `deploy/pack.sh`，得到不含密钥、不含源 DWG / PDF / 图库的精简目录。
3. 将精简目录同步到 `/opt/cad-studio/current/`。项目数据可放在 `current/data/`，或单独放在 `/opt/cad-studio/data/` 并设置 `CAD_DATA_DIR`。
4. 写入 `/opt/cad-studio/.env`，权限 `600`，至少包含 `MAAS_*`、`ACCESS_*`、`SESSION_SECRET`、`CAD_DATA_DIR`。生产绑定已写在 unit 里：`127.0.0.1:8780`。上 HTTPS 后设 `SESSION_SECURE=1`。
5. 在 `/opt/cad-studio/current` 建立 `.venv` 并 `pip install -r requirements.txt`。系统安装 `fonts-noto-cjk`。
6. 安装 `deploy/cad-studio.service`（默认用户 `cad`，需先建该用户）。先放 `deploy/nginx-cad.visitworld.me.conf`（只开 80，ACME 目录 `/opt/visitworld-deploy/acme`）。证书签发后再换成 `nginx-cad.visitworld.me.tls.conf.example`。
7. 启动服务并 `nginx -t && reload`。

生产磁盘较紧，打包脚本不会带上 `source.dwg`、组件库、转换中间稿、PDF、zip 或大量旧预览图。

## 设计

界面说明见 `DESIGN.md`。
