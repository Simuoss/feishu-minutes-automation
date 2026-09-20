"""登录刷新票：每次续期连刷新票一起换新。"""

from app.core.jwt_auth import (
    decode_refresh_token,
    decode_token,
    issue_super_session,
    issue_user_session,
    issue_user_token,
)


def test_user_session_refresh_cannot_be_used_as_bearer():
    pair = issue_user_session(user_id=7, username="alice")
    assert decode_token(pair.access_token).user_id == 7
    assert decode_token(pair.refresh_token) is None
    refresh = decode_refresh_token(pair.refresh_token)
    assert refresh is not None
    assert refresh.user_id == 7
    assert refresh.username == "alice"


def test_super_session_refresh_roundtrip():
    pair = issue_super_session()
    assert decode_token(pair.access_token).is_super_admin
    assert decode_refresh_token(pair.refresh_token).is_super_admin
    assert decode_token(pair.refresh_token) is None


def test_old_access_token_without_typ_still_works():
    token = issue_user_token(user_id=3, username="bob")
    principal = decode_token(token)
    assert principal is not None
    assert principal.user_id == 3
    assert decode_refresh_token(token) is None
