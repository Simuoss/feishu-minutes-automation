# 飞书妙记自动化

监听飞书妙记生成事件，按订阅用户自动下载音视频与转写，本地生成配图纪要（含敏感信息脱敏）；提供多用户管理端与访客分享页（密钥访问、导出、段落问答）。飞书免费版转写只有前几分钟，这种情况下会自动改走自建转写，并用声纹把说话人对到全局人物库。不在飞书上的录音、录屏、纪要文档也能**本地导入**成会议，走同一条流水线。可选接入 **Cloudflare Tunnel（公网入口）** 与 **R2（媒体加速）**。

许可证：[GNU General Public License v3.0](LICENSE)（GPL-3.0）。

本文只讲怎么部署和配置。系统怎么运转、某处设计为什么成了现在这样，见 [`docs/`](docs/README.md)：

| 文档 | 讲什么 |
|------|--------|
| [架构与数据流](docs/architecture.md) | 分层、一场妙记走完的路、作业队列、本地目录、元数据的单一写入者 |
| [检索助手](docs/agent.md) | 跨会议自然语言检索、虚拟目录、三个工具、配额与深链 |
| [自建转写与声纹](docs/transcription-and-voiceprint.md) | 覆盖率判定、声纹认人、阈值来历、分段与逐句复核 |
| [从飞书姓名自动建声纹](docs/voiceprint-harvest.md) | 拿飞书段头的真名提炼声纹，只落提案等超管核对 |
| [手动切换转写引擎](docs/transcript-engine-switch.md) | 在飞书转写与自建 ASR 之间来回切换并重跑 |
| [本地导入](docs/local-import.md) | 音视频与文档当成会议导入，走同一条流水线 |
| [媒体存储与 R2](docs/media-storage.md) | 哪些东西上云、音轨从哪来、不开 R2 会怎样 |

## 能做什么

- 飞书 SDK **WebSocket 长连接**收 `minutes.minute.generated_v1`（无需公网 Webhook）
- **多用户**：账号密码 / 飞书 SSO 登录；邀请码建号；侧栏手势解锁超级管理员
- 管理端：云端列表、本地下载与纪要进度、密钥/分享管理、用户与**系统配置**（超管）
- 事件下载：**仅对事件订阅者映射到的本地用户**落盘，避免无关账号重复下载
- 自动下载音视频 + 转写；可自动生成配图纪要（需本机 `ffmpeg`/`ffprobe`）
- 飞书转写被截断时自动改走**自建转写**，并用**声纹**把说话人对到全局人物库
- **手动切换转写引擎**：详情页可在飞书转写与自建 ASR 之间来回切换并重跑纪要
- **本地导入**：音视频或 txt/md/srt/docx/pdf 文档当成会议导入，走同一条纪要流水线
- **从飞书姓名自动建声纹**：沿用飞书转写的会议顺手提炼声纹，结果进待确认队列等超管核对
- 成文前由模型判定录音是**讲课**还是**会议**，两套纪要模板与配图侧重各不相同；判不出就中止，不猜模板
- 配图敏感扫描 → 马赛克 → 复核；分享页/详情支持访问票读图
- 分享：公开 / 需密钥、可导出；访客可记住密钥；侧栏「可访问课程」按**妙记生成时间**排序
- 段落问答：基于转写/纪要上下文的访客提问（受分享权限约束）
- **检索助手**：跨会议的自然语言检索，模型自己翻转写与纪要，命中段落连着跳转链接推回对话（管理端与分享页各有入口，公开访客按日限额）
- 可选 Cloudflare：Named Tunnel；R2 托管配图与压缩分享视频，并承接本地导入的浏览器直传

## 快速开始（仅本机）

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 复制并填写 .env（至少 FEISHU_*、JWT_SECRET、ADMIN_TOKEN、LLM_*）
copy .env.example .env

# 终端 1：后端
python -m app.main

