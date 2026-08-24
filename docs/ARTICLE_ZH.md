# 从用户点击到 LLM 回复：我们如何为 DeepTutor 构建无侵入运行时审计系统

## 摘要

传统 Web 监控擅长回答“接口是否可用、服务器是否健康”，却很难回答 LLM 应用真正重要的问题：哪个用户发送了什么内容，应用给模型拼装了哪些上下文，模型回复了什么，期间调用了哪些工具，网关策略是否生效。

为解决这个问题，我们开发了独立项目 **LLM Web Observer**，并以开源学习应用 DeepTutor 作为第一个实验对象。系统不修改 DeepTutor 源码，而是在浏览器入口和模型出口分别加入轻量观察层，将浏览器身份、用户会话、完整上下文、OpenRouter 调用、模型回复和策略决策串成可审计的数据链路。

本文介绍系统设计、部署方式和一组真实实验，并讨论实验中暴露出的会话误判、内部任务误拦截、UTF-8 乱码和浏览器关联准确性问题。

## 一、为什么普通日志不够

一个 LLM Web 应用通常至少包含以下链路：

```text
浏览器操作
  -> WebSocket / HTTP 请求
  -> 应用后端
  -> Agent 循环
  -> 工具调用或检索
  -> LLM Provider
  -> 流式回复
  -> 浏览器渲染
```

Nginx 日志可以看到 IP 和路径，后端日志可以看到异常，模型平台可以看到 token 用量，但它们彼此割裂。站长真正需要的是一条可读、可追溯的记录：

```text
用户 A（IP、浏览器、会话）
  问：“查一下上海的天气，用中文回复”
  系统调用搜索工具
  模型：openai/gpt-4.1-mini
  回复：“上海当前天气如下……”
  策略：允许
  延迟：2999.86 ms
```

同时，站长还需要保留底层原始事件，以便故障排查和安全取证。因此系统必须提供两个视角：

- **人类视角**：用户发送内容、LLM 最终回复、身份和策略结果。
- **工程视角**：完整上下文、内部 Agent 步骤、token、延迟和原始 JSON。

## 二、总体架构

实验部署在一台临时服务器上，DeepTutor 对外端口为 `3783`，Observer 控制台端口为 `8080`。

```text
用户浏览器
  |
  v
deeptutor-proxy :3783
  |-- 注入浏览器采集脚本
  |-- 转发 HTTP / WebSocket
  v
DeepTutor 前端与后端
  |
  v
deeptutor-mitm
  |-- 读取 OpenRouter 请求上下文
  |-- 执行 log / redact / block 策略
  |-- 解析流式或 JSON 回复
  v
OpenRouter / 多个 LLM

浏览器采集 + 模型调用事件
  -> LLM Web Observer :8080
  -> SQLite
  -> Overview / Conversations / Traces / Policies
```

这个方案的关键点是 **不修改 DeepTutor 仓库**。Nginx 在返回 HTML 时注入 `/_lwo/client.js`；DeepTutor 后端访问 OpenRouter 时经过 mitmproxy。Observer 本身保持独立，可以逐步适配其他 LLM Web 项目。

## 三、采集什么数据

### 1. 浏览器与访问身份

浏览器脚本采集：

- 公网来源 IP（由可信 Nginx 转发）。
- 完整 User-Agent。
- 浏览器类型和操作系统平台。
- 语言、时区、屏幕尺寸和颜色深度。
- 基于上述特征计算的 SHA-256 指纹。
- 每个标签页独立的 `session_id`。

脚本每 30 秒发送一次心跳，同一个标签页维持相同 `session_id`。当前指纹不使用 Canvas、音频、字体列表或硬件探测，尽量控制采集范围。

需要强调：浏览器指纹只能辅助区分浏览器环境，不能证明自然人的真实身份，UA 和浏览器特征也可能被伪造。

### 2. LLM 调用

mitmproxy 观察 OpenRouter 请求和响应，记录：

- 模型与 Provider。
- `system`、`user`、`assistant`、`tool` 上下文。
- 最后一条用户消息。
- 最终模型回复。
- 输入、输出和总 token。
- 响应状态、调用时延。
- 网关策略动作及命中规则。
- `session_id`、`conversation_id`、`trace_id` 和 `span_id`。

原始事件以版本化 schema 写入 SQLite。常见密码、Token、Cookie、Authorization 和 API Key 会在入库前脱敏。

## 四、三个不能混淆的概念

