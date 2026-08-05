# 飞书妙记自动化

监听飞书妙记生成事件，自动下载音视频与转写；本地生成配图纪要；支持管理端浏览/下载/分享，以及访客分享页（密钥访问、导出）。可选接入 **Cloudflare Tunnel（公网入口）** 与 **R2（分享媒体加速）**。

## 架构

```
前端 (7355 静态页)
  ↓ HTTP / SSE
API (FastAPI · 7354)
  ↓
Service（下载 / 纪要 / 分享 / R2）
  ↓
Repository (SQLite) + 本地磁盘 + 可选 Cloudflare R2
```

- **管理端**（`/`、`meeting.html`）：始终读本地媒体与配图  
- **分享页**（`share.html`）：鉴权后图/压缩视频走 R2 签名 URL（未启用 R2 时回退本机）  
- 前端 `apiBase` 固定为当前站点 `/api/v1`；本地由 `serve.py` 反代到 7354，公网由 Tunnel 路径分流

## 当前能力

- 飞书 SDK **WebSocket 长连接**收事件（无需公网 Webhook）
- Web 管理端：云端列表、本地下载、纪要生成进度、密钥管理、批量分享、分享管理（换密钥 / 改导出权限 / 取消分享）
- 前端图标：[Remix Icon](https://remixicon.com/)（Apache-2.0），字体本地托管于 `frontend/assets/vendor/remixicon/`，不依赖外网 CDN
- 自动下载音视频 + 转写；下载后可自动生成配图纪要（需本机 `ffmpeg`/`ffprobe`）
- 分享：公开 / 需密钥、可导出控制；访客可记住密钥；「我的分享」页汇总本机密钥可访问的全部链接
- 可选 Cloudflare：Named Tunnel 暴露前后端；R2 托管分享用配图与压缩视频

## 本地目录（每个妙记）

```
data/meetings/{minute_token}/
├── meta.json
├── raw/
│   ├── media/              # 原片音视频
│   └── transcript/
├── agent/                  # 抽帧中间产物等
└── output/
    ├── assets/             # 纪要配图（本地）
    ├── summary.md
    ├── summary.meta.json
    └── r2.meta.json        # R2 同步状态（启用 R2 后生成）
```

## 快速开始（仅本机）

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 复制并填写 .env（至少 FEISHU_*、ADMIN_TOKEN、LLM_*）
copy .env.example .env

# 终端 1：后端
python -m app.main

# 终端 2：前端
python frontend/serve.py
```

- 前端：<http://127.0.0.1:7355>  
- 后端：<http://127.0.0.1:7354>  
- 管理端登录口令：`.env` 里的 `ADMIN_TOKEN`

本机调试时保持：

```env
FRONTEND_ORIGIN=http://127.0.0.1:7355
API_PUBLIC_BASE=http://127.0.0.1:7355
R2_ENABLED=false
```

---

## 接入 Cloudflare（重点）

公网访客访问管理端/分享页需要 **Tunnel**；分享页媒体要快，再加 **R2**。两者可分开做：可以只开 Tunnel，也可以 Tunnel + R2。

### 总览

| 组件 | 作用 | 本项目用法 |
|------|------|------------|
| Named Tunnel | 把本机 7355/7354 暴露为 HTTPS 域名 | **推荐单域名**：`/api*`→7354，其余→7355 |
| R2 | 对象存储，出网免费额度友好 | **仅分享页**：配图双写 + 压缩视频；管理端仍读本地 |

示例域名（请换成你的）：

- 同域（推荐）：`https://larkmeeting.example.com`  
  - `/api*` → `http://127.0.0.1:7354`  
  - 其余 → `http://127.0.0.1:7355`  
- 本地开发：`python frontend/serve.py` 已内置把 `/api/*` 反代到 7354，前端 `apiBase` 固定为当前 origin。

示例 ingress 见 [`cloudflared.ingress.example.yml`](cloudflared.ingress.example.yml)。

---

### A. Cloudflare Tunnel（公网入口）

#### 1. 安装 cloudflared

Windows 可用 winget：

```powershell
winget install --id Cloudflare.cloudflared -e
```

可执行文件常见路径：`C:\Program Files (x86)\cloudflared\cloudflared.exe`

#### 2. 控制台创建 Named Tunnel（推荐）

1. 打开 [Zero Trust → Networks → Tunnels](https://one.dash.cloudflare.com/)  
2. **Create a tunnel** → Cloudflared → 命名（如 `feishu-minutes`）  
3. 复制安装命令中的 token，在本机执行（管理员权限）：

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" service install "<TOKEN>"
Start-Service Cloudflared
```

4. 在该隧道下添加 **Public Hostname**（**同一 hostname 两条，带 path 的靠前**）：

| Public hostname | Path | Service |
|-----------------|------|---------|
| `larkmeeting.example.com` | `/api*` | `http://127.0.0.1:7354` |
| `larkmeeting.example.com` | （空） | `http://127.0.0.1:7355` |

DNS 会由 Cloudflare 自动落到隧道（橙色云代理）。可删掉旧的 `larkmeeting-api.*` 子域。

#### 3. 改项目 `.env`

```env
FRONTEND_ORIGIN=https://larkmeeting.example.com
API_PUBLIC_BASE=https://larkmeeting.example.com
```

前端 [`config.js`](frontend/assets/config.js) 已使用 `${location.origin}/api/v1`，一般无需再改。

重启后端使 CORS / 分享媒体绝对 URL 生效。

#### 4. 飞书 OAuth 回调（若公网也要登录飞书）

开发者后台重定向 URL 请**同时保留本地与公网**（系统按当前访问 URL 动态选用）：

```
http://127.0.0.1:7355/api/v1/auth/feishu/callback
https://larkmeeting.example.com/api/v1/auth/feishu/callback
```

并写入 `.env` 的 `FEISHU_OAUTH_REDIRECT_URIS`。

#### 5. 体感说明

- Tunnel 路径：浏览器 → CF 边缘 → 回源到你这台机器，国内访问可能偏慢（页面/API/管理端原片）。  
- **分享页图与已就绪的压缩视频**走 R2 后会快很多。  
- 本机管理请继续用 `http://127.0.0.1:7355`（经前端反代打本机 API，无跨域）。

---

### B. Cloudflare R2（分享媒体）

#### 1. 控制台准备

1. Dashboard → **R2 Object Storage** → 启用  
2. **Create bucket**（如 `larkmeeting-share`），保持**私有**（不要开公开读）  
3. **Manage R2 API Tokens** → 创建 Token  
   - 权限：该 bucket 的 Object Read & Write  
   - 记下：`Account ID`、`Access Key ID`、`Secret Access Key`

无需给 bucket 绑自定义域名；分享页使用**短时签名 URL**。

#### 2. 填写 `.env`

```env
R2_ENABLED=true
R2_ACCOUNT_ID=你的AccountID
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=larkmeeting-share
R2_SIGNED_URL_TTL_SECONDS=7200
R2_VIDEO_CRF=32
R2_VIDEO_MAX_HEIGHT=720
R2_VIDEO_PRESET=veryfast
R2_VIDEO_COMPRESS_TIMEOUT_SECONDS=21600
```

重启后端。`R2_ENABLED=true` 且密钥齐全后才会上传/签名。

#### 3. 行为说明

| 时机 | 行为 |
|------|------|
| 纪要配图落盘后 | 双写到 R2 `meetings/{token}/assets/...` |
| 下载完成后 | 后台 ffmpeg 压成 720p 分享片 → 上传 `meetings/{token}/share/video.mp4` |
| 分享页鉴权通过后 | 配图 302 / 视频直链 → R2 签名 URL |
| 管理端 meeting 页 | **始终本地** `/meetings/local/.../media` |

压缩策略默认：高度 ≤720、`libx264` + `veryfast` + CRF 32、AAC 64k、`+faststart`（利于边下边播）。状态写在各会议的 `output/r2.meta.json`。

#### 4. 历史数据补传

对已有本地会议批量同步配图与压缩视频：

```bash
# 项目根目录
set PYTHONPATH=.
set PYTHONUNBUFFERED=1
.\.venv\Scripts\python.exe -u scripts\backfill_r2_media.py

# 仅某一场
.\.venv\Scripts\python.exe -u scripts\backfill_r2_media.py --token <minute_token>
```

若某场已有试压产物 `agent/r2-compress-test/share.mp4`，脚本会优先直接上传，避免长视频再压一遍。

#### 5. 免费额度提示

R2 每月约有 **10GB 存储** 与读写操作免费额度（以 Cloudflare 官网为准）。压缩后体积远小于原片，但仍建议只给分享链路用，原片留在本地。

---

### C. 配置检查清单

1. Tunnel 服务 `Cloudflared` 为 Running；同域下 `/api*`→7354、其余→7355（path 规则靠前）  
2. `.env` 的 `FRONTEND_ORIGIN` / `API_PUBLIC_BASE` 都为同一公网域名  
3. 需要分享加速时：`R2_ENABLED=true` 且 Token/Bucket 正确，重启后端  
4. 分享页视频若 403：确认没有把 `share_session` 拼到 R2 签名 URL 上（当前前端已处理）  
5. 本机管理用 `127.0.0.1:7355`，确认 Network 里 API 走同域 `/api/v1`（经反代）

---

## 飞书开发者后台

### 所需权限

| 权限 | 用途 |
|------|------|
| `minutes:minutes.basic:read` | 妙记基本信息 + 订阅生成事件 |
| `minutes:minutes.search:read` | 搜索云端列表 |
| `minutes:minutes.media:export` | 下载音视频 |
| `minutes:minutes.transcript:export` | 导出转写 |
| `offline_access` | 刷新用户授权 |

### 用户 OAuth

搜索、订阅、下载走 `user_access_token`。登录链接为相对路径 `/api/v1/auth/feishu/login`，**回调地址按当前访问的主机动态选择**（本地打开用本地回调，公网打开用公网回调）。  

请在飞书开放平台**同时登记**本地与公网回调，并写入 `.env` 的 `FEISHU_OAUTH_REDIRECT_URIS`（逗号分隔），例如：

```
http://127.0.0.1:7355/api/v1/auth/feishu/callback
http://127.0.0.1:7354/api/v1/auth/feishu/callback
https://larkmeeting.example.com/api/v1/auth/feishu/callback
```

### 事件订阅

1. 先启动本服务，日志出现长连接成功  
2. 开发者后台 → 事件配置 → **长连接接收事件**  
3. 订阅 `minutes.minute.generated_v1`  
4. 用户 OAuth 登录后会自动调用「订阅妙记变更」

## 常用脚本

| 脚本 | 说明 |
|------|------|
| `scripts/backfill_r2_media.py` | 补传本地会议配图/压缩视频到 R2 |
| `python -m app.main` | 后端 |
| `python frontend/serve.py` | 前端静态服务（含 `/api` → 7354 反代） |

## 相关文档

- [处理事件 - Python SDK](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)  
- [订阅妙记变更](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute/subscription)  
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)  
- [Cloudflare R2](https://developers.cloudflare.com/r2/)
