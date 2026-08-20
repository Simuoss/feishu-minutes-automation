"""R2 桶 CORS 覆盖判断与 AccessDenied 识别。"""

from app.integrations.r2_client import _is_access_denied, cors_rules_cover_origins


class _Denied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def test_cors_rules_cover_exact_origins():
    rules = [
        {
            "AllowedOrigins": [
                "https://larkmeeting.simuoss.cn",
                "http://127.0.0.1:7355",
            ],
            "AllowedMethods": ["GET", "HEAD"],
        }
    ]
    assert cors_rules_cover_origins(
        rules,
        ["https://larkmeeting.simuoss.cn", "http://127.0.0.1:7355"],
    )


def test_cors_rules_cover_wildcard():
    assert cors_rules_cover_origins(
        [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET"]}],
        ["https://larkmeeting.simuoss.cn"],
    )


def test_cors_rules_reject_missing_get():
    assert not cors_rules_cover_origins(
        [{"AllowedOrigins": ["*"], "AllowedMethods": ["PUT"]}],
        ["https://larkmeeting.simuoss.cn"],
    )


def test_cors_rules_can_be_asked_about_put():
    """本地导入是浏览器直传，只放开 GET 的桶会把 PUT 拦在预检那一步。"""
    read_only = [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"]}]
    origins = ["https://larkmeeting.simuoss.cn"]

    assert cors_rules_cover_origins(read_only, origins)
    assert not cors_rules_cover_origins(read_only, origins, method="PUT")
    assert cors_rules_cover_origins(
        [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD", "PUT"]}],
        origins,
        method="PUT",
    )


def test_is_access_denied():
    assert _is_access_denied(_Denied("AccessDenied"))
    assert not _is_access_denied(_Denied("NoSuchCORSConfiguration"))
