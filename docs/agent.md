# 检索助手

跨会议的自然语言检索。用户问「上次讲环境变量是在哪节课」，助手自己去翻转写和纪要，找到就把原文连着跳转链接推回对话里，点一下跳到那一段并高亮。

管理端入口在左侧栏第一项（`/agent.html`），分享页在侧栏最上方（`/share-agent.html`）。

## 为什么是 Agent 而不是一次 RAG 检索

课程语料的问法很杂：有时是找一个词，有时是「哪节课讲过 X 和 Y」，有时得先粗查再往上下文里翻几十行才能确认。这类多步骤的活儿写死在一次检索里做不好，交给会用工具的模型自己决定查几轮更合适。语料规模也支持这么干——现在 41 场课转写合计约 2 MB，每次全量扫盘都是毫秒级，不需要向量库。

跑在 [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/python) 上，模型走阶跃的 Anthropic 兼容端点（`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`）。SDK 是纯 Python 包，自带原生二进制，不需要 Node。

## 虚拟目录

助手看不到磁盘。它眼里只有这样一棵树：

```
/2026-07-25 1332/司沐的项目周会/转写.md
/2026-07-25 1332/司沐的项目周会/纪要.md
/2026-08-01 0900/包管理专场/转写.md
```

- 目录名是「妙记生成时间（到分钟）+ 会议名」。时间取 `create_time`，缺了回落 `downloaded_at`；会议名里的 `/` 换成 `_`；同一分钟撞上同名会议就加 ` (2)`。
- 只收录盘上真有正文的文件：没生成纪要的会议就只有 `转写.md`。
- 建树发生在每一轮问答开始时，范围由身份决定（见下）。反向解析只认树里存在的路径，其它一律返回工具错误——`../../etc/passwd` 这类路径连翻译都不会发生。
- 转写正文过 `apply_speaker_names`（贴真名），纪要过 `apply_speaker_names_to_summary`。同一轮里三个工具共用同一份归一化文本，所以 search 报的行号 read/send 拿得准。

代码在 [app/service/agent/virtual_fs.py](../app/service/agent/virtual_fs.py)。

## 三个工具

| 工具 | 输入 | 干什么 |
|------|------|--------|
| `search` | `patterns[]`、`logic`、`mode`、`paths?`、`max_results?` | 检索，返回虚拟路径、会议名、行号、跳转链接、上下各三行 |
| `read` | `path`、`start_line`、`line_count`、`limit` | 读一段，返回带 `123\| ` 行号前缀的正文，外加上下各还剩多少行多少字 |
| `send` | `path`、`start_line`、`line_count`、`note?` | 把这一段连同链接推给用户，在对话里显示成卡片 |

几个定死的语义：

- `mode=keyword` 是大小写不敏感子串，`mode=regex` 走 `re.search`。
- `logic=and` 是**文件级**：文件里每个 pattern 都出现过才算命中，返回各自的命中行。要同行 AND 让模型自己写正则。
- `read` 的返回带行号前缀。不带的话模型调 `send` 时只能靠数行，容易错位。
- `limit` 是字符上限，撞上了会截断并告诉模型「继续读请把 start_line 设成 N」。

代码在 [app/service/agent/tools.py](../app/service/agent/tools.py)。工具是按会话现造的闭包，直接抓住这一轮的虚拟目录，所以不存在拿错范围的可能。

## 检索范围

| 身份 | 范围 |
|------|------|
| 登录用户 | 自己名下全部会议 |
| 超级管理员 | 全站 |
| 密钥访客 | 本机保存的全部可用密钥能访问的分享 |
| 公开访客 | 本机记住过的公开分享 |

访客那两条挂在 `/api/v1/share/agent/...`（该前缀免 JWT），鉴权靠请求体里的 `keys[]` + `share_tokens[]` 过一遍 `resolve_library`：算出来是空集就 403，算出来的集合同时就是这次的检索范围。这跟分享页侧栏「可访问课程」是同一套逻辑，不会多给也不会少给。

## 跳转与高亮

卡片里的链接是站内相对路径：

- 转写：`/meeting.html?token=...&tab=transcript&seg=12&segs=3`。后端把行区间折算成段序号，折算规则跟前端 `parseTranscript` 逐字对齐（同一个 `SPEAKER_LINE_RE`，同样不排序），所以 `seg` 就是 `.transcript-segment[data-index]`。
- 纪要：`/share.html?s=...&tab=summary&q=<引文前 60 字>`。markdown 渲染后没有行号，只能带引文让前端在 DOM 里找；找不到就只切 tab。
- 访客侧换成 `/share.html?s=<share_token>`，超管侧多带 `owner_user_id`。

前端落地在 [frontend/assets/deep-link.js](../frontend/assets/deep-link.js)，`meeting.js` 与 `share.js` 在决定初始 tab 的地方让它先接管。

