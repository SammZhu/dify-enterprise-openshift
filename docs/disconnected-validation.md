# 离线安装验收记录

RHDP 自动化入口见 [Ansible 主安装指南](disconnected-install.md)；客户单机入口见 [客户 Quick Start](customer-quickstart.md)；其他拓扑见 [高级配置参考](disconnected-manual.md)。

用本清单为每个客户/交付包保留独立记录。当前入口执行原生 YAML 任务；历史 Python 部署记录和本地模拟测试都不能代替本次交付包的现场验收。

| 项目 | 现场记录 |
|---|---|
| 日期、执行人 | 待填写 |
| OpenShift 版本、context、架构 | 待填写 |
| 原始 Chart SHA256、交付包 SHA256 | 待填写 |
| `images.lock.yaml` SHA256、`image-map.json` 记录 | 待填写 |
| Registry / Python index / 模型端点 | 待填写，不记录密码 |
| 插件名称、版本、包 SHA256、Python 版本 | 待填写 |
| Dify 确认的离线 License 激活方式 | 待填写，不记录授权材料 |

## 当前平台与插件验证范围

**截至 2026-11-06，已验证的是 Dify 平台部署和基础验收；尚未验证任何具体插件的离线安装、运行或完整离线业务。** 现场范围为 RHDP OpenShift 4.20.8；通过项包括平台/数据层 Pod、内网镜像引用、四个数据库、Redis、认证 TLS S3 读写删除、HTTPS 入口，以及 Chrome 页面加载。License 激活与插件/模型/RAG 不在通过项内。

| 层次 | 需要具备什么 | 当前验证结论 |
|---|---|---|
| 插件能安装 | 指定版本的 `.difypkg`、构建镜像、匹配 Python ABI/平台的完整依赖，以及签名/构建/推送链路 | 已提供本地 Registry、wheel 索引和构建基础镜像等准备机制；**尚未实际验证某个插件的离线安装/构建，不能声称这一层已通过** |
| 插件能运行 | 版本兼容，配置、权限、证书正确，运行实例可调用 | **未验证**，须选定插件逐个测试 |
| 插件能离线完成业务 | 插件所需的模型、API、数据或其他服务在客户内网可用，实际业务不依赖公网 | **未验证**，须逐项核对外部依赖并实际执行 |

wheel 索引只提供已经放入介质的 Python 依赖文件，可以合法为空；它不会自动补齐缺失依赖，也不存放模型权重或提供模型服务。插件安装成功也不等于离线业务可用，例如仍需公网天气 API 的插件不能仅凭安装成功就认定能离线使用。

### 具体插件的测试记录

每个插件分别记录名称、版本、包摘要、Python ABI/平台、内网服务端点及依赖；不要用一个插件的结果推导其他插件。

- [ ] 能安装：在断网且不依赖既有构建缓存的条件下，完成包导入、签名校验、构建和内网镜像推送。
- [ ] 能运行：实际调用该插件，检查配置、权限、证书和运行实例。
- [ ] 能离线完成业务：完成真实业务请求，验证模型/API等依赖在内网可用，并检查边界日志中没有业务必需的公网请求。

以上是待执行项，不是当前已经通过的结果；保留各层实际结果，不能只填写一个笼统的“插件支持离线”。

## 前提和制品

- [ ] 核对源码、Chart、镜像访问授权、来源与校验值。
- [ ] Ansible transfer 阶段的目标校验（或底层工具 verify）接受 exported 包，拒绝 planned 或损坏包。
- [ ] 集群节点、Podman 主机与全部所需镜像架构一致。
- [ ] 集群节点、构建 Pod、运维主机均能解析真实 Registry DNS。
- [ ] Registry TLS、账号认证、持久化目录和 wheel 索引可用。
- [ ] 交付包不含 kubeconfig、Registry 密码、TLS 私钥或 License。

## 全新断网部署

