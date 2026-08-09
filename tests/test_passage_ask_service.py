from app.service.passage_ask_service import PassageAskService


def test_selection_in_source_accepts_whitespace_variants():
    svc = PassageAskService()
    source = "你好\n世界  测试"
    assert svc._selection_in_source("你好 世界 测试", source)


def test_selection_in_source_accepts_rendered_heading_without_hashes():
    svc = PassageAskService()
    source = "## 课程目标\n\n本周重点是 **异步** 设计。"
    assert svc._selection_in_source("课程目标", source)
    assert svc._selection_in_source("本周重点是 异步 设计。", source)


def test_nearby_context_includes_before_and_after():
    svc = PassageAskService()
    source = ("前" * 400) + "TARGET" + ("后" * 400)
    ctx = svc._nearby_context(source, "TARGET")
    assert "TARGET" in ctx
    assert len(ctx) <= 300 + 6 + 300 + 5


def test_build_first_user_message_default_question():
    text = PassageAskService._build_first_user_message("选中", "上下文", None)
    assert "【划选片段】" in text
    assert "选中" in text
    assert "解释这段" in text