# 终端 2：前端
python frontend/serve.py
```

- 前端：<http://127.0.0.1:7355>
- 后端：<http://127.0.0.1:7354>
- 登录：注册/邀请码建号，或飞书 SSO；超管口令为 `.env` 的 `ADMIN_TOKEN`（侧栏手势解锁，不是普通登录密码）

本机调试建议：

```env
FRONTEND_ORIGIN=http://127.0.0.1:7355
API_PUBLIC_BASE=http://127.0.0.1:7355
R2_ENABLED=false
```

`.env` 中密钥类项常驻；下载并发、纪要/脱敏/R2 画质等业务阈值首次启动写入 `system_configs`，之后以超管「系统配置」页为准（详见 `.env.example` 分区注释）。

## 模型分工

六个环节各调一次模型，负载差得很远，所以每个环节都有自己的一组 `BASE_URL` / `API_KEY` / `MODEL`，加上兜底那组共**七组**：

| 组 | 前缀 | 这一步在干什么 |
|----|------|----------------|
| 兜底默认 | `LLM_` | 下面哪项留空就用这儿的，本身不直接对应某个环节 |
| 纪要成文 | `LLM_SUMMARY_` | 整篇转写进去、成篇 markdown 出来，长上下文长输出，全站最贵的一步 |
| 挑图 | `LLM_FIGURE_` | 一次几十张候选帧进去、回一小段 JSON，必须能看图 |
| 配图脱敏 | `LLM_REDACT_` | 扫敏感信息再复核马赛克，同样必须能看图 |
| 场景判定 | `LLM_SCENE_` | 判讲课还是会议，一次短分类，用快模型省掉主模型的排队 |
| 划词答疑 | `LLM_ASK_` | 短问快答，可能带用户贴的图 |
| 检索助手 | `AGENT_` | 跨会议检索，会用工具且多轮往返 |

回落是**逐项**的，哪项留空就取兜底那组对应的那项——包括模型名：一组三样全留空，这个环节就整个走兜底。所以只想给某个环节换模型就只填它的 `MODEL`，地址和密钥继续用兜底那组；整个环节换供应商就三样一起填。

`BASE_URL` 填到域名或网关前缀为止，**不要带 `/v1`**——客户端固定往后接 `/v1/messages`，多写一层会拼成 `/v1/v1/messages`，服务端只回一个 404 `Invalid URL`。

三样里只填 `MODEL` 是正常用法，其余的半拉组合都是把两家的东西拼在一起，启动日志会单独告警：只填地址不填密钥（拿兜底那家的密钥打新地址）、只填密钥不填地址、填了地址却把模型名留空（拿兜底那家的模型名去请求新供应商，对方多半不认），以及地址末尾带了 `/v1`。这几种运行时只表现为 401 或"模型不存在"，不容易查。

七组都只在 `.env` 里配，改完要重启后端。启动时日志里能看到实际生效的分工：

```text
模型分工 兜底默认：water18 @ api.stepfun.com/step_plan（密钥 4hmu…z4）← 回落兜底
模型分工 纪要成文：water18 @ api.stepfun.com/step_plan（密钥 4hmu…z4）
模型分工 场景判定：step-3.7-flash @ api.stepfun.com/step_plan（密钥 4hmu…z4）
```

## 启用自建转写与声纹

飞书免费版只转写前几分钟，这条链路负责把剩下的补齐并认出说话人。先拉声纹嵌入模型：

```bash
# 几十兆，不进仓库；国内默认走 HuggingFace 镜像
python scripts/download_speaker_model.py
```

`.env` 里对应这几项，密钥留空会复用 `LLM_API_KEY`：

```env
STEP_ASR_ENABLED=true
STEP_ASR_API_KEY=
TRANSCRIPT_COVERAGE_THRESHOLD=0.9
SPEAKER_MATCH_THRESHOLD=0.55
SPEAKER_EMBEDDING_MODEL_PATH=models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
```

除密钥和模型路径外，覆盖率与声纹阈值都属于业务阈值，首次启动写入 `system_configs`，之后改超管「系统配置」页即可，不必重启。

这条链路依赖 R2（音频要有公网直链给识别服务）与本机 `ffmpeg`；关掉 `STEP_ASR_ENABLED` 就退回只用飞书转写。设计与取舍见 [自建转写与声纹](docs/transcription-and-voiceprint.md)，「拿飞书真名建声纹」那部分见 [声纹提炼](docs/voiceprint-harvest.md)（开关是超管「系统配置」里的 `VOICEPRINT_HARVEST_ENABLED`）。

## 接入 Cloudflare

公网访问需要 **Tunnel**；分享/播放媒体要快再加 **R2**。可只开 Tunnel，也可 Tunnel + R2。

| 组件 | 作用 | 本项目用法 |
|------|------|------------|
| Named Tunnel | 把本机 7355/7354 暴露为 HTTPS | **推荐单域名**：`/api*`→7354，其余→7355 |
| R2 | 对象存储 | 配图 + 压缩分享视频 + 本地导入直传；私有桶 + 短时签名 URL |

示例域名：

- 同域（推荐）：`https://larkmeeting.example.com`  
  - `/api*` → `http://127.0.0.1:7354`  
  - 其余 → `http://127.0.0.1:7355`  
