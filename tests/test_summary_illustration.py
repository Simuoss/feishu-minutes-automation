"""截图计划解析与配图收尾（未引用即删除）。"""

import json
from pathlib import Path

import pytest

from app.service.summary_illustration import (
    PreparedFigure,
    ScreenshotPlanItem,
    SummaryIllustrationService,
    _candidate_reporter,
    extract_json_object,
    figure_limit_for_duration,
    parse_plan,
    salvage_figure_objects,
)

# 真实故障样本：模型服务返 500 期间流被掐断，JSON 停在第三条候选中间
TRUNCATED_PLAN = (
    '{"screen_shared": true, "reason": "多处指向屏幕", '
    '"figures": [{"timestamp": "00:23:16", "expect": "文章页面，可见标题与配图", '
    '"purpose": "标出推荐阅读的第一篇文章", "priority": 2}, '
    '{"timestamp": "00:24:20", "expect": "第二篇文章页面", '
    '"purpose": "对应讲解的概念来源", "priority": 2}, {"timestam'
)


def _plan_json(*entries: dict[str, object], screen_shared: bool = True) -> str:
    payload: dict[str, object] = {
        "screen_shared": screen_shared,
        "reason": "测试用理由",
        "figures": list(entries),
    }
    return json.dumps(payload, ensure_ascii=False)


def _plan_items(raw: str, *, limit: int) -> list[ScreenshotPlanItem]:
    return parse_plan(raw, limit=limit).items


def test_figure_limit_for_duration_first_hour_caps_at_20():
    assert figure_limit_for_duration(1) == 20
    assert figure_limit_for_duration(1800) == 20
    assert figure_limit_for_duration(3600) == 20


def test_figure_limit_for_duration_extra_half_hours_ceil():
    # 刚过 1 小时：不足半小时按半小时 → +6
    assert figure_limit_for_duration(3601) == 26
    # 正好 1.5 小时 → +6
    assert figure_limit_for_duration(5400) == 26
    # 刚过 1.5 小时 → +12
    assert figure_limit_for_duration(5401) == 32
    # 正好 2 小时 → +12
    assert figure_limit_for_duration(7200) == 32


def test_extract_json_handles_code_fence():
    payload = extract_json_object('```json\n{"figures": []}\n```')
    assert payload == {"figures": []}


def test_extract_json_handles_surrounding_prose():
    payload = extract_json_object('好的，结果如下：\n{"figures": [1]}\n希望有帮助')
    assert payload == {"figures": [1]}


def test_extract_json_returns_none_on_prose_only():
    assert extract_json_object("这节课没有需要截图的地方。") is None


def test_extract_json_returns_none_on_truncated_output():
    assert extract_json_object('{"figures": [{"timestamp": "00:01:0') is None


def test_candidate_reporter_emits_each_entry_as_it_completes():
    """规划阶段要跑几十秒，候选一写完就该报出去，而不是等整份 JSON。"""
    reported: list[tuple[str, str]] = []
    on_delta = _candidate_reporter(lambda ts, expect: reported.append((ts, expect)))

    # 按模型实际的吐字节奏切片
    for chunk in [
        '{"screen_shared": true, "reason": "有共享", "figures": [',
        '{"timestamp": "00:02:52", "expect": "终端里的 pip 报错"',
        ', "purpose": "说明路径不对", "priority": 1}',
        ', {"timestamp": "00:17:25", "expect": "conda 下载的包列表"',
        ', "purpose": "说明装了哪个版本", "priority": 2}]}',
    ]:
        on_delta(chunk)

    assert reported == [
        ("00:02:52", "终端里的 pip 报错"),
        ("00:17:25", "conda 下载的包列表"),
    ]


def test_candidate_reporter_does_not_repeat_entries():
    reported: list[tuple[str, str]] = []
    on_delta = _candidate_reporter(lambda ts, expect: reported.append((ts, expect)))

    on_delta('{"figures": [{"timestamp": "00:01:00", "expect": "a"}')
    on_delta(", ")
    on_delta('{"timestamp": "00:02:00", "expect": "b"}]}')

    assert len(reported) == 2


def test_candidate_reporter_skips_entries_without_valid_time():
    reported: list[tuple[str, str]] = []
    on_delta = _candidate_reporter(lambda ts, expect: reported.append((ts, expect)))

    on_delta('{"figures": [{"timestamp": "第三分钟", "expect": "a"}, ')
    on_delta('{"timestamp": "00:02:00", "expect": "b"}]}')

    assert reported == [("00:02:00", "b")]


