from llm_web_observer_sdk import Observer, current_span


def test_nested_spans_share_trace(monkeypatch):
    observer = Observer("http://observer", "test", batch_size=100)
    with observer.span("agent.run", conversation_id="c1") as parent:
        assert current_span() is parent
        with observer.llm("openai", "gpt-test") as child:
            child.set(**{"gen_ai.usage.input_tokens": 12})
    assert current_span() is None
    assert len(observer._events) == 2
    child_event, parent_event = observer._events
    assert child_event["trace_id"] == parent_event["trace_id"]
    assert child_event["parent_span_id"] == parent_event["span_id"]
    assert child_event["attributes"]["gen_ai.request.model"] == "gpt-test"


def test_errors_are_recorded():
    observer = Observer("http://observer", "test", batch_size=100)
    try:
        with observer.tool("broken"):
            raise ValueError("failure")
    except ValueError:
        pass
    event = observer._events[0]
    assert event["status"] == "error"
    assert event["attributes"]["error.type"] == "ValueError"

