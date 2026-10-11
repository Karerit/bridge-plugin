# astrbot_plugin_qq_bridge

把 [qq-bridge](https://github.com/)（Node.js + DeepSeek Harness + SnowLuma 那套）的**群聊仿真能力**
移植成 AstrBot 插件。QQ 连接交给 AstrBot 的适配器，大模型改走 AstrBot 的 Provider，
原来的 MCP 工具面变成插件注册的 LLM 工具，控制台换成 AstrBot WebUI + `/qq` 指令。

---

## 一、它做什么

核心语义与 qq-bridge 一致：**AI 自己决定说不说话**。

- 收到消息后，插件把消息记进会话缓冲，再按**唤醒门控**决定这条要不要交给 AI；
- 交给 AI 时，插件调用 `context.tool_loop_agent()` 起一个带工具的 Agent 回合；
- 模型的**文本输出只算思考、不会自动发出去**，发言必须调用 `qq_send_message` / `qq_reply`；
- 不想说话就调用 `qq_dive` 收尾；
- 回合结束后插件 `event.stop_event()`，阻止 AstrBot 默认对话流程再回一次（否则会重复发言）。

在此之上保留了原项目的这些人设化能力：人设卡注入、群黑话学习与确认、表情包收藏、
语音库、拍一拍、轻量记忆、用量统计（走 AstrBot 自带的）、冷场主动开口。
记忆另配了一个**管理员查看页**（浏览器打开，默认 `http://127.0.0.1:8899/`）。

---

## 二、安装

### 方式 A：WebUI 上传 zip（推荐）

1. 打开 AstrBot 管理面板 → **插件** → 右上角上传按钮；
2. 选择 `astrbot_plugin_qq_bridge.zip`；
3. 安装完成后在插件列表里找到「QQ 群聊仿真桥接」，确认已启用。

zip 里是**单个顶层目录** `astrbot_plugin_qq_bridge/`，AstrBot 会自动展平并安装到
`data/plugins/astrbot_plugin_qq_bridge/`。请勿在 zip 根目录再放别的文件（会让校验失败）。

### 方式 B：手动放目录

把解压出来的 `astrbot_plugin_qq_bridge/` 整个放进 `AstrBot/data/plugins/`，然后重启 AstrBot
或在插件页点「重载插件」。

### 前置条件

| 项 | 要求 |
| --- | --- |
| AstrBot | **>= 4.5.7**（本插件用了 `context.tool_loop_agent` / `llm_generate`，这两个是 4.5.7 引入的） |
| 平台适配器 | aiocqhttp（NapCat / Lagrange / SnowLuma 等 OneBot v11 实现都行） |
| 模型 | 在 AstrBot 里配好至少一个**对话模型**，且该模型支持 function-calling |

> 本插件不需要额外第三方 Python 包，`requirements.txt` 是空的。
> 原项目里的 SnowLuma 桥接层、DSH 会话、控制台网页都不再需要——这些能力由 AstrBot 本体提供。

---

## 三、快速开始

1. **配模型**：AstrBot → 服务提供商 → 添加一个对话模型（DeepSeek / OpenAI 兼容 / Ollama 都行）。
   建议挑支持工具调用的模型，否则 Agent 回合只能靠兜底文本回复。
2. **配插件**：插件页点进「QQ 群聊仿真桥接」→ 配置。
   - `wake.names`：填机器人的群名片/昵称，被叫到就唤醒；
   - `persona.roleCard`：填 `小鲸鱼`（内置人设卡）或留空；
   - `access`：想只让机器人服务特定群，把群号填进 `access.groups` 并把 `allowAllWhenEmpty` 关掉。
3. **发消息试**：在群里 @ 它一下，或直接叫名字。
4. **看状态**：群里发 `/qq 状态`。
5. **看记忆**（可选）：浏览器打开 `http://127.0.0.1:8899/`，或群里发 `/qq 记忆 全部`（仅管理员）。

想看它有没有在动、为什么不动，把日志级别调到 INFO，搜 `[qq-bridge]`。

### 它不回消息？按这个顺序排查

1. **上一轮还没跑完。** 模型调完工具后往往还要再问一次模型，本地模型这一步可能好几秒到几十秒；
   这段时间里你发的消息旧版会被直接丢掉，表现就是「明明在线却不回」——尤其是刚看到
   `qq_dive` 返回「已收尾」之后紧接着发的那条（「收尾」只是告诉模型别再说话了，
   本轮循环并没有真的结束）。**v0.1.5 起改为排队**：当前回合结束后自动补跑一轮，
   不会再丢。群里发 `/qq 状态` 看「回合排队」那行，排队次数不为 0 就说明发生过，且已经补跑。
2. **没命中唤醒门控。** 默认 `wake.mode=mention`：只有 @ 它、回复它的消息、叫它名字
   （`wake.names`）或带问号的提问才会唤醒。想让它每条都参与，把 `wake.mode` 改成 `anyMessage`。
3. **模型自己选择潜水。** 这是设计行为：模型会调 `qq_dive` 表示「这轮没什么要说的」。
   日志里搜「潜水」能看到它给的理由（`reason` 只进日志，不发到群里）。

### 同一句消息被回复两遍以上？

v0.1.6 修的就是这个。根子在「一轮里模型看到的历史」：

1. **补跑把上一轮已经看到的消息又跑了一遍。** 上一轮还在跑时进来的消息会排队补跑，但如果那一轮
   其实已经看到它了（模型用 `qq_wait_for_messages` 把它等进来了），再补跑就是第二次回复。
   现在补跑前会比对「模型已经看到的最大消息序号」，已看到就跳过；`/qq 状态` 里会多出一项
   「跳过重复 N 次」。
2. **模型回头再回一遍旧消息。** 快照里混着已经回过的旧消息，没有标记就容易再回一遍（冷场主动
   开口时尤其明显）。现在快照里**带【新】的才是本轮刚到的新消息**，提示词里也明确「同一条消息
   只回一次」，主动模式的说明改成「这是开新话题的机会，不是回头回复历史消息」。

另外，同一条消息如果被平台重复投递（重连 / 重放），插件会识别并跳过。判定要求
「消息 id、发言人、内容都相同，且时间挨得很近」四个条件同时成立——只凭消息 id 判断太危险，
万一某个适配器的 id 不唯一，真正的新消息会被当成重复吞掉，那比重复回复严重得多。

---

## 四、功能对照表（qq-bridge → 本插件）

| qq-bridge 的部件 | 本插件里的对应物 | 说明 |
| --- | --- | --- |
| SnowLuma OneBot 连接（`src/snowluma-conn.js`） | AstrBot 的 aiocqhttp 适配器 | 不再需要 SnowLuma 与 WS token 自愈 |
| DSH 会话 + 预设提示词（`dsh-client.js` / `preset-prompt.js`） | `context.tool_loop_agent()` + `qbridge/prompt.py` | 预设提示词按 AstrBot 工具面重写 |
| MCP 工具面（`mcp-snowluma-safe.js`，30 个工具） | `@filter.llm_tool` 注册的 20 个工具 | 见第五节 |
| 桥接控制台（3100 网页） | AstrBot WebUI 插件配置 + `/qq` 指令 | 不再单独跑一个网页 |
| 人设卡 `roles/*.md` | `roles/*.md` | 原样保留（含 2 张卡） |
| 白名单 `allow` / `deny` | `access.*` 配置 | 行为一致 |
| 唤醒配置 `socialV2.wake` | `wake.*` 配置 | 改为事件驱动门控 |
| 黑话学习 `slang-learner.js` | `qbridge/slang.py` | 保留「候选→管理员确认」状态机 |
| 表情库 `sticker-lib.js` | `qbridge/stickers.py` | 改为插件自己攒收藏（不再依赖 SnowLuma 接口） |
| 语音 `voice-core.js` | `qbridge/voice.py` | 目录扫描 + 限流；不再做 ffmpeg 转码与响度归一 |
| Token 账本 `token-ledger.js`（自建 + 计价） | `qbridge/usage.py`，写进 **AstrBot 自带的用量统计** | 不再自建账本，也没有金额（AstrBot 只记 token） |
| 主动消息 `social.proactive*` | `proactive.*` 配置 + 后台任务 | 保留概率式主动开口 |
| （原项目无） | `qbridge/memview.py` + `webui/memory.html` | 管理员查看页：浏览器里看全部会话的记忆，默认端口 8899 |
| 消息转发解析 `forward.js` | `qq_get_forward_msg` | **受限**：AstrBot 适配器没给转发正文接口，只能返回 id |
| 图片理解 `vision` | `qq_get_message_images` + 模型多模态 | 图片进上下文交给模型 |
| `md-to-plain.js` | `qbridge/textutil.py` | 逻辑一致 |
| `harden-state-acl.mjs` 等 Windows 加固脚本 | 不适用 | Linux/容器权限模型不同 |
| `console` / `setup-dsh.mjs` / Docker 那套 | 不适用 | AstrBot 自带部署与依赖管理 |

---

## 五、工具面（模型可调用）

| 工具 | 作用 |
| --- | --- |
| `qq_send_message` | 发言（`messages` 传数组即分条，可 @ 或引用） |
| `qq_reply` | 引用某条消息回复 |
| `qq_dive` | 收尾、不说话（潜水） |
| `qq_wait_for_messages` | 等新消息（`purpose=reply` 时按静默补足，已安静就立即返回） |
| `qq_get_recent_messages` | 看最近记录（带消息 id 与 QQ 号） |
| `qq_get_active_members` | 看最近谁在说话（取 userId 用） |
| `qq_get_message_images` | 取某条消息的图，放进上下文 |
| `qq_get_forward_msg` | 查合并转发（**仅返回 id**，见限制） |
| `qq_send_poke` | 拍一拍 |
| `qq_list_stickers` / `qq_send_sticker` / `qq_sticker_note` / `qq_collect_sticker` | 表情包收藏、发送、备注 |
| `qq_list_voices` / `qq_send_voice` | 语音库与发语音 |
| `qq_slang_query` / `qq_slang_submit` | 查黑话 / 提交候选 |
| `qq_memory_append` / `qq_memory_query` | 轻量记忆 |
| `qq_token_usage` | 查本会话累计 token（含面板口径与写入状态） |

> 工具是注册到 AstrBot 全局工具表里的，所以 `/tool ls` 能看到它们。
> 如果你希望 AstrBot 默认对话流程**不要**暴露这些工具，用 `/tool off qq_send_message` 之类的
> 命令关掉即可；本插件自己的 Agent 回合是显式传入工具集，不受影响。

---

## 六、指令

| 指令 | 作用 |
| --- | --- |
| `/qq` 或 `/qq 状态` | 看总开关、唤醒模式、人设、白名单、黑话/表情数量、用量统计开关 |
| `/qq 开` / `/qq 关` | 总开关 |
| `/qq 人设` | 列出人设卡；`/qq 人设 小鲸鱼` 切换；`/qq 人设 无` 清空 |
| `/qq 黑话` | 看候选与统计；`/qq 黑话确认 <词>` 启用；`/qq 黑话删除 <词>` |
| `/qq 表情` | 看收藏；`/qq 表情备注 <id> <备注>`；`/qq 表情删除 <id>` |
| `/qq 用量` | 本会话累计 token + 面板口径 + 写入状态（自查用量有没有进面板） |
| `/qq 唤醒 <mention\|probability\|anyMessage> [概率]` | 改唤醒模式 |
| `/qq 记忆` / `/qq 记忆清空` | 看 / 清本会话记忆 |
| `/qq 记忆 全部` | **仅管理员**：所有会话的记忆概览（含查看页地址） |
| `/qq 重置` | 清空消息缓冲 |

---

## 七、数据放在哪

全部落在 `data/plugin_data/astrbot_plugin_qq_bridge/`（**不在插件目录里**，重装插件不会丢）：

```
slang.json     黑话库（含候选与确认状态）
stickers.json  表情收藏
memory.json    轻量记忆（按会话分桶）—— 记忆查看页读的就是它
runtime.json   预留
voices/        语音库：把 wav 放进来即可被 qq_list_voices 看到
```

**用量不在这里。** 本插件不自建账本，而是把每轮 token 用量写进 AstrBot 自己的数据库
（`provider_stats` 表），到 WebUI「**数据与日志 → 统计**」看：模型调用趋势、调用排名、
按会话的用量都在那一页，时间范围是最近 1/3/7 天。

### 用量是怎么进到面板里的

这里有个容易踩的坑，值得说清楚：

AstrBot 自己记账的位置只在它**内部 agent** 那条路径上（`insert_provider_stat` 全仓库只有那一处调用）。
用 `yield event.request_llm(...)` 驱动对话的插件，用量是 AstrBot 自动记的，什么都不用做；
而**本插件为了保住「模型文本只算思考、不自动发出」的语义，自己调 `context.tool_loop_agent()` 跑回合**，
这条路径 AstrBot 不会替它记账——所以必须自己写。

于是每轮结束后插件做两件事，和 AstrBot 内部那条路完全一致：

1. `conversation_manager.update_conversation(token_usage=...)`（有对话时）；
2. `db_helper.insert_provider_stat(..., agent_type="internal")` —— 用 `astrbot.core.db_helper`
   这个单例写，保证和面板读的是同一张表；`agent_type` 必须是 `"internal"`，因为统计接口的查询
   带了这个过滤条件。

**关键一点：即使 provider 没回报 token（usage 为空），也照样写一行。** AstrBot 自己就是这么做的，
面板的「调用次数」靠的就是这些行。早期版本在 token 为 0 时直接跳过，结果是只要模型端点不回
usage，插件的流量在面板上就完全不存在。

同理，**模型调用失败也会写一行**（`status="error"`、token 记 0）。AstrBot 内部那条路同样如此；
如果不写，模型端点不通时面板上连「这个插件跑过」都看不到，根本没法排查。

用 `/qq 用量` 可以直接自查：

```
【本会话用量｜AstrBot 统计口径】…        ← 该会话累计
【数据与日志 → 统计｜最近 1 天】调用 N 次 / X tokens   ← 面板上能看到的那部分
用量写入：成功 N 次｜失败 M 次（错误原因）  ← 写入是否真的成功
```

如果这里显示「成功 0 次」，说明还没跑过 Agent 回合（没有用量产生）；如果显示失败，错误原因
会直接写出来。让 AI 调 `qq_token_usage` 得到的是同样的内容。

---

## 八、记忆是怎么工作的

1. **按会话分桶。** 记忆存在 `memory.json`，键是会话（`aiocqhttp:GroupMessage:123456`），
   另有一个 `global` 桶。`qq_memory_query` 读「本会话 + 全局」，不会串到别人的会话。
2. **会话记忆会进系统提示词。** 每轮组提示词时把「本会话 + 全局」的记忆注入成【你的记忆】块。
   早期版本这里写死了空会话名，结果只有全局记忆被注入——会话记忆等于「记了也用不上」。
3. **记忆为空时也保留【你的记忆】块**，里面说明当前是空的、可以用 `qq_memory_append` 记。
   否则模型不知道有记忆这回事，永远不会去写，记忆就一直是空的（冷启动死角）。
4. **`qq_memory_query` 的返回分三种情况**：命中（列出条目）、关键词没匹配（会说明「现有 N 条」）、
   记忆为空（说明还没记过）或记忆功能被关闭（说明是配置项）。不会一律回一句「没有匹配的记忆」。

---

## 九、记忆查看页（管理员）

记忆原本只能在 QQ 里用 `/qq 记忆` 看**当前会话**的那一份。要总览全部会话，有两种入口：

**1. 浏览器页面（默认 `http://127.0.0.1:8899/`）**

- 按会话分组列出所有记忆，带会话标签（`群 123456` / `私聊 88888`）、条目数、最近时间；
- 支持关键词搜索、导出 JSON（`/api/memory/export`）；
- 插件启动时自动拉起，`/qq 状态` 会显示地址；改配置后重载插件生效；
- 只读。要能在页面上删记忆，把 `memview.allowDelete` 打开（默认关）。

**打不开怎么办（`ERR_EMPTY_RESPONSE` / `ERR_CONNECTION_REFUSED`）**

查看页只监听 `memview.host` 指定的地址，默认 `127.0.0.1` —— 那是**运行 AstrBot 的那台机器
（或那个容器/虚拟机）自己的回环地址**：

- AstrBot 直接跑在 Windows 上：浏览器打开 `http://127.0.0.1:8899/` 即可。
- AstrBot 跑在 Docker / WSL2 里：容器内的 `127.0.0.1` **不是**宿主机的 `127.0.0.1`。宿主机浏览器
  连过来时，连接会被接受、然后**不带任何数据地关掉**——浏览器报的就是 `ERR_EMPTY_RESPONSE`。
  解决：把 `memview.host` 改成 `0.0.0.0`，设一个 `memview.token`（非回环地址不设 token 插件会拒绝
  启动），Docker 再把端口发布出来（`-p 8899:8899`），然后访问
  `http://127.0.0.1:8899/?token=你设的token`。

**判断请求到底有没有到达插件**：发 `/qq 状态`，看「记忆查看页」那行末尾有没有「收到空连接 N 次」。
有 → 连接确实到了插件（日志里会有对应的 warning 行）；一直是 0 → 连接根本没到插件，问题在网络
边界（容器端口、代理、防火墙），不在插件里。

**2. QQ 指令 `/qq 记忆 全部`**（仅管理员，走 AstrBot 的 `event.is_admin()`）

列出每个会话的条目数与最近时间，并给出查看页地址。非管理员会被明确拒绝。

### 安全默认值

这个页面能读到所有人的记忆，所以默认值偏保守：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `memview.enabled` | `true` | 关掉就不起服务 |
| `memview.host` | `127.0.0.1` | **只监听本机**，同一网络里的其他人打不开 |
| `memview.port` | `8899` | 监听端口 |
| `memview.token` | 空 | 访问令牌 |
| `memview.allowDelete` | `false` | 页面只读 |

**把 `host` 改成 `0.0.0.0`（或其他非回环地址）时，如果 `token` 还是空的，插件会拒绝启动**，
并在日志里说明原因——一个能被全网读到的记忆库不叫「管理员可见」，叫「所有人可见」。
设置了 token 之后，页面和接口都需要带 `?token=xxx`（也支持 `X-QQBridge-Token` 请求头）。

Docker 部署要额外注意：容器里的 `127.0.0.1` 不是你的电脑，想让宿主机打开页面得
`-p 8899:8899`（或改 `host` 为 `0.0.0.0` 并设 token）。

### 为什么不用 AstrBot 自带的插件页面

AstrBot 从较新版本起支持插件在 WebUI 里放自己的页面（`views/` + `context.register_web_api()`），
但那套机制是后来才有的（`astrbot/api/web.py` 在 4.22 及以前不存在），而本插件声明支持
`>=4.5.7`。为了让这个功能在任何版本上都能用，插件自带了上面这个小服务，只依赖标准库。

---

## 十、已知限制与差异

请先读这一节，再判断它是不是你要的东西。

1. **没有在真实 AstrBot 实例上跑过。** 开发环境里没有可运行的 AstrBot，所以采用的办法是：
   按 AstrBot 仓库源码逐条核对 API 签名，然后用一套桩件把插件真正加载起来跑了 163 项功能测试
   （黑话状态机、表情收藏、用量写入与面板口径读回、数据库句柄优先级、成功与失败回合都记账、会话记忆注入提示词、记忆查询的三种返回、@ 与引用真的挂上消息链、dive 后不被兜底覆盖、回合进行中进来的消息排队并在回合结束后补跑、已被上一轮看到的消息不再补跑、快照标出【新】消息、重复投递只处理一次、主动回合给用户回合让路、主动回合与黑话抽取的用量、主动消息过白名单、记忆快照与删除、查看页的 HTTP 路由与鉴权、
   消息缓冲与等待语义、唤醒判定、20 个工具方法、指令）。
   语法、schema、工具 docstring 解析都过了校验。但**首次装进真实实例仍可能有意外**，
   报错请把日志发我。
2. **「未读 / 已读水位」没有对应物。** 原项目由桥接主动唤醒 AI、AI 用工具读未读、用
   `qq_mark_read` 提交已读水位；AstrBot 是事件驱动、每条消息直接送达，所以这套概念被取消，
   改为「消息入缓冲 + 唤醒门控」。原来依赖已读水位的精细行为（比如只把看过的消息标已读）
   在 AstrBot 上无从实现。
3. **合并转发只能拿到 id，看不到正文。** AstrBot 的消息组件里没有转发正文接口，
   `qq_get_forward_msg` 会如实说明拿不到内容，不会编造。
4. **主动回合的工具调用是「无事件」的。** 冷场主动开口时没有 `AstrMessageEvent`，
   插件用 ContextVar 把目标会话传给工具，所以主动回合里 `qq_send_message`、`qq_send_poke`、
   `qq_send_sticker`、`qq_send_voice` 都能用；但依赖 event 的细节（如 `event.get_sender_id()`）
   拿不到，代码里已按此处理。
5. **语音不做转码。** 原项目可以调 ffmpeg 转码、探测时长、做响度归一；这里只做目录扫描、
   大小校验与限流。AstrBot 的语音组件目前对 wav 支持最好，非 wav 会提示先转码。
6. **用量统计的口径有四点要知道。** ① 统计页的接口只读 `agent_type == "internal"` 的记录，
   所以插件写进去时用的就是这个取值，否则数据落了库也不会显示在页面上；② AstrBot 的用量模型
   只有 token（`input_other` / `input_cached` / `output`），没有价格字段，所以本插件不再提供
   「成本」——那个数字 AstrBot 算不出来，硬报就是编的；③ 插件的回合走 `tool_loop_agent()`，
   公开接口只暴露最后一步的 usage，所以多步工具调用的一轮记的是那一步的用量（偏小）；
   ④ provider 完全不回报 usage 时 token 记 0，模型调用直接失败时 status 记 `error`——
   **这两种情况那一行仍然会写进去**，面板上至少能看到调用次数。
   用 `/qq 用量` 能自查写入是否成功。
7. **提示词每轮都带会变的内容，可能吃不到服务端缓存。** 系统提示词里的【群黑话】【表情包收藏】
   【你的记忆】会随库内容变化，而 AstrBot 官方文档明确指出：把每轮都变的文本塞进 system_prompt
   会破坏模型服务端的提示词缓存，可能明显抬高成本与首 token 延迟。要压成本就把 `slang.injectMax`、
   `stickers.injectMax`、`memory.injectMax` 调小，或把这几项直接关掉。
8. **主动消息与黑话学习都会先过一次白名单。** 消息缓冲记的是**全部**收到的消息（否则上下文不完整），
   但主动开口、黑话学习这两个「自己发起」的动作会再用 `access.*` 过滤一遍——被拉黑或不在白名单的
   会话既不会收到主动消息，它的聊天记录也不会被拿去学黑话。
9. **兜底发言默认开启。** 原项目严格规定「文本输出不发送」。但模型不听话（尤其不支持
   function-calling 的模型）时会完全没反应，所以 `agent.fallbackSendThinking` 默认为 `true`：
   整轮没调用任何发言工具时，把模型的文本当回复发出去。要严格照原语义就在配置里关掉。
10. **本插件会接管它管辖会话的回合。** 命中白名单的会话里，消息由本插件的 Agent 处理，
   AstrBot 默认对话流程不会参与。想让某个群走默认流程，就别把它放进白名单。
11. **记忆查看页是插件自己起的服务，不共享 AstrBot 面板的登录态。** 也就是说它的「管理员」
   由 `memview.host` + `memview.token` 决定，而不是 AstrBot 的账号。默认只监听回环地址时
   只有本机能访问，够用；要远程访问就必须自己设 token（不设会拒绝启动）。页面的写操作
   只有删除，且默认关闭。

---

## 十一、和之前那套 Docker 工程的关系

`dsh-docker/`、`qq-bridge-docker/`、`dsh-qq-bridge/` 那三套是**跑原版 Node.js 桥接**的部署工程，
和本插件是两条独立路线，不要混着用：

| 你想要的 | 用哪个 |
| --- | --- |
| 完全按原项目跑（DSH + SnowLuma + 桥接控制台） | 那三套 Docker 工程 |
| 在 AstrBot 里做同样的群聊仿真，不额外跑服务 | **本插件** |

本插件不读原项目的 `config.json`、`state/`、`roles/`（人设卡是复制过来的一份），
也没有把原项目的历史数据搬过来。