def test_candidate_reporter_ignores_chunks_without_closing_brace():
    calls = 0

    def record(_ts: str, _expect: str) -> None:
        nonlocal calls
        calls += 1

    on_delta = _candidate_reporter(record)
    on_delta('{"figures": [{"timestamp": "00:01:00"')
    assert calls == 0


def test_plan_respects_no_screen_share_verdict():
    """模型判定没有共享屏幕时，整个配图流程都不该启动。"""
    raw = _plan_json(screen_shared=False)

    plan = parse_plan(raw, limit=10)

    assert plan.screen_shared is False
    assert plan.items == []
    assert plan.usable is False


def test_plan_discards_candidates_that_contradict_the_verdict():
    """判定为无共享却仍列了候选时，以判定结果为准。"""
    raw = _plan_json(
        {"timestamp": "00:01:00", "expect": "a", "purpose": "", "priority": 1},
        screen_shared=False,
    )

    plan = parse_plan(raw, limit=10)

    assert plan.screen_shared is False
    assert plan.items == []


def test_plan_keeps_reason_for_logging():
    plan = parse_plan(_plan_json(screen_shared=False), limit=10)

    assert plan.reason == "测试用理由"


def test_plan_accepts_string_boolean_from_model():
    raw = json.dumps(
        {
            "screen_shared": "false",
            "reason": "纯语音答疑",
            "figures": [],
        },
        ensure_ascii=False,
    )

    assert parse_plan(raw, limit=10).screen_shared is False


def test_plan_falls_back_to_candidate_presence_when_field_missing():
    """老响应漏写 screen_shared 时，不该因此丧失配图能力。"""
    with_figures = json.dumps(
        {"figures": [{"timestamp": "00:01:00", "expect": "a", "purpose": "", "priority": 1}]},
        ensure_ascii=False,
    )

    plan = parse_plan(with_figures, limit=10)

    assert plan.screen_shared is True
    assert len(plan.items) == 1


def test_plan_is_usable_only_with_verdict_and_candidates():
    with_both = _plan_json(
        {"timestamp": "00:01:00", "expect": "a", "purpose": "", "priority": 1}
    )
    assert parse_plan(with_both, limit=10).usable is True
    # 有共享但没挑出值得配图的地方
    assert parse_plan(_plan_json(), limit=10).usable is False


def test_truncated_plan_is_treated_as_screen_shared():
    """流被掐断读不到判断字段，但它已经在列候选，说明判定是有共享。"""
    plan = parse_plan(TRUNCATED_PLAN, limit=10)

    assert plan.screen_shared is True
    assert len(plan.items) == 2


def test_salvage_recovers_complete_entries_from_truncated_json():
    salvaged = salvage_figure_objects(TRUNCATED_PLAN)

    assert len(salvaged) == 2
    assert salvaged[0]["timestamp"] == "00:23:16"
    assert salvaged[1]["timestamp"] == "00:24:20"


def test_salvage_ignores_braces_inside_strings():
    text = '{"figures": [{"timestamp": "00:00:10", "expect": "终端里打印了 {\\"a\\": 1}"}]}'

    salvaged = salvage_figure_objects(text)

    assert len(salvaged) == 1
    assert salvaged[0]["expect"] == '终端里打印了 {"a": 1}'


def test_salvage_returns_empty_without_any_complete_entry():
    assert salvage_figure_objects('{"figures": [{"timestam') == []


def test_parse_plan_uses_salvaged_entries_when_stream_is_cut():
    """流被掐断不该让整次调用白跑，已完整的候选要继续用。"""
    items = _plan_items(TRUNCATED_PLAN, limit=10)

    assert [i.timestamp for i in items] == ["00:23:16", "00:24:20"]


def test_parse_plan_reads_all_fields():
    raw = _plan_json(
        {
            "timestamp": "00:02:52",
            "expect": "终端里的 pip 输出",
            "purpose": "说明装错了环境",
            "priority": 1,
        }
    )
    items = _plan_items(raw, limit=10)
    assert len(items) == 1
    assert items[0].timestamp == "00:02:52"
    assert items[0].seconds == 172
    assert items[0].expect == "终端里的 pip 输出"
    assert items[0].priority == 1


