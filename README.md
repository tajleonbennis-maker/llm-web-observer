# LLM Web Observer

End-to-end observability and audit trails for multi-user LLM Web applications.

LLM Web Observer connects a user's browser interaction to the backend request,
agent execution, context construction, retrieval, model calls, tool calls, and
streamed response that followed it. DeepTutor will be the first integration,
but the project is application-agnostic.

## Project Status

Experimental runtime deployment. DeepTutor traffic is currently captured through
an Nginx browser collector and an outbound mitmproxy gateway. Interfaces and
storage schemas are not stable yet.

Current capabilities include browser UA/fingerprint capture, full conversation
context and model response audit, OpenRouter model/token/latency reporting,
gateway log/redact/block policies, raw trace inspection, and automated server
deployment. See [the technical guide](docs/TECHNICAL.md) for the architecture,
security boundaries, APIs, deployment, and verification procedure.

中文项目文章与完整实验记录见
[《从用户点击到 LLM 回复》](docs/ARTICLE_ZH.md)。

## Scope

The first version will provide:

- Browser interaction tracing for meaningful product actions.
- W3C Trace Context propagation across HTTP and WebSocket boundaries.
- FastAPI/Python and Node.js instrumentation.
- Agent, retrieval, LLM, and tool-call spans.
- Model, provider, token, latency, retry, error, and cost metadata.
- Conversation and trace waterfall views.
- Configurable sampling, redaction, and retention.
- OTLP ingestion and an API for custom events.
- An independently deployable collector, storage layer, and dashboard.

## Non-Goals

The initial system does not depend on or integrate with BuildProof, host audit
agents, deployment orchestrators, or infrastructure monitoring. Those systems
may be connected later through optional adapters.

## Trace Model

```text
browser interaction
  -> HTTP or WebSocket request
    -> agent run
      -> memory and context loading
      -> retrieval
      -> model call
      -> tool call
      -> model call
    -> streamed response
  -> browser render
```

Core correlation identifiers:

```text
tenant_id
user_id_hash
session_id
conversation_id
interaction_id
trace_id
span_id
```

## Privacy

The generic SDK remains metadata-oriented. The current DeepTutor experimental
integration explicitly collects prompt context and responses for runtime audit.
Production deployments must choose and enforce one of three content policies:

- `metadata-only`: record counts, timings, hashes, and sizes.
- `redacted`: record content after configured redaction.
- `full-content`: explicit opt-in for authorized debugging environments.

The system will support field-level redaction, secret detection, tenant
isolation, retention limits, deletion, and access auditing.

## Planned Components

```text
packages/browser-sdk/
packages/node-sdk/
packages/python-sdk/
services/collector/
services/api/
services/dashboard/
deploy/
docs/
```

OpenTelemetry and its Generative AI semantic conventions will be used where
stable. Product-level browser events will retain a small, versioned schema so
the application contract does not depend on experimental browser conventions.

## Run the MVP

```bash
docker compose up --build
LWO_API_KEY=replace-with-a-random-secret python3 examples/seed_trace.py
```

Open `http://127.0.0.1:8080` to inspect the example trace. API documentation is
available at `http://127.0.0.1:8080/docs`.

When `LWO_API_KEY` is configured, telemetry writers must send it as a Bearer
token. The dashboard and read-only trace APIs remain directly accessible.

The collector accepts batches at `POST /v1/events`:

```json
{
  "events": [{
    "schema_version": "0.1",
    "timestamp": "2026-08-24T12:00:00Z",
    "event_type": "gen_ai.chat",
    "trace_id": "0123456789abcdef0123456789abcdef",
    "span_id": "0123456789abcdef",
    "service": "example-agent",
    "duration_ms": 520,
    "status": "ok",
    "attributes": {
      "gen_ai.provider.name": "openai",
      "gen_ai.request.model": "gpt-5",
      "gen_ai.usage.input_tokens": 100,
      "gen_ai.usage.output_tokens": 50
    }
  }]
}
```

## License

MIT