- [ ] 使用全新命名空间/测试集群，清除测试前已有镜像/模型缓存的影响。
- [ ] 在网络边界禁止集群及 Podman 主机访问公网，同时保留 API、DNS、Registry、wheels、S3、模型等必要内网链路。Pod 的 NetworkPolicy 本身不能证明节点和宿主机已断网。
- [ ] Registry 从包内 OCI 启动，无外部镜像拉取。
- [ ] 导入日志无源 Registry 拉取；CA 派生镜像构建使用 `--pull=never --network=none`。
- [ ] 安装期间不访问 GitHub、公共 Helm 仓库或公共镜像仓库。
- [ ] 未创建 ArgoCD Application 或新的 Operator Subscription/CatalogSource。
- [ ] 原有 Registry CA 条目保留，集群仍能访问原有内部 Registry。
- [ ] Ansible check 阶段通过：运行 Pod Ready、镜像内部化、四个数据库、Redis PONG；RHDP 和客户单机 Playbook 还验证 TLS S3 和 HTTPS。外部对象存储拓扑需分别完成实际服务的认证读写删除和入口验收。
- [ ] 六个 Dify Route 可用，浏览器端也没有必要公网依赖。

## 功能

- [ ] 按 Dify 确认的离线方式激活 License；重启后仍有效。
- [ ] Dify UI 不展示可用的公共 Marketplace 入口；实际 API/worker ConfigMap 的 `MARKETPLACE_ENABLED` 为 `false`。
- [ ] 每个批准的 `.difypkg` 在完全断网条件下完成安装，签名校验保持有效。
- [ ] 记录每个插件的 Python ABI、全部依赖、构建 Pod、内网基础镜像和推送出的插件镜像。
- [ ] 首次插件构建时能从 HTTPS wheel 索引取得依赖，不依赖缓存、不下载公网制品。
- [ ] Kaniko 的 Registry CA 和 Python/uv/pip 的证书信任均通过实际请求验证。
- [ ] 插件 controller 创建的 builder/runner/gateway Pod 能拉取镜像并 Ready。
- [ ] 内网 LLM 普通回答、流式回答、所需工具调用成功。
- [ ] 内网 Embedding 或预置 Ollama 模型成功，不执行在线模型下载。
- [ ] 上传文档，S3 写入、读取、删除验证成功。
- [ ] 完成切分、Embedding、向量入库、知识库问答并检查引用来源。
- [ ] 工作流、文档解析及插件自身的外部端点均已审查并验证。
- [ ] 观察边界防火墙/DNS/代理日志，无完成必要功能所依赖的公网请求。

## 重复安装和恢复

- [ ] 同一个包重复安装，不旋转已有应用/数据库密钥，不清空数据和 PVC。
- [ ] 重启数据库、API、worker、插件 runner 后，原有数据和问答仍可用。
- [ ] 在无镜像缓存的工作节点调度新 Pod，验证 Registry 可重新拉取镜像。
- [ ] 重启 Podman 主机并恢复 Registry、wheels 服务，验证后续插件安装。
- [ ] 完成数据库、S3、Registry 与配置备份恢复演练；验证模型 provider 凭据仍可解密。
- [ ] 记录证书/认证/License 到期日期及续期职责。

最终结论填写“通过 / 部分通过 / 不通过”，逐项记录失败证据与处理人。未完成的 License、插件或 RAG 项不能以“Pod Running”代替。

## 清理与重装验证

操作入口见 [Ansible 清理与重装](disconnected-reset.md)。以下现场项须单独记录，不能用模拟测试代替：

- [ ] 预览只读，目标身份和数据范围正确；外来资源、错误确认值和身份变化被拒绝。
- [ ] 应用重置后命名空间消失、旧 PVC 删除、专用 S3 数据清空，Registry/认证保留。
- [ ] 完整重置后本项目三个容器、专用 unit 和 Registry 数据清空，原 mirror-registry/集群共享资源保留。
- [ ] PV 的 Delete/Retain 策略和后续存储回收状态已记录。
- [ ] 失败后按原 run ID 续跑；旧计划不能清理新建命名空间。
- [ ] 重装重新生成部署验收报告，数据层和 TLS S3/HTTPS 检查通过；License/业务功能另验。
