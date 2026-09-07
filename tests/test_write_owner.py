"""超管代用户同步会议、补跑纪要时的归属解析。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.jwt_auth import AuthPrincipal
from app.service.ownership import resolve_write_owner


def _request(auth: AuthPrincipal | None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


def test_regular_user_writes_as_self_and_ignores_foreign_owner():
    req = _request(AuthPrincipal(role="USER", user_id=4, username="zhou"))
    assert resolve_write_owner(req, owner_user_id=99) == 4


def test_super_admin_must_name_the_meeting_owner():
    req = _request(AuthPrincipal(role="SUPER_ADMIN"))
    with pytest.raises(HTTPException) as exc:
        resolve_write_owner(req)
    assert exc.value.status_code == 400
    assert resolve_write_owner(req, owner_user_id=4) == 4


def test_anonymous_cannot_write():
    req = _request(None)
    with pytest.raises(HTTPException) as exc:
        resolve_write_owner(req, owner_user_id=4)
    assert exc.value.status_code == 401
