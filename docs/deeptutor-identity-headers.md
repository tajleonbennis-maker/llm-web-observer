# DeepTutor 需求：出站 LLM 请求携带用户身份头（X-LWO-*）

> 状态：待实现 | 优先级：高 | 影响：LLM 审计归属准确性
> 对接方：LLM Web Observer（已改造完成，本仓库 `integrations/mitmproxy/lwo_addon.py`）

## 1. 背景

LLM Web Observer 通过 mitmproxy 网关观测 DeepTutor → OpenRouter 的全部 LLM 调用。当前出站请求不携带任何用户标识，observer 只能按"5 分钟内最新 client-context"猜测调用者（`confidence: temporal`），导致：

- 多用户平台无法区分"谁"发起了哪次 LLM 调用；
- Android 原生 app 的消息被错误贴上其它来源（如浏览器 beacon）的身份（已实测复现）；
- 标题生成、推荐等内部任务完全无法归属到用户。

身份在 DeepTutor 内部其实一直存在（`multi_user/context.py` 的 `get_current_user()`、chat pipeline 的 `_current_user_id()`），只是在 LLM provider 层丢失。

## 2. 目标

每次出站 LLM 请求（含 user.chat、internal.title、internal.recommendations 等**全部** LLM 调用）都携带身份头，使 observer 端归属置信度从 `temporal` 升级为 `exact`。

## 3. Header 契约

| Header | 必填 | 说明 |
|---|---|---|
| `X-LWO-User` | ✅ | 平台 user_id（`user.id`，字符串）。observer 落为 `enduser.id` + 事件 `user_id_hash` |
| `X-LWO-Username` | 建议 | 显示名（`user.username`）。observer 落为 `enduser.name`，在 Conversations 列表直接展示 |
| `X-LWO-Session` | 建议 | 会话/对话 session_id。observer 落为事件 `session_id`，实现按用户会话分组 |
| `X-LWO-Platform` | 建议 | `android` / `web` / `ios` / `cli`。observer 落为 `client.platform` |
| `X-LWO-Task` | 可选 | 后台任务可标注 `title` / `recommendations` 等，便于 observer 区分内部任务来源 |

约束：
- 值一律 UTF-8 字符串，**≤160 字符**（超长截断或拒绝）；
- 头在网关会被剥除后再转发 OpenRouter，不会泄漏给第三方；
- **所有值来自服务端会话（`get_current_user()` 等），禁止信任客户端上报的任何身份字段**（防伪造）。

## 4. 实现方案（推荐）

### 4.1 核心问题：client 复用

`OpenAICompatProvider`（`deeptutor/services/llm/provider_core/openai_compat_provider.py`）在构造函数里创建**单个复用的** `AsyncOpenAI` client，`default_headers` 是静态的（第 147-156 行）。因此身份头**不能**塞进 `default_headers`——必须按请求注入。

OpenAI SDK 的 `chat.completions.create(...)` / `responses.create(...)` 支持**每次调用传 `extra_headers=`**，这是唯一正确的注入层。

### 4.2 推荐步骤

1. **新建辅助模块** `deeptutor/services/llm/identity_headers.py`：

   ```python
   from deeptutor.multi_user.context import get_current_user

   def lwo_identity_headers(platform: str | None = None) -> dict[str, str]:
       """Build X-LWO-* headers from the current request context. Empty dict when unauthenticated."""
       try:
           user = get_current_user()
       except Exception:
           return {}
       if user is None or user.is_admin is None and not user.id:
           return {}
       headers = {"X-LWO-User": str(user.id)[:160]}
       if getattr(user, "username", None):
           headers["X-LWO-Username"] = str(user.username)[:160]
       if platform:
           headers["X-LWO-Platform"] = platform[:160]
       return headers
   ```

   （具体取用户方式以现有 `multi_user` 实现为准；核心是**从服务端上下文取，不是从请求参数取**。）

2. **在 provider 的请求组装点合并**：`openai_compat_provider.py` 中所有
   `self._client.chat.completions.create(**request_kwargs)` 与 `self._client.responses.create(**body)`
   调用前（非流式 ~L715/L724，流式 ~L778/L821/L830），合并：

   ```python
   identity = lwo_identity_headers()
   if identity:
       request_kwargs.setdefault("extra_headers", {}).update(identity)
   ```

   建议封装成 `_with_identity(kwargs)` 一个私有方法，所有调用点统一走它。

3. **Session 头**：chat pipeline（`deeptutor/agents/chat/agentic_pipeline.py`）发起调用处，把当前
   conversation/session id 通过 contextvar 或显式传参带到 provider，加入 `X-LWO-Session`。
   若现有代码已有 per-request 上下文对象，挂在同一处即可。

4. **Platform 头**：API 层已知请求来源（web 走 HTTP、Android app 的 UA 可识别），在入口
   middleware 设置一次 contextvar（如 `request_platform.set("android")`），辅助模块读取。

### 4.3 后台任务（重要）

标题生成 / 推荐等任务经 `deeptutor_worker`（`DurableWorker`）异步执行，worker 进程里没有请求上下文，直接调 `get_current_user()` 会拿到空。要求：

- **投递任务时把 `user_id` / `username` / `session_id` 写进 job payload**（job 里大概率已有 user 字段，确认并补全）；
- worker 执行 handler 前设置身份 contextvar（或直接把身份作为参数传给 pipeline）；
- 这类调用同时带 `X-LWO-Task: title|recommendations`，observer 端即可把内部任务也精确归属到用户。

## 5. 兼容性

- 网关侧已完成改造并**向后兼容**：请求不带 `X-LWO-*` 头时，行为与现在完全一致（退回时间窗推测，`confidence: temporal`）；带头则精确归属（`confidence: exact`，`enduser.id` / `enduser.name` / `session_id` 全部落库）。
- 因此 DeepTutor 可灰度上线：先改 user.chat 主链路，验证归属正确后再补后台任务。
- 网关会把 `X-LWO-*` 头剥除后转发，OpenRouter 侧零感知。

## 6. 验收标准

1. Web 端登录用户 A 发消息 → observer Conversations 里该条 `username=A`、`identity_confidence=exact`；
2. Android 端登录用户 B 发消息 → 同上归属 B，且与 A 的消息分属不同 `session_id`；
3. 未登录/匿名调用 → 无 `X-LWO-User` 头，observer 端 `confidence=temporal`（现状，不劣化）；
4. 标题生成 / 推荐任务 → trace 带 `enduser.id` 且 `lwo.call.kind` 仍正确分类；
5. 两个用户交替发消息（间隔 < 1 分钟），归属 100% 正确（这是当前时间窗方案必然出错、新方案必须通过的场景）；
6. 出站抓包确认 `X-LWO-*` 头只在 DeepTutor → 网关段出现，网关 → OpenRouter 段无此头。

## 7. 验证方式

部署后在 observer 端核对：

```bash
curl -s "http://165.154.226.119:8080/v1/conversations?limit=5" | python3 -m json.tool
# 看 username / session_id / identity_confidence 字段
curl -s "http://165.154.226.119:8080/v1/traces/<trace_id>" | python3 -m json.tool
# 看 attributes 里 enduser.id / enduser.name / lwo.identity.source == "header"
```
