# 演示脚本:Dify Enterprise on OpenShift

**主线一句话**:企业 AI 平台跑在 OpenShift 上,身份、存储、可观测性全部用平台自己的能力——不另买、不另装第三方产品。

- 时长:约 20 分钟(六幕)
- 需要:浏览器三个标签页 + 一个终端
- 账号:OpenShift `admin`、SSO 演示用 `user1`、Dify 的 `demo@dify.ai` / `dashboard@dify.ai`(密码另行提供,不写在这里)

**2026-09-26 在这套集群上完整彩排过一遍,每一步都截图确认。** 下文的数字来自那次彩排中 user1 在北京时间 22:35 问的「部署架构」,不是估算。

---

## 演示前 30 分钟:检查单

| # | 做什么 | 怎么确认 |
|---|---|---|
| 1 | **集群在运行**(RHDP 每晚自动停机) | `oc whoami` 有返回;连不上就去 RHDP 页面启动。提前一小时确认 |
| 2 | 依赖层全绿 | `./scripts/preflight-check.sh dify` → 33 passed。唯一的 WARN(LiteMaaS key 不含 embedding 模型)是预期的 |
| 3 | **插件 daemon 补丁在位** | `./scripts/fix-plugin-daemon-otlp.sh` 输出 *Nothing to do*。没有它,模型调用不会出现在 trace 里。Dify 做过 `helm upgrade` 后一定要重跑 |
| 4 | Demo 应用的 LLM 追踪已开 | Dify → Demo 应用 → 监测 → 追踪应用性能 → Phoenix 显示已启用 |
| 5 | **知识库内容和演示口径一致** | 知识库里那份 Markdown 写于迁移之前,有 4 个分段提到 MinIO,回答会说「MinIO」。要么把文档里的 MinIO 改成 ODF MCG 重新上传,要么用第二幕里给的那句话接住 |
| 6 | **预热**:问一个知识库问题 | API 重启后第一次检索 2.5 秒,预热后 0.2 秒。不要把冷启动放在台上 |
| 7 | 预热至少提前 5 分钟 | 新 trace 进搜索索引要 1–4 分钟(彩排实测约 3 分钟);这条预热对话就是台上的**备用 trace** |
| 8 | 打开三个标签页 | ① OpenShift 控制台(admin);② Dify 开发者界面(**无痕窗口**,第一幕用);③ Dify 管理后台 |

**准备好的问题**(都能命中知识库):「部署架构」「方案优势」「监控设计」。

**开场先说一句**:Dify 界面左上角有红色的「许可证还有 N 天到期」——「这是一个临时测试环境」,不然观众会以为出了问题。

---

## 第一幕 · 用企业身份登录(2 分钟)

**操作**:无痕窗口打开 `https://dify-console.apps.<集群域名>` → 点 **SSO 登录** → 跳到 Keycloak 登录页(页头显示 **SSO**)→ 输入 `user1` 和密码 → 回到 **Demo's Workspace**,右上角头像是 **U**。

**讲**:
- 登录走的是 **Red Hat build of Keycloak**,而且是 OpenShift 集群**自己登录用的同一个 realm**——一个身份源同时管平台和平台上的应用。
- 授权码流程**强制 PKCE(S256)**,关掉会直接登录失败,而不是悄悄降级。
- Dify 按**邮箱**匹配成员——身份以 Keycloak 为准。
- 登录被 Dify 审计日志记录(彩排:14:33:51 UTC,User1)。

---

## 第二幕 · 问一个知识库问题(2 分钟)

**操作**:点顶部 **探索** → **Demo** → 问「部署架构」。**记下提问时间**,第三幕要用。

> 从「探索」进,不要从「工作室」点应用——user1 是普通成员,工作室里点进去是编辑界面。

**讲**:这一次回答用到了——
- 文档原件存在 **ODF 对象存储**(MCG)
- 向量在 Qdrant,**嵌入模型跑在集群内**
- 对话模型通过 Dify 的插件调用,插件本身也是在集群内构建、推到 **OpenShift 内部镜像仓库**的

> **如果回答里出现「MinIO」**(检查单第 5 项没改文档时):「它说 MinIO,因为这份资料写于我们迁到平台存储之前——RAG 忠实于资料,资料也得跟着系统一起更新。」

---

## 第三幕 · 模型花了多久?(5 分钟,核心)

控制台:**Observe → Traces**,实例选 `dify-observability / dify-traces`,租户选 `dify`,时间范围 **Last 15 minutes**,点 **Show query**。

