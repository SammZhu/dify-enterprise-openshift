# 离线安装验收记录

用本清单为每个客户/交付包保留独立记录。脚本测试通过不代表下面的现场项目已通过。

| 项目 | 现场记录 |
|---|---|
| 日期、执行人 | 待填写 |
| OpenShift 版本、context、架构 | 待填写 |
| 原始 Chart SHA256、交付包 SHA256 | 待填写 |
| `images.lock.yaml` SHA256、`image-map.json` 记录 | 待填写 |
| Registry / Python index / 模型端点 | 待填写，不记录密码 |
| 插件名称、版本、包 SHA256、Python 版本 | 待填写 |
| Dify 确认的离线 License 激活方式 | 待填写，不记录授权材料 |

## 前提和制品

- [ ] 核对源码、Chart、镜像访问授权、来源与校验值。
- [ ] `offline.py verify` 接受 exported 包，拒绝 planned 或损坏包。
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
- [ ] `offline.py check` 通过：Pod Ready、镜像内部化、四个数据库、Redis PONG。
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
