from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 常驻 .env（不进配置表）----------
    feishu_app_id: str
    feishu_app_secret: str

    # 超级管理员解锁口令（兑换 SUPER_ADMIN JWT）；兼容旧名 ADMIN_TOKEN
    admin_token: str = ""
    super_admin_token: str = ""

    # 用户/超管 JWT
    jwt_secret: str = ""
    jwt_expire_seconds: int = 604800

    @property
    def resolved_super_admin_token(self) -> str:
        return (self.super_admin_token or self.admin_token or "").strip()

    app_host: str = "0.0.0.0"
    app_port: int = 7354
    app_debug: bool = False

    frontend_host: str = "0.0.0.0"
    frontend_port: int = 7355
    frontend_origin: str = "http://127.0.0.1:7355"
    api_public_base: str = "http://127.0.0.1:7355"

    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    storage_root: str = "./data/meetings"

    feishu_api_base: str = "https://open.feishu.cn"
    feishu_oauth_authorize_url: str = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    feishu_oauth_token_url: str = "https://accounts.feishu.cn/oauth/v3/token"
    # 单项兼容；多项白名单请用 FEISHU_OAUTH_REDIRECT_URIS（逗号分隔，本地+公网都登记）
    feishu_oauth_redirect_uri: str = "http://127.0.0.1:7355/api/v1/auth/feishu/callback"
    feishu_oauth_redirect_uris: str = ""
    feishu_oauth_scopes: str = (
        "minutes:minutes.search:read minutes:minutes.basic:read "
        "minutes:minutes.media:export minutes:minutes.transcript:export offline_access"
    )
    # 飞书 SSO 建号是否强制邀请码（当前默认关闭，预留开关）
    feishu_sso_require_invite: bool = False

    # step-explore 走 Step Plan 通道；SDK/自拼路径都会再接 /v1/messages
    llm_base_url: str = "https://api.stepfun.com/step_plan"
    llm_api_key: str = ""
    llm_model: str = "step-explore"

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # Cloudflare R2：密钥与桶常驻 .env；画质/TTL 见下方种子项
    r2_enabled: bool = False
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""

    # ---------- 配置表种子（首次写入 system_configs；运行时以 runtime_config 为准）----------
    # 0 表示不人为截断，请求时按平台允许的上限发送
    llm_max_tokens: int = 0
    llm_timeout_seconds: float = 600.0
    llm_max_attempts: int = 5
    # 全局大模型 HTTP 调用并发（纪要/脱敏/挑图/答疑等共用）；过高易 429
    llm_concurrency: int = 20

    # 妙记下载队列并发（媒体 + 转写拉取）
    download_concurrency: int = 5

    summary_auto_generate: bool = True

    # minutes.minute.generated_v1 可能早于转写就绪发出（2091003），下载前按这个节奏重试
    minute_ready_max_attempts: int = 8
    minute_ready_retry_seconds: float = 15.0

    # 有视频的会议改走"配图纪要"流水线：规划截图 -> 抽帧筛选 -> 脱敏 -> 成文
    summary_illustrate: bool = True
    # 配图上限按时长：首小时 base 张，之后每多半小时 +per 张（不足半小时按半小时）
    summary_figures_first_hour: int = 20
    summary_figures_per_extra_half_hour: int = 6
    summary_figure_batch_size: int = 6
    # 成文前敏感信息扫描 + 马赛克复核；关闭则直接把候选图交给成文模型
    summary_redact: bool = True
    summary_redact_max_attempts: int = 3
    # 马赛克像素块边长（越大越糊）；区域会再外扩约 2%
    summary_redact_mosaic_block: int = 12

    frame_max_width: int = 1920
    # ffmpeg -q:v，2 最好 31 最差；讲课录屏多是文字，画质不能压太狠
    frame_jpeg_quality: int = 3
    # 讲话往往滞后于画面，围绕候选时间点抽一簇再挑，单位为秒
    frame_burst_offsets: str = "-2,1,4,8"
    frame_regrab_limit: int = 3

    r2_signed_url_ttl_seconds: int = 7200
    r2_video_crf: int = 32
    r2_video_max_height: int = 720
    r2_video_preset: str = "veryfast"
    # 压缩超时上限（秒）；长课可能很久，默认 6 小时
    r2_video_compress_timeout_seconds: float = 21600.0

    # 导出 PDF/DOCX 水印文案；空字符串表示不加水印
    export_watermark_text: str = ""

    @property
    def r2_endpoint(self) -> str:
        account = (self.r2_account_id or "").strip()
        if not account:
            return ""
        return f"https://{account}.r2.cloudflarestorage.com"

    @property
    def frame_burst_offset_list(self) -> list[float]:
        offsets: list[float] = []
        for raw in self.frame_burst_offsets.split(","):
            item = raw.strip()
            if not item:
                continue
            try:
                offsets.append(float(item))
            except ValueError:
                continue
        return offsets or [0.0]


settings = Settings()  # pyright: ignore[reportCallIssue]
