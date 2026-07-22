import asyncio

from cineflow.events import EventBroker
from cineflow.models import JobEvent


async def test_event_broker_replays_history_and_ends_on_terminal_event():
    broker = EventBroker()
    await broker.publish("job-1", JobEvent(event_type="accepted"))
    await broker.publish("job-1", JobEvent(event_type="succeeded", progress=100))

    events = []
    async for event in broker.subscribe("job-1"):
        events.append(event.event_type)

    assert events == ["accepted", "succeeded"]


async def test_event_broker_does_not_block_on_slow_subscriber():
    broker = EventBroker(history_limit=2)
    for index in range(5):
        await asyncio.wait_for(
            broker.publish("job-2", JobEvent(event_type=f"event-{index}")),
            timeout=0.1,
        )
    history = await broker.history("job-2")
    assert [item.event_type for item in history] == ["event-3", "event-4"]
