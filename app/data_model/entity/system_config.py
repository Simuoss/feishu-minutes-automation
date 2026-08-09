from dataclasses import dataclass


@dataclass
class SystemConfigEntity:
    key: str
    description: str
    value: str
    remark: str
    updated_at: int


@dataclass
class SystemConfigCreateEntity:
    key: str
    description: str = ""
    value: str = ""
    remark: str = ""


@dataclass
class SystemConfigUpdateEntity:
    key: str
    description: str | None = None
    value: str | None = None
    remark: str | None = None
