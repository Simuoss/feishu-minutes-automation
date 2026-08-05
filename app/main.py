"""飞书妙记自动化 - 后端 API 入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.admin_auth import AdminAuthMiddleware
from app.core.config import settings
from app.core.database import init_db
from app.core.logging import setup_logging
from app.integrations.feishu.ws_client import FeishuWsClientRunner
from app.service.minute_subscription_service import ensure_minute_generated_subscription

_ws_runner: FeishuWsClientRunner | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _ws_runner
    setup_logging(debug=settings.app_debug)
    await init_db()
    _ws_runner = FeishuWsClientRunner()
    _ws_runner.start()
    # 进程重启后补订阅：用户身份订阅不会随 WS 建连自动恢复
    await ensure_minute_generated_subscription()
    yield


app = FastAPI(
    title="飞书妙记自动化",
    description="飞书/妙记对接：长连接监听妙记生成事件，下载并本地存储会议资源",
    version="0.3.0",
    lifespan=lifespan,
)

# 后添加的中间件更靠外：CORS 必须最外层，否则鉴权 401 缺 CORS 头会被浏览器吞掉
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        f"http://localhost:{settings.frontend_port}",
        f"http://127.0.0.1:{settings.frontend_port}",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
    )


if __name__ == "__main__":
    main()
