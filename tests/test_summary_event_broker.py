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