**A. 一次对话,一棵树**(首选)

```
{ span.openinference.span.kind = "LLM" }
```

列表里这条的名字是 **`langgenius/dify: apply_async/tasks.ops_trace_task.process_trace_tasks`**(不是 `llm`),约 11 个 span,**Duration 那一栏就是模型耗时**。点开:

```
Dify
├─ dataset_retrieval     263 ms    问题 + 检索到的段落(往下滚才看得到)
└─ message             12430 ms
   └─ llm              12430 ms    deepseek-flash,1813 + 2492 = 4305 tokens
```

**讲**:模型、耗时、token 在一张图里。token 数和 Dify 自己账上的**逐位一致**(彩排两次都一致)。这些数据发到的是**平台自己的 Tempo**——Dify 本来是要接 Langfuse、Phoenix 这类第三方平台的,我们把它指向了 OpenShift。

> 这几行的服务名显示为 **`dify-demo`**。Dify 的 Phoenix 导出器本身不设服务名(原来显示 `unknown`),是**平台的 collector 用项目名补上的**——「平台团队自己决定数据的样子,不用改应用」,可以顺势讲。耗时比 Dify 记录的略短约 0.2 秒(12.43 vs 12.61 秒),两次彩排都是这样。

**B. 从基础设施看同一次调用——以及一个看不见的调用**

```
{ name =~ ".*dispatch/llm/invoke" }
```

**一个新对话的第一个问题,会出现两条**:

| 列表里 | 是什么 | API 侧 | 插件 daemon 侧 |
|---|---|---|---|
| `langgenius/dify: POST`,2 spans | **生成回答** | 1001 ms ← 约等于**首个 token** | **11711 ms** = 完整生成 |
| `<root span not yet received>`,约 46 spans | **自动给对话起标题**(入口 `POST …/conversations/<id>/name`) | 2926 ms | 2925 ms |

**讲**:
- 回答是流式的,API 在收到第一段内容时就停止计时,只有 daemon 那边记录了完整生成;起标题不是流式,两边一样长。
- **问一个问题,模型被调了两次。** 回答一结束,Dify 在后台又调一次模型给对话起名字(左边栏的标题就是这么来的)——界面上完全看不出来,trace 让它现形。

**C. 「其中花在我们数据上的时间」**

```
{ name =~ ".*RetrievalService.retrieve" }
```

点开 22:35 那条(260.74 ms),**往下滚到第 63–69 行附近**:

```
retrieve                                  260.7 ms
├ embedding_search(向量)                 181.3 ms
│  ├ POST → plugin-daemon  问题转向量      103.3 ms
│  └ POST → Qdrant         向量搜索          6.9 ms
└ full_text_index_search(全文,并行)     124.3 ms
   └ POST → Qdrant         全文检索         67.6 ms
```

**讲**:向量搜索本身不到 10 ms,最耗时的是把问题转成向量。**慢的从来不是数据库。**

**D. 安全团队会问的一个问题**

```
{ span.http.url =~ "https://.*" }
```

时间范围改成 **Last 1 hour**。列表里名字是 `<root span not yet received>`(上游是没导出 trace 的前端网关)。点开 → **点 257.95 ms 的那一行 `GET` 展开属性** → `http.url = https://tmpl.dify.ai/apps?language=zh-Hans`。

**讲**:「这个 AI 平台背着你往外网发了哪些请求」——**每打开一次「探索」页,就访问一次** `tmpl.dify.ai`。这是 trace 替我们发现的、文档里没写的外网依赖。

---

## 第四幕 · 花了多少钱、健不健康(4 分钟)

**操作**:**Observe → Dashboards** → 下拉选 **Dify Enterprise** → 时间 **Last 1 hour**。刚才那次对话在三个面板上是同一时刻的一个台阶。

**讲**:
- **Tokens by model — cumulative**:台阶正好是 **4305**(29466 → 33771),和 Dify 的账**完全一致**
- **Answers by application**:回答次数,和 Dify 的消息记录逐条一致
- 每个指标都带 `tenant_id` / `app_id` / `model_name` 标签——**按租户计费(chargeback)的原料**
- 用的是 OpenShift 自带的 Prometheus,**没装任何新 operator**

> 读面板时注意两点:
> - **per 5 min 面板会显示约 4.8k**,比实际的 4305 多——`increase()` 会向窗口两端外推。要精确数字看 cumulative。
> - **Answers by application** 只统计回答(`type="message"`),等于对话里的回答次数——和 Dify 的消息记录逐条一致(6 = 6)。原始指标还把检索和起标题各算一次,所以面板做了过滤。

