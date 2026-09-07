"""纪要事件 broker 的状态机行为。"""

from app.service.summary_event_broker import SummaryStatus, summary_broker

OWNER = 1


def test_queue_does_not_downgrade_generating_status():
    token = "broker-test-1"
    summary_broker.clear(token, owner_user_id=OWNER)
    summary_broker.update(
        token, owner_user_id=OWNER, percent=10, stage="解析转写文本"
    )

    summary_broker.queue(token, owner_user_id=OWNER, position=0)

    channel = summary_broker.get(token, owner_user_id=OWNER)
    assert channel is not None
    assert channel.status == SummaryStatus.GENERATING.value
    assert channel.stage == "解析转写文本"
    summary_broker.clear(token, owner_user_id=OWNER)


def test_snapshot_carries_progress_profile():
    token = "broker-test-profile"
    summary_broker.clear(token, owner_user_id=OWNER)
    summary_broker.set_profile(
        token,
        owner_user_id=OWNER,
        profile={
            "id": "import_audio",
            "label": "上传音频",
            "has_transcribe": True,
            "has_figures": False,
        },
    )
    channel = summary_broker.get(token, owner_user_id=OWNER)
    assert channel is not None
    snap = channel.snapshot()
    assert snap["progress_profile"]["id"] == "import_audio"
    assert snap["progress_profile"]["has_figures"] is False
    summary_broker.clear(token, owner_user_id=OWNER)


def test_append_delta_promotes_queued_to_generating():
    token = "broker-test-2"
    summary_broker.clear(token, owner_user_id=OWNER)
    summary_broker.queue(token, owner_user_id=OWNER, position=0)

    summary_broker.append_delta(token, owner_user_id=OWNER, text="hello")

    channel = summary_broker.get(token, owner_user_id=OWNER)
    assert channel is not None
    assert channel.status == SummaryStatus.GENERATING.value
    assert channel.buffer == "hello"
    summary_broker.clear(token, owner_user_id=OWNER)
