# 演示脚本:Dify Enterprise on OpenShift

**主线一句话**:企业 AI 平台跑在 OpenShift 上,身份、存储、可观测性全部用平台自己的能力——不另买、不另装第三方产品。

- 时长:约 20 分钟(六幕)
- 需要:浏览器三个标签页 + 一个终端
- 账号:OpenShift `admin`、SSO 演示用 `user1`、Dify 的 `demo@dify.ai` / `dashboard@dify.ai`(密码另行提供,不写在这里)

下文里的数字都来自这套集群上的真实对话(2026-09-26),不是估算。

---

## 演示前 30 分钟:检查单

| # | 做什么 | 怎么确认 |
|---|---|---|
| 1 | **集群在运行**(RHDP 每晚自动停机) | `oc whoami` 有返回;连不上就去 RHDP 页面启动 |
| 2 | 依赖层全绿 | `./scripts/preflight-check.sh dify` |
| 3 | **插件 daemon 补丁在位** | `./scripts/fix-plugin-daemon-otlp.sh` 输出 *Nothing to do*。没有它,模型调用不会出现在 trace 里。Dify 做过 `helm upgrade` 后一定要重跑 |
| 4 | Demo 应用的 LLM 追踪已开 | Dify → Demo 应用 → 监测 → 追踪应用性能 → Phoenix 显示已启用 |
| 5 | **预热**:问一个知识库问题 | API 重启后第一次检索要 2.5 秒,预热后 0.2 秒。不要把冷启动放在台上 |
| 6 | 预热至少提前 5 分钟 | 新 trace 要 1–4 分钟才能被搜到;这条预热对话就是台上的**备用 trace** |
| 7 | 打开三个标签页 | ① OpenShift 控制台(admin);② Dify 开发者界面(**无痕窗口**,第一幕用);③ Dify 管理后台 |

**准备好的问题**(都能命中知识库):「部署架构」「方案优势」「监控设计」。

---

## 第一幕 · 用企业身份登录(2 分钟)

**操作**:无痕窗口打开 `https://dify-console.apps.<集群域名>` → 点 **SSO 登录** → 跳到 Keycloak 登录页 → 输入 `user1` 和密码 → 回到 Dify 工作空间。

**讲**:
- 登录走的是 **Red Hat build of Keycloak**,而且是 OpenShift 集群**自己登录用的同一个 realm**——一个身份源同时管平台和平台上的应用。
- 授权码流程**强制 PKCE(S256)**,关掉会直接登录失败,而不是悄悄降级。
- Dify 按**邮箱**匹配成员——身份以 Keycloak 为准。

---

## 第二幕 · 问一个知识库问题(2 分钟)

**操作**:进入 Demo 应用,问「部署架构」。**记下提问时间**,第三幕要用。

**讲**:这一次回答用到了——
- 文档原件存在 **ODF 对象存储**(MCG),不是另装的 MinIO
- 向量在 Qdrant,**嵌入模型跑在集群内**
- 对话模型通过 Dify 的插件调用,插件本身也是在集群内构建、推到 **OpenShift 内部镜像仓库**的

---

## 第三幕 · 模型花了多久?(5 分钟,核心)

控制台:**Observe → Traces**,实例选 `dify-observability / dify-traces`,租户选 `dify`,点 **Show query**。

**A. 一次对话,一棵树**(首选)

```
{ span.openinference.span.kind = "LLM" }
```

点开最新一条:

```
Dify
├─ dataset_retrieval     326 ms    问题 + 检索到的段落
└─ message              7748 ms
   └─ llm               7748 ms    deepseek-flash,5499 + 1550 = 7049 tokens
```

**讲**:模型、耗时、token 在一张图里。token 数和 Dify 自己账上的**逐位一致**。这些数据发到的是**平台自己的 Tempo**——Dify 本来是要接 Langfuse、Phoenix 这类第三方平台的,我们把它指向了 OpenShift。

**B. 从基础设施看同一次调用**

```
{ name =~ ".*dispatch/llm/invoke" }
```

两个 span:API 侧 1154 ms ≈ **首个 token 出来的时间**;插件 daemon 侧 8892 ms = **完整生成时间**。

**C. 「其中花在我们数据上的时间」**

```
{ name =~ ".*RetrievalService.retrieve" }
```

预热后检索 0.2–0.4 秒,其中 Qdrant 只占 70–120 ms。**慢的从来不是数据库。**

**D. 安全团队会问的一个问题**

```
{ span.http.url =~ "https://.*" }
```

「这个 AI 平台背着你往外网发了哪些请求」——会找到 Dify 在打开「探索」页时访问 `tmpl.dify.ai`。这是 trace 替我们发现的、文档里没写的外网依赖。