**操作**:**Observe → Alerting** → **警报规则** 标签 → 名称筛选 **Dify**。

**讲**:4 条规则,来源都是「用户」——采集中断、采集目标消失(chart 升级后最容易静默发生的故障)、回答太慢、某个工作空间 token 消耗异常。当前都未触发。

> 筛选是模糊匹配,列表下面会带出两条平台的 `etcdHighFsyncDurations`(d-i-f-y 恰好按顺序出现在名字里),说一句「下面两条是平台自带的」即可。

---

## 第五幕 · 这次请求到底做了什么(3 分钟)

**从 trace 出发,不要从报错出发。** 在第三幕 B 里点开「起标题」那条 trace,复制页面上的 **Trace ID**。

**操作**:**Observe → Logs**(租户 `application`,**Last 1 hour**),把查询整个换成(替换 ID):

```
{kubernetes_namespace_name="dify"} |= "<trace ID>" | json | line_format "{{ trimPrefix \"dify-dify-enterprise-\" .kubernetes_container_name }}  {{ trunc 140 .message }}"
```

结果是一行一条、开头是组件名的列表(彩排:9 行):

```
gateway         … handled request …
api             … HTTP Request: POST …
plugin-daemon   … "latency_ms":2925, … /dispatch/llm …
plugin-manager  …
enterprise      …
```

**讲**:
- **一个请求经过了 5 个组件**——网关、API、企业服务、插件管理、插件 daemon——一条查询全部拉出来。
- 网关那一行里有 `Traceparent: 00-<trace ID>-01`:**这条链路是从网关开始的**。
- plugin-daemon 那行记录的模型耗时 **2925 ms**,和 trace 里 daemon span 的 2925 ms 一致——**日志和追踪两个独立信号给出同一个数字**。

> 不加后面的 `| json | line_format …`,每行会显示日志平台外面那层完整信封(几千字符),台上没法看。

**可选:展示一条真实的报错**

```
{kubernetes_namespace_name="dify"} | json | level="error"
```

会看到 `Failed to detach context`,展开是完整的堆栈:`RuntimeError: Token … has already been used once`——**这就是一次对话被拆成几段 trace 的根因**。

> **不要把这条报错的 `trace_id` 贴去 Traces**——它属于生成结束后丢失的那一段,在 Tempo 里是 404,会打开空页面。

---

## 第六幕 · 它是怎么跑在平台上的(4 分钟)

**安全**(终端):

```bash
oc get pods -n dify -o custom-columns='POD:.metadata.name,SCC:.metadata.annotations.openshift\.io/scc'
```

彩排结果:**22 个 `anyuid`、1 个 `dify-nonroot`、1 个 `dify-sandbox`,0 个 `privileged`**。

**讲**:
- **没有一个组件跑 `privileged`**。Dify 自带模板给 sandbox 要的是 privileged,我们实测它只需要 `SYS_CHROOT`,给了一个专用的最小权限 SCC
- 集群级只需要**装一次 CRD**;Dify 团队只有 namespace 权限,**不需要 cluster-admin、不需要 SCC 管理权**
- 可观测组件用的全是**红帽支持的配置**:Tempo 开多租户、LokiStack 用 `1x.pico`——更小的规格能跑,但官方声明不支持

**存储**:块存储和对象存储都是 **ODF**。原来的 MinIO 社区镜像已经不再公开发布,换成平台存储后少了一个第三方依赖。

**交付**(终端):`oc get application -n openshift-gitops`,9 个应用全部 Synced/Healthy。**所有凭据在集群内生成,Git 仓库里一个密码都没有**——仓库本身是公开的。

---

## 被问到时,照实回答

