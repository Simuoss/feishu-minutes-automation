"""敏感扫描 JSON 解析与马赛克打码。"""

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.integrations.llm.messages_client import LlmCompletion
from app.service.figure_redaction import (
    FigureRedactionService,
    parse_scan_response,
    parse_verify_response,
)
from app.service.image_mosaic import MosaicRegion, apply_mosaic, mosaic_file
from app.service.summary_illustration import PreparedFigure


class ScriptedLlm:
    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    async def complete(self, system_prompt, user_content, *, images=None, **kwargs):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_content": user_content,
                "image_count": len(images or []),
            }
        )
        text = self._outputs.pop(0) if self._outputs else ""
        return LlmCompletion(
            text=text,
            input_tokens=10,
            output_tokens=10,
            stop_reason="end_turn",
            attempts=1,
            elapsed_seconds=0.1,
        )


def _make_figure(tmp_path: Path, figure_id: str) -> PreparedFigure:
    path = tmp_path / f"{figure_id}.jpg"
    img = Image.new("RGB", (120, 80), color=(30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle((10, 30, 110, 50), fill=(220, 220, 40))
    img.save(path, format="JPEG")
    return PreparedFigure(
        figure_id=figure_id,
        relative_path=f"assets/{figure_id}.jpg",
        path=path,
        timestamp="00:01:00",
        frame_seconds=60.0,
        expect="测试画面",
        purpose="单测",
    )


def test_parse_scan_marks_sensitive_with_regions():
    raw = json.dumps(
        {
            "figures": [
                {"figure_id": "fig-01", "sensitive": False, "regions": []},
                {
                    "figure_id": "fig-02",
                    "sensitive": True,
                    "regions": [
                        {"label": "API密钥", "x1": 0.1, "y1": 0.2, "x2": 0.8, "y2": 0.3}
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )
    items = parse_scan_response(raw, ["fig-01", "fig-02"])
    assert items[0].sensitive is False
    assert items[1].sensitive is True
    assert items[1].regions[0].label == "API密钥"
    assert items[1].regions[0].x1 == 0.1


def test_parse_scan_fills_missing_ids_as_non_sensitive():
    raw = '{"figures": [{"figure_id": "fig-01", "sensitive": false, "regions": []}]}'
    items = parse_scan_response(raw, ["fig-01", "fig-02"])
    assert [i.figure_id for i in items] == ["fig-01", "fig-02"]
    assert items[1].sensitive is False


def test_parse_scan_sensitive_without_regions_becomes_non_sensitive():
    raw = '{"figures": [{"figure_id": "fig-01", "sensitive": true, "regions": []}]}'
    items = parse_scan_response(raw, ["fig-01"])
    assert items[0].sensitive is False


def test_parse_verify_approved():
    raw = '{"figure_id": "fig-02", "approved": true, "reason": "已盖住", "regions": []}'
    result = parse_verify_response(raw, "fig-02")
    assert result is not None
    assert result.approved is True


def test_parse_verify_rejected_with_corrected_regions():
    raw = json.dumps(
        {
            "figure_id": "fig-02",
            "approved": False,
            "reason": "右侧仍可见",
            "regions": [{"label": "密钥", "x1": 0.05, "y1": 0.1, "x2": 0.9, "y2": 0.25}],
        },
        ensure_ascii=False,
    )
    result = parse_verify_response(raw, "fig-02")
    assert result is not None
    assert result.approved is False
    assert len(result.regions) == 1
    assert result.regions[0].x2 == 0.9


def test_apply_mosaic_pixelates_region():
    img = Image.new("RGB", (100, 100), color=(0, 0, 0))
    # 棋盘细纹：打码后邻域像素应变相同
    for x in range(10, 90):
        for y in range(40, 60):
            img.putpixel((x, y), (255, 0, 0) if (x + y) % 2 == 0 else (0, 255, 0))
    assert img.getpixel((50, 50)) != img.getpixel((51, 50))
    out = apply_mosaic(
        img,
        [MosaicRegion(x1=0.1, y1=0.4, x2=0.9, y2=0.6)],
        block_size=8,
    )
    assert out.getpixel((50, 50)) == out.getpixel((51, 50))
    # 区域外不应被改动
    assert out.getpixel((5, 5)) == (0, 0, 0)


def test_mosaic_file_overwrites_jpeg(tmp_path: Path):
    src = tmp_path / "fig.jpg"
    img = Image.new("RGB", (80, 80), color=(20, 40, 60))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 40, 20), fill=(200, 200, 0))
    img.save(src, format="JPEG")
    mosaic_file(
        src,
        src,
        [MosaicRegion(x1=0.0, y1=0.0, x2=0.5, y2=0.25)],
        block_size=10,
    )
    assert src.is_file()
    with Image.open(src) as loaded:
        assert loaded.size == (80, 80)


def test_redact_skips_when_no_sensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.service.figure_redaction.settings.summary_redact", True)
    fig = _make_figure(tmp_path, "fig-01")
    before = fig.path.read_bytes()
    scan = json.dumps(
        {"figures": [{"figure_id": "fig-01", "sensitive": False, "regions": []}]},
        ensure_ascii=False,
    )
    llm = ScriptedLlm(scan)
    outcome = asyncio.run(
        FigureRedactionService(llm).redact_figures(
            [fig], tmp_path / "work", tmp_path / "originals"
        )
    )
    assert outcome.sensitive == 0
    assert outcome.figures == [fig]
    assert fig.path.read_bytes() == before
    assert len(llm.calls) == 1
    assert outcome.items[0].status == "CLEAN"


def test_redact_passes_after_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.service.figure_redaction.settings.summary_redact", True)
    monkeypatch.setattr("app.service.figure_redaction.settings.summary_redact_max_attempts", 3)
    fig = _make_figure(tmp_path, "fig-01")
    scan = json.dumps(
        {
            "figures": [
                {
                    "figure_id": "fig-01",
                    "sensitive": True,
                    "regions": [
                        {"label": "密钥", "x1": 0.05, "y1": 0.3, "x2": 0.95, "y2": 0.7}
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
    verify = json.dumps(
        {"figure_id": "fig-01", "approved": True, "reason": "已盖住", "regions": []},
        ensure_ascii=False,
    )
    llm = ScriptedLlm(scan, verify)
    outcome = asyncio.run(
        FigureRedactionService(llm).redact_figures(
            [fig], tmp_path / "work", tmp_path / "originals"
        )
    )
    assert outcome.redacted == 1
    assert outcome.abandoned == 0
    assert fig.path.is_file()
    assert len(llm.calls) == 2
    assert llm.calls[1]["image_count"] == 2
    assert outcome.items[0].status == "REDACTED"
    assert (tmp_path / "originals" / "fig-01.jpg").is_file()


def test_redact_abandons_after_max_failed_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.service.figure_redaction.settings.summary_redact", True)
    monkeypatch.setattr("app.service.figure_redaction.settings.summary_redact_max_attempts", 3)
    fig = _make_figure(tmp_path, "fig-01")
    scan = json.dumps(
        {
            "figures": [
                {
                    "figure_id": "fig-01",
                    "sensitive": True,
                    "regions": [
                        {"label": "密钥", "x1": 0.1, "y1": 0.3, "x2": 0.4, "y2": 0.5}
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
    reject = json.dumps(
        {
            "figure_id": "fig-01",
            "approved": False,
            "reason": "仍可见",
            "regions": [
                {"label": "密钥", "x1": 0.05, "y1": 0.25, "x2": 0.95, "y2": 0.75}
            ],
        },
        ensure_ascii=False,
    )
    llm = ScriptedLlm(scan, reject, reject, reject)
    outcome = asyncio.run(
        FigureRedactionService(llm).redact_figures(
            [fig], tmp_path / "work", tmp_path / "originals"
        )
    )
    assert outcome.abandoned == 1
    assert outcome.figures == []
    assert not fig.path.exists()
    assert len(llm.calls) == 4  # 1 scan + 3 verify
    assert outcome.items[0].status == "ABANDONED"
    assert outcome.items[0].abandon_reason