> **刚问的那次搜不到?** 新 trace 要 1–4 分钟才进搜索索引。先打开**预热那条**讲,或把时间范围缩短到最近 15 分钟再搜。

---

## 第四幕 · 花了多少钱、健不健康(4 分钟)

**操作**:**Observe → Dashboards** → 下拉选 **Dify Enterprise**。

**讲**:
- 最上面是**重启以来的累计值**:按模型的 token、按应用的请求数
- **按工作空间的 token**——每个指标都带 `tenant_id` / `app_id` / `model_name` 标签,这就是**按租户计费(chargeback)的原料**
- 用的是 OpenShift 自带的 Prometheus,**没装任何新 operator**

**操作**:**Observe → Alerting**,找 `Dify` 开头的 4 条规则。

**讲**:采集中断、采集目标消失(chart 升级后最容易静默发生的故障)、回答太慢、某个工作空间 token 消耗异常。

> ⚠️ **彩排未完成**:告警页面只通过 API 验证过(4 条规则已加载,投递到 Alertmanager 已用测试告警证明),**控制台界面还没走过**。

---

## 第五幕 · 这次请求到底做了什么(3 分钟)

**操作**:**Observe → Logs**,查询:

```
{kubernetes_namespace_name="dify"} | json | level="error"
```

从任意一行里复制 `trace_id`,再查:

```
{kubernetes_namespace_name="dify"} |= "<trace_id>"
```

**讲**:同一个请求在 **API 和企业服务两个容器**里留下的日志,一次查出来。反过来,把这个 `trace_id` 贴到 Traces 里,直接打开这次请求的 trace——**日志和追踪在同一个平台里互相跳转**。

> ⚠️ **彩排未完成**:日志页面只通过 Loki API 验证过(11 个容器的日志、trace_id 反查 5 行跨 2 个容器),**控制台界面还没走过**。

---

## 第六幕 · 它是怎么跑在平台上的(4 分钟)

**安全**(终端):

```bash
oc get pods -n dify -o custom-columns='POD:.metadata.name,SCC:.metadata.annotations.openshift\.io/scc'
```

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
| 22 个 pod 是 anyuid,是不是太宽了? | anyuid 只允许容器用镜像自带的用户(Dify 的镜像按官方要求以 root 运行),**不额外给任何 capability**;真正需要额外能力的 sandbox 单独用最小 SCC。但它是**整个命名空间**绑定的,之后放进这个命名空间的新组件也会被套上——我们的可观测组件因此放在了独立命名空间里 |
| 为什么基础设施 trace 里一次对话是好几段? | Dify 生成回答的线程不传递追踪上下文。第三幕 A 那种视图(Phoenix)是一棵树,那是看单次对话的正确入口 |
| trace 都能收全吗? | 不能。约五分之一的 trace 到不了 Tempo,原因还没查到;采样率已从默认的 20% 调到 100% |
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
| 第一次回答特别慢 | API 冷启动 | 提前预热(检查单第 5 项) |
| Traces 里找不到刚才的对话 | 搜索索引延迟 1–4 分钟 | 用预热那条;或缩短时间范围;或从日志里拿 `trace_id` 按 ID 打开 |
| 提示「Not all matching traces are currently visible」 | 匹配数超过显示上限,列表不是按最新排 | 缩短时间范围、查询写具体;调大上限不保证包含最新 |
| 模型调用的 trace 没有 | 插件 daemon 补丁被 `helm upgrade` 覆盖 | 跑 `fix-plugin-daemon-otlp.sh`,之后的新对话才会有 |
| 仪表板下拉里没有 Dify Enterprise | 不是 admin 登录 | 用 admin |
| 仪表板面板空 | 重启后还没有请求 | 先问一个问题,半分钟后刷新 |
| SSO 报 account not found | 成员不存在,或没加入工作空间 | Dify 管理后台 → 成员 → 添加并分配工作空间 |

---

## 彩排状态

| 步骤 | 控制台界面走过 | 依据 |
|---|---|---|
| SSO 登录 | ✅ | user1 登录成功,审计日志有记录 |
| 知识库问答 | ✅ | 多次,Qdrant 访问日志 200 |
| Traces:Phoenix 树 | ✅(数据) | 在 Tempo 里按 ID 取到;界面上打开这一条还没截图确认 |
| Traces:检索瀑布图 | ✅ | 控制台截图确认 |
| 仪表板 | ✅ | 控制台截图确认(10 个面板) |
| **告警页面** | ❌ | 仅 API 验证 |
| **日志页面** | ❌ | 仅 Loki API 验证 |
| SCC 命令、GitOps 命令 | ✅(终端) | 两条命令原样执行过 |

**演示前请把 ❌ 两项在控制台里走一遍。**