| 问题 | 回答 |
|---|---|
| 登录页有 Register,谁都能注册? | 这个 realm 开着自助注册,但**注册了也进不了 Dify**:工作空间只认已存在的成员,会报 account not found。有风险的只有「管理后台自动创建系统用户」+ 自助注册同时打开,前者是关着的 |
| 22 个 pod 是 anyuid,是不是太宽了? | anyuid 只允许容器用镜像自带的用户(Dify 的镜像按官方要求以 root 运行),**不额外给任何 capability**;真正需要额外能力的 sandbox 单独用最小 SCC。但它是**整个命名空间**绑定的,之后放进这个命名空间的新组件也会被套上——我们的可观测组件因此放在了独立命名空间里 |
| 为什么基础设施 trace 里一次对话是好几段? | Dify 生成回答的线程不传递追踪上下文(日志里那条 `Failed to detach context`)。第三幕 A 那种视图(Phoenix)是一棵树,那是看单次对话的正确入口 |
| trace 都能收全吗? | 不能。约五分之一的 trace 到不了 Tempo,原因还没查到;采样率已从默认的 20% 调到 100% |
| token 计费准吗? | 回答用的 token 和 Dify 的账逐位一致。但**起标题那次模型调用的 token 不计入指标**——按指标计费会漏掉每个新对话的起标题成本,已提给 Dify |
| p95 延迟准吗? | 不太准。Dify 的耗时直方图用的是毫秒级的桶,10–25 秒之间没有刻度,p95 是插值。告警因此改用「超过 25 秒的占比」 |
| 对话内容会进 Tempo 吗? | 会。LLM trace 里有提示词、检索原文和回答,能读这个租户的人都看得到——这是客户环境要评估的数据治理问题 |
| 普通用户能看这些吗? | 仪表板目前只有 admin 能看;日志按命名空间权限授权 |
| 支持边界? | Dify Enterprise 在红帽目录里是 **Partner Validated**,而 Dify 的标准部署服务把 OpenShift 列在例外里——生产支持需要单独谈 |
| 升级 Dify 会破坏这些吗? | 会覆盖两处补丁:先用 `render-dify-values.sh` 重新渲染 values(**不要** `--reuse-values`),升级后重跑 `fix-plugin-daemon-otlp.sh` |

---

## 台上出岔子时

| 现象 | 原因 | 处理 |
|---|---|---|
| 集群连不上 | RHDP 夜间停机 | RHDP 页面启动;演示前一小时确认,别卡在最后几分钟 |
| `oc` 报 Unauthorized | 登录 token 过期 | 控制台右上角 → Copy login command |
| 第一次回答特别慢 | API 冷启动 | 提前预热(检查单第 6 项) |
| 回答里出现 MinIO | 知识库文档写于迁移前 | 第二幕里那句话;演示后更新文档 |
| Traces 里找不到刚才的对话 | 搜索索引延迟 1–4 分钟 | 用预热那条;或缩短时间范围;或从日志里拿 `trace_id` 按 ID 打开 |
| 提示「Not all matching traces are currently visible」 | 匹配数超过显示上限,列表不是按最新排 | 缩短时间范围、查询写具体;调大上限不保证包含最新 |
| 模型调用的 trace 没有 | 插件 daemon 补丁被 `helm upgrade` 覆盖 | 跑 `fix-plugin-daemon-otlp.sh`,之后的新对话才会有 |
| 从日志贴 trace_id 到 Traces 打开是空的 | 那段 trace 丢了(生成结束后的收尾) | 反过来:从 Traces 里存在的 trace 复制 ID 去查日志 |
| 日志每行是几千字符的 JSON | 查询没加 `| json | line_format` | 用第五幕的完整查询 |
| 仪表板下拉里没有 Dify Enterprise | 不是 admin 登录 | 用 admin |
| 仪表板面板空 | 重启后还没有请求 | 先问一个问题,半分钟后刷新 |
| SSO 报 account not found | 成员不存在,或没加入工作空间 | Dify 管理后台 → 成员 → 添加并分配工作空间 |

---

## 彩排状态(2026-09-26 完整走过一遍)

| 步骤 | 界面走过 | 依据 |
|---|---|---|
| 检查单 1–4 | ✅ | 终端执行 |
| 第一幕 SSO 登录 | ✅ | 截图 + 审计日志 14:33:51 User1 |
| 第二幕 知识库问答 | ✅ | 截图;消息记录 user1、4305 tokens;Qdrant 200 |
| 第三幕 A Phoenix 树 | ✅ | 截图;token 与消息记录一致 |
| 第三幕 B 模型调用 | ✅ | 截图;两条(回答 + 起标题)按 ID 核对 |
| 第三幕 C 检索 | ✅ | 截图;各段耗时按 ID 核对 |
| 第三幕 D 外网调用 | ✅ | 截图;URL 按 ID 核对 |
| 第四幕 仪表板 | ✅ | 截图;token 台阶与消息记录一致 |
| 第四幕 告警 | ✅ | 截图,4 条规则 |
| 第五幕 日志 | ✅ | 截图;9 行 5 个组件,可读格式 |
| 第六幕 终端命令 | ✅ | 原样执行 |
