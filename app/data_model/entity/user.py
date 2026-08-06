from dataclasses import dataclass


@dataclass
class UserEntity:
    id: int | None
    username: str
    password_hash: str
    status: str
    created_at: int
    updated_at: int


@dataclass
class UserCreateEntity:
    username: str
    password_hash: str
    status: str = "ACTIVE"
    created_at: int = 0
    updated_at: int = 0


@dataclass
class UserUpdateEntity:
    id: int
    username: str | None = None
    password_hash: str | None = None
    status: str | None = None
    updated_at: int | None = None


@dataclass
class UserQueryEntity:
    id: int | None = None
    username: str | None = None
    status: str | None = None