def test_parse_plan_skips_unparsable_timestamp():
    raw = _plan_json(
        {"timestamp": "第三分钟", "expect": "x", "purpose": "y", "priority": 1},
        {"timestamp": "00:00:30", "expect": "x", "purpose": "y", "priority": 1},
    )
    items = _plan_items(raw, limit=10)
    assert [i.timestamp for i in items] == ["00:00:30"]


def test_parse_plan_sorts_by_time():
    raw = _plan_json(
        {"timestamp": "00:10:00", "expect": "b", "purpose": "", "priority": 2},
        {"timestamp": "00:01:00", "expect": "a", "purpose": "", "priority": 2},
    )
    items = _plan_items(raw, limit=10)
    assert [i.timestamp for i in items] == ["00:01:00", "00:10:00"]


def test_parse_plan_trims_by_priority_then_restores_time_order():
    raw = _plan_json(
        {"timestamp": "00:01:00", "expect": "a", "purpose": "", "priority": 3},
        {"timestamp": "00:02:00", "expect": "b", "purpose": "", "priority": 1},
        {"timestamp": "00:03:00", "expect": "c", "purpose": "", "priority": 1},
    )
    items = _plan_items(raw, limit=2)
    assert [i.timestamp for i in items] == ["00:02:00", "00:03:00"]


def test_parse_plan_handles_empty_figures():
    assert _plan_items('{"figures": []}', limit=5) == []


def test_parse_plan_handles_missing_figures_key():
    assert _plan_items('{"items": []}', limit=5) == []


def test_parse_plan_defaults_bad_priority():
    raw = _plan_json({"timestamp": "00:00:10", "expect": "a", "purpose": "", "priority": "高"})
    items = _plan_items(raw, limit=5)
    assert items[0].priority == 2


def _figure(tmp_path: Path, index: int) -> PreparedFigure:
    figure_id = f"fig-{index:02d}"
    path = tmp_path / f"{figure_id}.jpg"
    path.write_bytes(b"fake-jpeg")
    return PreparedFigure(
        figure_id=figure_id,
        relative_path=f"assets/{figure_id}.jpg",
        path=path,
        timestamp="00:00:10",
        frame_seconds=10.0,
        expect="",
        purpose="",
    )


def test_referenced_ids_matches_relative_path(tmp_path: Path):
    figures = [_figure(tmp_path, 1), _figure(tmp_path, 2)]
    markdown = "正文\n\n![终端](assets/fig-02.jpg)\n图：说明 `00:00:10`"

    assert SummaryIllustrationService.referenced_ids(markdown, figures) == {"fig-02"}


def test_drop_unreferenced_deletes_files_from_disk(tmp_path: Path):
    kept_fig = _figure(tmp_path, 1)
    dropped = _figure(tmp_path, 2)
    markdown = "![a](assets/fig-01.jpg)\n图：说明 `00:00:10`"

    kept = SummaryIllustrationService.drop_unreferenced(markdown, [kept_fig, dropped])

    assert [f.figure_id for f in kept] == ["fig-01"]
    assert kept_fig.path.is_file()
    # 未被引用的图必须落地删除：模型被要求跳过含密钥的画面，这里是兜底
    assert not dropped.path.exists()


def test_drop_unreferenced_removes_everything_when_no_image_used(tmp_path: Path):
    figures = [_figure(tmp_path, 1), _figure(tmp_path, 2)]

    kept = SummaryIllustrationService.drop_unreferenced("纯文字纪要，没有插图", figures)

    assert kept == []
    assert not any(f.path.exists() for f in figures)


def test_build_manifest_lists_every_figure(tmp_path: Path):
    figures = [_figure(tmp_path, 1), _figure(tmp_path, 2)]
    manifest = SummaryIllustrationService.build_manifest(figures)

    assert "fig-01" in manifest
    assert "assets/fig-02.jpg" in manifest
    assert manifest.startswith("<figures>")
    assert manifest.endswith("</figures>")


def test_to_images_reads_bytes_in_order(tmp_path: Path):
    figures = [_figure(tmp_path, 1), _figure(tmp_path, 2)]
    images = SummaryIllustrationService.to_images(figures)

    assert len(images) == 2
    assert all(img.data == b"fake-jpeg" for img in images)
    assert all(img.media_type == "image/jpeg" for img in images)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