实验中最重要的认识，是不能把“会话”“用户消息”和“LLM 调用”当成同一件事。

### 用户会话

用户在同一个 DeepTutor 对话页面连续交流，属于同一个 `conversation_id`。

### 消息轮次

用户发送一次消息并得到最终回答，是一个站长可读的消息轮次。

### 底层 LLM 调用

一次消息轮次可能触发多次模型调用。例如天气问题会先让模型决定调用搜索工具，再把搜索结果交给模型生成最终回复。因此一次用户消息可能对应两次甚至更多 LLM 调用。

Observer 的 Conversations 页面按消息轮次展示；中间步骤仍完整保存在 Traces 页面。

## 五、网关策略

系统支持三种动作：

- `log`：只记录命中，不修改请求。
- `redact`：替换敏感内容后再发送给 Provider。
- `block`：不把消息发送给 Provider，直接返回管理员阻断提示。

策略支持字符串包含和正则表达式匹配。在实验中，我们创建了两条规则：

1. 用户发送 `hello` 时阻断。
2. 用户询问服务器 IP 时阻断。

策略只应作用于真正的 `user.chat`。DeepTutor 自身的标题生成、推荐下一步等内部任务不能按用户消息处理，否则可能因为内部提示词包含敏感词而被错误阻断。

## 六、实验环境与方法

### 环境

| 项目 | 配置 |
| --- | --- |
| 审计服务器 | 临时 Linux 云服务器 |
| 被审计应用 | DeepTutor |
| LLM Provider | OpenRouter |
| 实验模型 | `openai/gpt-4.1-mini` |
| 入口代理 | Nginx |
| 出口观察与策略 | mitmproxy |
| Collector / API | FastAPI |
| 存储 | SQLite |
| 自动测试 | pytest |
| 部署方式 | Docker + 自动部署脚本 |

### 验证标准

每个实验至少检查四个结果：

1. DeepTutor 页面和 WebSocket 正常。
2. 用户操作没有因审计层而中断。
3. Conversations 页面能给站长阅读。
4. Traces 页面保留足够的原始信息。

## 七、实验结果

### 实验一：基础用户消息与回复

用户发送：

```text
34
```

模型回复：

```text
You entered "34." Could you please clarify what you would like to do...
```

Observer 成功记录用户消息、模型回复、模型名称、token 和约 2.9 秒的调用时延。这验证了最小聊天链路。

### 实验二：阻断 `hello`

用户发送 `hello` 后，网关没有继续访问 Provider，而是返回：

```text
This message was blocked by an administrator gateway policy.
```

事件记录了：

```text
policy_action = block
policy_rule   = Block hello
```

响应耗时约 35 毫秒，说明阻断发生在外部模型调用之前。

### 实验三：IP 询问策略误伤内部任务

最初的策略直接检查每个 OpenRouter 请求的最后一条 `user` 消息。DeepTutor 有一个内部任务会读取 Recent activity 并生成三个学习建议，其中包含历史会话标题“Requesting Specific Server IP Address Details”。结果内部任务也命中了 IP 规则。

这暴露了一个重要问题：在 OpenAI 兼容协议中，应用内部提示也经常使用 `role=user`，但它并不代表真实终端用户。

修复后调用被分类为：

```text
user.chat
internal.recommendations
internal.title
internal.unknown
```

只有 `user.chat` 会执行用户网关策略。内部任务保留在 Trace 中，但不会出现在站长 Conversations 列表。

### 实验四：带工具调用的天气查询

用户在同一个页面连续发送两条天气消息。Observer 最初记录了四次 LLM 调用：

| 用户消息 | 中间调用 | 最终调用 |
| --- | ---: | ---: |
| 查一下上海的天气 | 1 | 1 |
| 查一下上海的天气，用中文回复 | 1 | 1 |

四次调用属于同一个 `conversation_id`。中间调用用于决定和执行搜索工具，`assistant.message` 为空；最终调用包含面向用户的回答。

站长不应看到“四段对话”。修复后：

- Conversations 显示 2 个用户消息轮次。
- 每个轮次显示 **User sent** 和 **LLM replied**。
- Traces 仍显示全部 4 次模型调用。
- 完整上下文中保留搜索工具结果。

这证明审计系统既需要保留执行细节，也需要建立面向业务人员的语义投影。

### 实验五：中文响应乱码

天气回复首次展示时出现：

```text
ä¸æµ·å½åå¤©æ°...
```