## 配额

阈值在超管「系统配置」里，0 表示不限：

| 键 | 默认 | 作用于 |
|----|------|--------|
| `AGENT_DAILY_QUOTA_ANON` | 20 | 公开访客 |
| `AGENT_DAILY_QUOTA_KEY` | 0 | 密钥访客 |
| `AGENT_DAILY_QUOTA_USER` | 0 | 登录用户 |

主体标识：登录用户是 `user_id`，密钥访客是手上全部可用密钥哈希排序后再哈希（增减密钥才换主体），公开访客是 `sha256(share_session|ip)`。日界按 Asia/Shanghai 切。**不限额的主体也照样计数**，只是不拦，便于事后看用量。

真实 IP 走 [app/core/client_ip.py](../app/core/client_ip.py)：`CF-Connecting-IP` → `X-Forwarded-For` 首段 → `X-Real-IP` → `request.client.host`。过 Cloudflare Tunnel 时最后那个永远是隧道那一端，所以前面几个不能省。

## 沙箱与成本

`ClaudeAgentOptions` 里几处约束值得单独说：

- `disallowed_tools` 列了 30 多个内置工具的裸名字。这不只是安全问题——不禁掉的话 SDK 会把 Claude Code 那套完整系统提示词和工具定义一起塞进上下文，实测单轮 input 从 **28847** tokens 降到 **900** 上下，差 30 倍。
- `setting_sources=[]` 阻止它读本机 `~/.claude/` 与仓库里的 `CLAUDE.md`；`strict_mcp_config=True` 只认我们传的 MCP。
- `cwd` 指向 `data/agent_sessions/`（一个专用空目录）。SDK 按 `cwd` 给会话归档分组，给它一个独立目录就不会跟本机其它 Claude Code 会话混在一起。
- 多轮上下文交给 SDK：首轮传 `session_id=<我们生成的 UUID>`，之后每轮传 `resume=<同一 UUID>`。库里存的消息只用于前端回放，不负责喂模型。

  这里有个运维上的隐含依赖：**上下文实际落在运行后端那个系统账号的 `~/.claude/projects/` 下**，不在我们的库里。后端重启没影响，但换服务账号跑或者清了这个目录，老对话的追问就接不上了（会当成新会话开头）。真要彻底自持，得改成我们自己拼历史消息重放。
- `AGENT_CONCURRENCY` 是独立的并发闸（每个会话是一个 CLI 子进程，比普通 HTTP 调用重），不跟纪要那边的 `LLM_CONCURRENCY` 抢。

## SSE 事件

`POST .../chat/stream` 沿用项目里 `event: <name>\ndata: <JSON>\n\n` 的写法：

| 事件 | 说明 |
|------|------|
| `status` | `PREPARING` / `READY` / `QUEUED` / `WAITING_MODEL` |
| `segment` | 新开一段思考或正文，前端据此拆块 |
| `thinking` | 工具调用之间的思考增量，对话里展开着流 |
| `tool_start` / `tool_end` | 模型刚决定调用时先占位，工具真正跑起来再补人话摘要（「检索 期末考试｜正则｜或」/「命中 7 处，分布在 3 个文件」） |
| `delta` | 回答的流式增量 |
| `card` | send 工具推的引文卡片 |
| `done` | 完整回答、卡片清单、token 用量、剩余配额 |
| `error` | 出错原因；配额用尽时带 `quota_exceeded` |

思考和工具痕迹按发生顺序落进 `agent_messages`（`role=TOOL`），回看对话时中间那段不会变成空白。

## 数据表

- `agent_sessions`：`sdk_session_id`、主体、范围、标题、轮次。
- `agent_messages`：`session_id` + `seq` + `role`（USER / ASSISTANT / CARD / TOOL）+ `content` + `meta_json`。访客的也落库。
- `agent_daily_usage`：`(subject_type, subject_key, day)` 唯一，`used` 计数。

## 相关环境变量

```ini
AGENT_BASE_URL=            # 三件套逐项留空则回落兜底那组 LLM_*
AGENT_API_KEY=
AGENT_MODEL=
AGENT_MAX_TURNS=24
AGENT_CONCURRENCY=4
AGENT_DAILY_QUOTA_ANON=20
AGENT_DAILY_QUOTA_KEY=0
AGENT_DAILY_QUOTA_USER=0
AGENT_SEARCH_MAX_RESULTS=50
```

三件套（`AGENT_BASE_URL` / `AGENT_API_KEY` / `AGENT_MODEL`）是[模型分工](../README.md#模型分工)七组里的一组，只在 `.env` 里配，改完重启。其余 `AGENT_*` 是配置表种子，改完以超管「系统配置」页为准；`AGENT_CONCURRENCY` 要重启后端才生效。
