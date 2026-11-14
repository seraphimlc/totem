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
