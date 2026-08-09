from dataclasses import dataclass


@dataclass
class UserEntity:
    id: int | None
    username: str
    password_hash: str
    status: str
    created_at: int
    updated_at: int
    display_name: str | None = None
    display_name_set_at: int | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_id: str | None = None
    feishu_name: str | None = None
    feishu_en_name: str | None = None
    feishu_avatar_url: str | None = None
    feishu_tenant_key: str | None = None
    feishu_email: str | None = None
    feishu_mobile: str | None = None
    feishu_profile_json: str | None = None

    def has_password(self) -> bool:
        return bool(self.password_hash)

    def public_display_name(self) -> str:
        name = (self.display_name or "").strip()
        if name:
            return name
        fs = (self.feishu_name or "").strip()
        if fs:
            return fs
        return self.username


@dataclass
class UserCreateEntity:
    username: str
    password_hash: str = ""
    status: str = "ACTIVE"
    created_at: int = 0
    updated_at: int = 0
    display_name: str | None = None
    display_name_set_at: int | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_id: str | None = None
    feishu_name: str | None = None
    feishu_en_name: str | None = None
    feishu_avatar_url: str | None = None
    feishu_tenant_key: str | None = None
    feishu_email: str | None = None
    feishu_mobile: str | None = None
    feishu_profile_json: str | None = None


@dataclass
class UserUpdateEntity:
    id: int
    username: str | None = None
    password_hash: str | None = None
    status: str | None = None
    updated_at: int | None = None
    display_name: str | None = None
    display_name_set_at: int | None = None
    feishu_open_id: str | None = None
    feishu_union_id: str | None = None
    feishu_user_id: str | None = None
    feishu_name: str | None = None
    feishu_en_name: str | None = None
    feishu_avatar_url: str | None = None
    feishu_tenant_key: str | None = None
    feishu_email: str | None = None
    feishu_mobile: str | None = None
    feishu_profile_json: str | None = None
    # 显式清空 open_id 等少见；绑定时用非 None 写入


@dataclass
class UserQueryEntity:
    id: int | None = None
    username: str | None = None
    status: str | None = None
    feishu_open_id: str | None = None