- 本地：`python frontend/serve.py` 已反代 `/api/*` → 7354

示例 ingress 见 [`cloudflared.ingress.example.yml`](cloudflared.ingress.example.yml)。开了 R2 的话，桶还要配 CORS，见下面 [R2 桶 CORS](#3-桶-cors浏览器要直接连-r2)。

### A. Cloudflare Tunnel（公网入口）

#### 1. 安装 cloudflared

```powershell
winget install --id Cloudflare.cloudflared -e
```

常见路径：`C:\Program Files (x86)\cloudflared\cloudflared.exe`

#### 2. 控制台创建 Named Tunnel

1. 打开 [Zero Trust → Networks → Tunnels](https://one.dash.cloudflare.com/)
2. **Create a tunnel** → Cloudflared → 命名（如 `feishu-minutes`）
3. 复制安装命令中的 token，管理员权限执行：

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" service install "<TOKEN>"
Start-Service Cloudflared
```

4. 添加 **Public Hostname**（同一 hostname 两条，**带 path 的靠前**）：

| Public hostname | Path | Service |
|-----------------|------|---------|
| `larkmeeting.example.com` | `/api*` | `http://127.0.0.1:7354` |
| `larkmeeting.example.com` | （空） | `http://127.0.0.1:7355` |

#### 3. 改项目 `.env`

```env
FRONTEND_ORIGIN=https://larkmeeting.example.com
API_PUBLIC_BASE=https://larkmeeting.example.com
```

前端 [`config.js`](frontend/assets/config.js) 使用 `${location.origin}/api/v1`，一般无需再改。重启后端使 CORS / 绝对 URL 生效。

#### 4. 飞书 OAuth / SSO 回调

开发者后台重定向 URL 请**同时保留本地与公网**，并写入 `FEISHU_OAUTH_REDIRECT_URIS`：

```
http://127.0.0.1:7355/api/v1/auth/feishu/callback
https://larkmeeting.example.com/api/v1/auth/feishu/callback
```

#### 5. 体感说明

- Tunnel：浏览器 → CF 边缘 → 回源本机，国内页面/API 可能偏慢。
- 配图与已就绪的压缩视频走 R2 后会快很多。
- 本机管理继续用 `http://127.0.0.1:7355`。

### B. Cloudflare R2（媒体）

#### 1. 控制台准备

1. Dashboard → **R2 Object Storage** → 启用  
2. **Create bucket**（如 `larkmeeting-share`），保持**私有**  
3. **Manage R2 API Tokens** → Object Read & Write；记下 Account ID / Access Key / Secret  

#### 2. 填写 `.env`

```env
R2_ENABLED=true
R2_ACCOUNT_ID=你的AccountID
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=larkmeeting-share
```

画质与签名 TTL（`R2_SIGNED_URL_TTL_SECONDS`、`R2_VIDEO_*`）可在 `.env` 种子或「系统配置」中调整。`R2_ENABLED=true` 且密钥齐全后才会上传/签名。

哪些对象会上云、不开 R2 会退化成什么样，见 [媒体存储与 R2](docs/media-storage.md)。

#### 3. 桶 CORS（浏览器要直接连 R2）

配图和分享片是浏览器拿签名 URL 直接从 R2 读的，本地导入的大文件又是浏览器直接 `PUT` 上去的，这两件事都要桶放开 CORS，否则被浏览器挡在预检那一步。

服务启动时会自己试一次：读桶上现有规则，够用就跳过，不够用才写。但 R2 API Token 默认没有读写 CORS 的权限，这时程序只在日志里说一句「沿用控制台手配规则」就过去了——所以通常还是得去控制台手配一次。

Dashboard → R2 → 选桶 → **Settings** → **CORS Policy** → Edit，把这段按自己的域名改完粘进去（[`scripts/r2_browser_cors.example.json`](scripts/r2_browser_cors.example.json) 是同一份，可直接复制）：

```json
[
  {
    "AllowedOrigins": [
      "https://larkmeeting.example.com",
      "http://127.0.0.1:7355",
      "http://localhost:7355"
    ],
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
    "MaxAgeSeconds": 3600
  }
]
```

几处别改错：

- **`AllowedOrigins` 填页面的域名，不是 R2 的域名。** 也就是 `.env` 里的 `FRONTEND_ORIGIN`；本机调试的 `127.0.0.1:7355` 和 `localhost:7355` 是两个不同的 Origin，都要写上，端口也算在内。
- **`PUT` 是本地导入直传要用的。** 只放 `GET`/`HEAD` 的话，看图和播放正常，但导入大文件时预检会被挡；这种情况下一个字节都还没发出去，前端会静默改走服务器上传（走 Tunnel，几百兆会慢），功能不至于坏掉。
- **`ExposeHeaders` 是「允许页面上的 JS 读到这几个响应头」。** 跨域响应里其余的头浏览器会藏起来，即使真的返回了也读不到。当前是整文件一次 `PUT`，前端不读 `ETag`，传完只告诉后端一声、由后端去 R2 把对象拉回来；照抄留着是因为 S3 直传惯例如此，且将来要做分片上传就必须有——每片的 `ETag` 得回传给合并请求，缺了就传不完。
- `MaxAgeSeconds` 是预检结果的缓存时间，一小时够了；调小只是多几次 `OPTIONS` 往返。

改完不用重启后端，浏览器侧刷新页面即可（旧的预检结果可能还在缓存里，等一会儿或换个无痕窗口）。想确认有没有生效，启动日志里会有一行：覆盖到了写「R2 桶 CORS 已覆盖前端 Origin」，只放开了读会明确警告 PUT 没放开。

#### 4. 历史数据补传 / 清本地

```bash
set PYTHONPATH=.
.\.venv\Scripts\python.exe -u scripts\backfill_r2_media.py
.\.venv\Scripts\python.exe -u scripts\backfill_r2_media.py --token <minute_token>

# R2 已同步后清理本地媒体（慎用）
.\.venv\Scripts\python.exe scripts\purge_local_media_after_r2.py
```

#### 5. 免费额度提示

R2 每月约有 **10GB 存储** 与读写操作免费额度（以官网为准）。压缩后体积远小于原片，建议只长期存分享链路所需对象。

### C. 配置检查清单

1. Tunnel 服务 Running；同域 `/api*`→7354、其余→7355  
2. `FRONTEND_ORIGIN` / `API_PUBLIC_BASE` 为同一公网域名  
3. 需要媒体加速：`R2_ENABLED=true`、密钥齐全、重启后端；桶 CORS 按 [上面那节](#3-桶-cors浏览器要直接连-r2) 配好，`AllowedOrigins` 覆盖前端域名、方法里有 `PUT`、暴露 `ETag`  
4. 分享页配图 401：确认已拿到访问票再渲染纪要（当前前端会先取票）  
5. 本机管理用 `127.0.0.1:7355`，Network 里 API 走同域 `/api/v1`

## 飞书开发者后台

### 所需权限

开发者后台 → 开发配置 → **权限管理**，用页面上的 JSON 批量导入把这份粘进去（也可以照着名字逐个勾）：

```json
{
  "scopes": {
    "tenant": [
      "wiki:wiki"
    ],
    "user": [
      "minutes:minutes",
      "minutes:minutes.basic:read",
      "minutes:minutes.media:export",
      "minutes:minutes.search:read",
      "minutes:minutes.transcript:export",
      "minutes:minutes:readonly",
      "offline_access",
      "vc:meeting.meetingevent:read",
      "vc:recording:read"
    ]
  }
}
```

改完记得**发布版本**，权限才对线上生效。其中当前代码真正用到的是这几项：

| 权限 | 用途 |
|------|------|
| `minutes:minutes.basic:read` | 妙记基本信息 + 订阅生成事件 |
| `minutes:minutes.search:read` | 搜索云端列表 |
| `minutes:minutes.media:export` | 下载音视频 |
| `minutes:minutes.transcript:export` | 导出转写 |
| `offline_access` | 刷新用户授权，免得每次都要重新登录 |

剩下几项是留着的：`minutes:minutes` 与 `minutes:minutes:readonly` 是妙记的粗粒度父权限，后台勾细项时常一并开上；`vc:recording:read`、`vc:meeting.meetingevent:read` 对应会议录制那条入口（`FeishuVcClient` 已经封好了取录制文件的接口，但当前流程不走它，`vc.recording.*` 事件也是明确忽略的）；`wiki:wiki` 是租户级，代码目前不碰知识库接口。开着不影响，将来要用不必再回后台改一遍。

发起授权时实际申请哪些，由 `.env` 的 `FEISHU_OAUTH_SCOPES` 决定（默认就是上表那五项，空格分隔）。这里写了但后台没开通的，登录后前端会顶出「缺少权限」横幅，点横幅上的链接去后台补；反过来后台开了但这里没写的，就只是不申请而已。租户级权限（`wiki:wiki`）不属于用户授权，别往这个变量里塞。

### 用户 OAuth / SSO

搜索、订阅、下载走 `user_access_token`。登录与回调按当前访问主机动态选择。请在开放平台**同时登记**本地与公网回调，并写入 `FEISHU_OAUTH_REDIRECT_URIS`，例如：

```
http://127.0.0.1:7355/api/v1/auth/feishu/callback
http://127.0.0.1:7354/api/v1/auth/feishu/callback
https://larkmeeting.example.com/api/v1/auth/feishu/callback
```

`FEISHU_SSO_REQUIRE_INVITE=true` 时，飞书建号需带邀请参数。

### 事件订阅

1. 先启动本服务，日志出现长连接成功  
2. 开发者后台 → 事件配置 → **长连接接收事件**  
3. 订阅 `minutes.minute.generated_v1`  
4. 用户 OAuth 登录后会自动调用「订阅妙记变更」  
5. 事件到达后，仅为映射到本地账号的订阅者触发下载

## 常用脚本

| 脚本 | 说明 |
|------|------|
| `scripts/download_speaker_model.py` | 拉声纹嵌入模型（启用自建转写前跑一次） |
| `scripts/backfill_r2_media.py` | 补传配图/压缩视频到 R2 |
| `scripts/purge_local_media_after_r2.py` | R2 已同步后清理本地媒体 |
| `scripts/migrate_filesystem_meta_to_db.py` | 旧版目录 JSON 元数据迁入 SQLite |
| `scripts/migrate_storage_to_owner_paths.py` | 存储路径迁到 `{owner}/{token}` |
| `scripts/migrate_aux_tables_owner_keys.py` | 附属表补齐 owner 维度 |
| `scripts/cleanup_filesystem_meta.py` | 清理已迁库的残留 meta 文件 |
| `scripts/_e2e_replay_minute.py` | 联调：清理并重放指定妙记下载（`--token`/`--owner` 必填） |
| `python -m app.main` | 后端 |
| `python frontend/serve.py` | 前端（含 `/api` → 7354 反代） |

## 外部参考

- [处理事件 - Python SDK](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)  
- [订阅妙记变更](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/minutes-v1/minute/subscription)  
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)  
- [Cloudflare R2](https://developers.cloudflare.com/r2/)