原因是 UTF-8 字节被按 Latin-1 解码。修复分为两层：

1. mitmproxy 直接从原始响应字节按 UTF-8 解码，避免新数据损坏。
2. Conversations API 对历史字符串执行保守的可逆检测，仅在恢复后中文字符增加且原字符串含 C1 控制字符时纠正。

修复后历史记录无需改写原始事件，即可在人类视图中恢复为：

```text
上海当前天气如下……
```

### 实验六：Safari WebSocket 断开

Chrome 可以聊天，而 Safari 将 `/api/v1/ws` 当成普通 HTTP 请求并收到 404。最终定位到两个因素：

- 代理容器仍加载旧 Nginx 配置。
- Safari 缓存了热修复前的前端 chunk。

我们重建代理容器，并对修复后的静态资源增加缓存版本。验收结果为页面 `200`、WebSocket 握手 `101`。这说明运行时审计的部署层必须同时验证 HTTP、静态资源和 WebSocket，而不能只看容器健康状态。

## 八、站长最终看到什么

Observer 当前提供四个页面：

- **Overview**：LLM 调用数、Trace 数、错误率、平均延迟、模型和 token 使用。
- **Conversations**：用户身份、IP、浏览器、用户消息、LLM 回复、策略和延迟。
- **Traces**：内部任务、工具步骤和原始存储事件。
- **Policies**：创建、修改、启用、禁用和删除网关规则。

Conversations 是站长的默认业务视角。系统提示词、模型参数和 Provider 上下文被放在可展开技术详情中；Original stored event 只用于工程取证。

## 九、自动化部署与验证

项目提供统一入口：

```bash
deploy/deploy.sh
```

脚本自动完成：

1. 打包当前提交。
2. 上传服务器。
3. 构建带提交哈希的 Docker 镜像。
4. 更新 Observer 和 mitmproxy 插件。
5. 更新 DeepTutor Nginx 代理。
6. 保留 SQLite 数据卷。
7. 验证 Observer 健康接口、DeepTutor 页面和浏览器采集脚本。

同一脚本也被 GitHub Actions 调用。部署需要使用独立 SSH Key 和固定服务器主机指纹，密钥不进入仓库。

当前自动测试覆盖事件幂等入库、敏感字段脱敏、API 鉴权、策略 CRUD、浏览器身份、上下文投影、内部任务过滤、中间步骤过滤和历史 UTF-8 修复，共 `11` 个测试通过。

## 十、当前局限

### 1. 浏览器与模型请求仍是时间关联

当前版本用最近活跃浏览器的五分钟窗口关联 Provider 请求，并标记：

```text
client.identity.confidence = temporal
```

这适合单用户实验，不适合多用户并发。正式多用户版本必须从浏览器 WebSocket 入口生成签名 `interaction_id`，经后端传播到 Provider 请求，再以确定性 ID 关联。

### 2. 指纹不是身份认证

指纹可能变化或被伪造。用户账户 ID 应由业务系统产生，并在进入 Observer 前进行哈希或假名化。

### 3. 全量内容具有隐私风险

本实验环境明确开启完整上下文审计。生产环境必须增加：

- 管理员登录和角色权限。
- 租户隔离。
- 内容采集授权和告知。
- 数据保留周期和删除接口。
- 数据库加密与备份策略。
- 管理员访问审计。

### 4. 中间步骤识别仍需标准化

当前利用回复内容和调用类型区分中间步骤与最终回答。后续应定义统一的 `interaction_id`、`step_id`、`step_kind` 和 `is_final` 字段，适配不同 Agent 框架。

## 十一、结论

LLM 应用的运行时审计不能停留在“记录一次 API 请求”。真正有价值的系统必须同时理解浏览器用户、业务会话、消息轮次、Agent 步骤和底层模型调用，并为站长与工程师提供不同层级的视图。

DeepTutor 实验验证了无侵入代理方案的可行性：我们能够在不修改应用源码的情况下采集浏览器特征、完整上下文和模型回复，执行网关阻断，并通过独立控制台展示。但实验也证明，时间窗口关联和简单的 `role=user` 判断无法支撑真正的多用户系统。

下一阶段的重点不是继续增加更多 JSON 字段，而是实现请求级确定性关联、用户与租户模型、生产级权限和保留策略，让“用户发了什么，LLM 回了什么，中间发生了什么”成为一条可信、可查询、可治理的审计链路。
