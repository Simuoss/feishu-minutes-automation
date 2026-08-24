"""批量导出：按会议名打 zip，缺文件写进跳过清单，不让整包失败。"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.service.export_service import ExportService, _zip_entry_name


def test_zip_entry_keeps_chinese_and_disambiguates_collisions():
    used: set[str] = set()
    first = _zip_entry_name("司沐的项目周会", "-纪要.md", used)
    again = _zip_entry_name("司沐的项目周会", "-纪要.md", used)
    slashed = _zip_entry_name("a/b\\c", "-转写.txt", used)

    assert first == "司沐的项目周会-纪要.md"
    assert again == "司沐的项目周会 (2)-纪要.md"
    assert slashed == "a_b_c-转写.txt"


def test_batch_zip_includes_what_exists_and_lists_what_does_not(
    monkeypatch: pytest.MonkeyPatch,
):
    service = ExportService()

    def fake_title(token: str, *, owner_user_id: int) -> str:
        return {"ready": "包管理专场", "empty": "还没写完"}.get(token, token)

    def fake_summary(token: str, fmt: str, *, owner_user_id: int):
        if token != "ready":
            raise LookupError("该会议尚未生成纪要")
        return "# 纪要\n".encode("utf-8"), "pkg-summary.md", "text/markdown"

    def fake_transcript(token: str, fmt: str, *, owner_user_id: int):
        if token != "ready":
            raise LookupError("本地没有该会议的转写文本")
        return "转写正文".encode("utf-8"), "pkg-transcript.txt", "text/plain"

    monkeypatch.setattr("app.service.export_service._meeting_title", fake_title)
    monkeypatch.setattr(service, "export_summary", fake_summary)
    monkeypatch.setattr(service, "export_transcript", fake_transcript)

    data, filename = service.export_batch(
        [("ready", 1), ("empty", 1)],
        include_summary=True,
        include_transcript=True,
        summary_format="md",
        transcript_format="txt",
    )

    assert filename.startswith("会议导出-")
    assert filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "包管理专场-纪要.md" in names
        assert "包管理专场-转写.txt" in names
        assert "_跳过.txt" in names
        skip = zf.read("_跳过.txt").decode("utf-8")
        assert "还没写完" in skip
        assert zf.read("包管理专场-纪要.md") == "# 纪要\n".encode("utf-8")


def test_batch_with_nothing_to_pack_raises(monkeypatch: pytest.MonkeyPatch):
    service = ExportService()
    monkeypatch.setattr(
        "app.service.export_service._meeting_title", lambda token, **_: token
    )
    monkeypatch.setattr(
        service,
        "export_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(LookupError("没有")),
    )

    with pytest.raises(LookupError, match="没有可导出"):
        service.export_batch(
            [("gone", 1)],
            include_summary=True,
            include_transcript=False,
        )


def test_batch_rejects_empty_selection():
    with pytest.raises(ValueError, match="至少勾选"):
        ExportService().export_batch(
            [("a", 1)],
            include_summary=False,
            include_transcript=False,
        )
