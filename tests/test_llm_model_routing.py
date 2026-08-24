"""七组模型出处：逐项回落、三件套绑在一起取、配了一半能被认出来。"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.llm_stages import half_configured, suspicious_base_url
from app.integrations.llm.messages_client import AnthropicMessagesClient


def test_a_stage_that_fills_nothing_falls_back_to_the_default_group(
    monkeypatch: pytest.MonkeyPatch,
):
    # 不看本机 .env 实际填了什么，自己造一个全空的环节
    monkeypatch.setattr(settings, "llm_summary_base_url", "")
    monkeypatch.setattr(settings, "llm_summary_api_key", "")
    monkeypatch.setattr(settings, "llm_summary_model", "")

    endpoint = settings.summary_llm

    assert endpoint.base_url == settings.llm_base_url.rstrip("/")
    assert endpoint.api_key == settings.llm_api_key
    assert endpoint.model == settings.llm_model
    assert endpoint.is_default


def test_filling_only_the_model_keeps_the_default_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "llm_figure_base_url", "")
    monkeypatch.setattr(settings, "llm_figure_api_key", "")
    monkeypatch.setattr(settings, "llm_figure_model", "vision-only")

    endpoint = settings.figure_llm

    assert endpoint.model == "vision-only"
    assert endpoint.base_url == settings.llm_base_url.rstrip("/")
    assert endpoint.api_key == settings.llm_api_key
    assert endpoint.overridden == {"model"}
    # 同一家换模型是正常用法，不该报警
    assert half_configured(endpoint) is None


def test_blank_model_falls_back_to_the_default_model_name(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "llm_summary_model", "   ")

    # 纯空格也算没填
    assert settings.summary_llm.model == settings.llm_model
    assert "model" not in settings.summary_llm.overridden


def test_switching_provider_without_a_model_name_is_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "llm_ask_base_url", "https://other.example.com/v1")
    monkeypatch.setattr(settings, "llm_ask_api_key", "sk-other")
    monkeypatch.setattr(settings, "llm_ask_model", "")

    warning = half_configured(settings.ask_llm)

    assert warning is not None
    assert "模型名" in warning


def test_filling_all_three_switches_provider_wholesale(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "llm_redact_base_url", "https://other.example.com/v1/")
    monkeypatch.setattr(settings, "llm_redact_api_key", "sk-other")
    monkeypatch.setattr(settings, "llm_redact_model", "other-vision")

    endpoint = settings.redact_llm

    # 尾斜杠要去掉，否则拼出来是 //v1/messages
    assert endpoint.base_url == "https://other.example.com/v1"
    assert endpoint.api_key == "sk-other"
    assert endpoint.model == "other-vision"
    assert half_configured(endpoint) is None


def test_url_without_key_is_reported_as_half_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "llm_ask_base_url", "https://other.example.com")
    monkeypatch.setattr(settings, "llm_ask_api_key", "")

    warning = half_configured(settings.ask_llm)

    assert warning is not None
    assert "密钥" in warning


def test_key_without_url_is_reported_as_half_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "llm_scene_base_url", "")
    monkeypatch.setattr(settings, "llm_scene_api_key", "sk-other")

    warning = half_configured(settings.scene_llm)

    assert warning is not None
    assert "地址" in warning


def test_a_base_url_ending_in_v1_is_flagged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_summary_base_url", "https://yunwu.ai/v1")
    monkeypatch.setattr(settings, "llm_summary_api_key", "sk-other")
    monkeypatch.setattr(settings, "llm_summary_model", "claude-sonnet-5")

    # 客户端固定接 /v1/messages，带 /v1 的地址会拼成 /v1/v1/messages
    warning = suspicious_base_url(settings.summary_llm)

    assert warning is not None
    assert "/v1" in warning


def test_a_base_url_with_the_full_path_is_flagged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        settings, "llm_figure_base_url", "https://yunwu.ai/v1/messages/"
    )

    assert suspicious_base_url(settings.figure_llm) is not None


def test_a_gateway_path_prefix_is_not_flagged(monkeypatch: pytest.MonkeyPatch):
    # 阶跃的 Step Plan 通道就带一段路径前缀，这是对的
    monkeypatch.setattr(
        settings, "llm_ask_base_url", "https://api.stepfun.com/step_plan"
    )

    assert suspicious_base_url(settings.ask_llm) is None


def test_all_seven_groups_are_reported():
    endpoints = settings.llm_endpoints()

    assert len(endpoints) == 7
    assert [e.stage for e in endpoints][0] == "兜底默认"
    assert {"纪要成文", "挑图", "配图脱敏", "场景判定", "划词答疑", "检索助手"} == {
        e.stage for e in endpoints[1:]
    }
    # 每组都得解析出可用的三件套，不能有空地址或空模型名
    for endpoint in endpoints:
        assert endpoint.base_url
        assert endpoint.model


def test_for_stage_takes_all_three_from_one_group(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_summary_base_url", "https://sum.example.com")
    monkeypatch.setattr(settings, "llm_summary_api_key", "sk-sum")
    monkeypatch.setattr(settings, "llm_summary_model", "sum-model")

    client = AnthropicMessagesClient.for_stage(settings.summary_llm)

    assert client.model == "sum-model"
    assert client._base_url == "https://sum.example.com"
    assert client._api_key == "sk-sum"


def test_a_bare_client_still_falls_back_to_the_default_group():
    assert AnthropicMessagesClient().model == settings.llm_model
