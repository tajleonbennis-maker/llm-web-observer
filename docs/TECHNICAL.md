# LLM Web Observer Technical Guide

## 1. Purpose

LLM Web Observer audits an LLM web application's runtime behavior without
requiring changes to that application's source code. The DeepTutor integration
captures the browser identity, provider request context, model response,
latency, token usage, policy decision, and original event payload.

## 2. Runtime Architecture

```text
Browser
  -> deeptutor-proxy :3783
       -> browser fingerprint collector -> Observer :8080
       -> DeepTutor frontend :3782 / backend :8001
            -> deeptutor-mitm :8080
                 -> gateway policy evaluation
                 -> OpenRouter
                 -> Observer event ingestion

Administrator -> Observer dashboard :8080
```

The Nginx proxy injects `/_lwo/client.js` into HTML. The script computes a
SHA-256 fingerprint from UA, platform, language, screen properties and timezone,
then sends it to the same-origin `/_lwo/client-context` endpoint. It does not use
canvas, audio, installed fonts, cookies, or hardware probing.

mitmproxy observes outbound OpenRouter calls. It captures up to 100 messages
from the request (`system`, `user`, `assistant`, and `tool` roles), evaluates the
last user message against gateway policies, parses the streamed or JSON model
response, and exports one `gen_ai.chat` event.

## 3. Identity And Correlation

Stored client fields:

| Field | Meaning |
| --- | --- |
| `client.address` | Public source IP supplied by the trusted Nginx proxy |
| `user_agent.original` | Full browser UA |
| `client.browser` | Chrome, Edge, Firefox, Safari, or Unknown |
| `client.platform` | Browser-reported operating system/platform |
| `client.fingerprint` | SHA-256 browser characteristic hash |
| `session_id` | Random per-tab browser session ID |
| `conversation_id` | Hash derived from the first user message |

The proxy integration currently associates the most recently active browser
within a five-minute window with a provider request. The event records
`client.identity.confidence=temporal`. This is suitable for the current
single-user experiment but is not a strong multi-user correlation mechanism.
Before enabling multiple concurrent users, propagate a signed request ID from
the WebSocket request into the provider request and join on that ID.

## 4. Data And Privacy

SQLite data is stored at `/var/lib/llm-web-observer/observer.db`. Container
replacement does not remove this directory. Provider keys, authorization,
cookies, passwords, and common secret formats are redacted during ingestion.

Full conversation content is intentionally enabled for this experimental audit
environment. Production deployments need explicit user notice, a retention
period, role-based dashboard access, deletion APIs, encryption at rest, and a
review of local privacy law. A browser fingerprint is an identifier and must be
handled accordingly.

## 5. APIs

| Method | Path | Purpose | Authorization |
| --- | --- | --- | --- |
| `GET` | `/health` | Liveness | None |
| `POST` | `/v1/events` | Batch event ingestion | Bearer key |
| `GET` | `/v1/conversations` | Readable messages, responses and context | None |
| `GET` | `/v1/traces/{id}` | Original stored events | None |
| `POST` | `/v1/client-context` | Browser identity registration | Same-origin proxy |
| `GET` | `/v1/client-context/latest` | Internal correlation lookup | Bearer key |
| `GET/POST/PUT/DELETE` | `/v1/policies` | Gateway policy management | Writes require Bearer key |

The current dashboard read APIs are intentionally open for the temporary test
server. Put them behind authentication before using real user data.

## 6. Automated Deployment

The repository has one deployment entry point:

```bash
set -a
source .surface.env
set +a
export SURFACE_KNOWN_HOSTS="$HOME/.ssh/known_hosts"
deploy/deploy.sh
```

Required local variables are `SURFACE_HOST`, `SURFACE_USER`, and either
`SURFACE_PASSWORD` or `SURFACE_IDENTITY_FILE`. The server must already contain:

- Docker, curl, and the `deeptutor-net` Docker network.
- `/var/lib/llm-web-observer/mitmproxy/observer.env` with `LWO_API_KEY` and
  `LWO_OBSERVER_URL`.
- Running DeepTutor containers named `bp-deeptutor-product` and optionally
  `deeptutor-mitm`.

The script packages the current checkout, uploads it, builds an immutable image
tag, updates the addon and proxy configuration, recreates containers, retains
the database directory, and verifies ports 8080 and 3783.

### GitHub Actions

The `Deploy Observer` workflow runs tests and deploys each relevant push to
`main`. Configure these repository secrets:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | Server IP or hostname |
| `DEPLOY_USER` | SSH account, currently `ubuntu` |
| `DEPLOY_SSH_KEY` | Dedicated deployment private key |
| `DEPLOY_HOST_KEY` | Complete pinned known-hosts line from `ssh-keyscan` |

Use a dedicated key limited to this server. Do not store the SSH password,
OpenRouter key, or Observer API key in the repository.

## 7. Verification

After deployment:

```bash
curl -f http://SERVER:8080/health
curl -f http://SERVER:3783/ | grep '/_lwo/client.js'
curl -f http://SERVER:3783/_lwo/client.js | grep fingerprint
```

Open DeepTutor in a fresh browser tab and send a multi-turn conversation. In
Observer, open **Conversations**, select the newest row, and verify the IP,
browser, platform, UA, fingerprint, roles, complete context, response, model,
policy decision, and latency.

## 8. Operational Risks

- Temporal browser/provider association can attach the wrong identity under
  concurrent traffic; request-level signed correlation is the next milestone.
- mitmproxy sees decrypted provider content and its CA/private material must be
  restricted to the audit host.
- A client can spoof UA and browser characteristics. The fingerprint identifies
  a browser profile; it does not prove a person's identity.
- The dashboard is currently unauthenticated and must not be exposed with real
  customer data.
