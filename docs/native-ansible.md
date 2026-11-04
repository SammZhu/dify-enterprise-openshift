# 客户离线安装的原生 Ansible 实现

客户交付是主线：[Quick Start](customer-quickstart.md) 使用 `customer-prepare.yml`、`customer-install.yml` 和 `customer-reset.yml`。这三个入口直接执行 `ansible/roles/dify_offline/tasks/` 的 YAML 任务；不调用自定义 Python 模块、Python 全流程脚本或 Shell 安装器。

RHDP 的 `dify.yml` 只发现验证环境的参数、传输介质和配置 AWS 私网访问，然后调用同一个客户 Role。客户任务不读取 RHDP 身份文件、不要求 AWS CLI、不创建 OpenShift，也不修改客户防火墙。RHDP 集群创建仍由独立仓库维护。

## 从用户入口到具体操作

| 入口 / 任务 | 职责 |
|---|---|
| `customer-prepare.yml` → `prepare/` | 校验客户参数；下载并校验 oc/Helm；本地渲染；导出 OCI；打入 Ansible wheels；封包 |
| `render.yml` / `render-child.yml` | 根据同一份配置渲染本地依赖 Chart，生成厂商 Chart values，收集构建容器配置 |
| `seal.yml` / `verify.yml` | 枚举完整文件集合，拒绝符号链接及特殊文件，逐个做流式 SHA256 校验 |
| `customer-install.yml` → `install/` | 核对集群身份；管理 TLS、凭证和 rootful Podman；导入镜像；安装和部署验收 |
| `map-text.yml` / `transform-resource.yml` | 用完整引用边界替换镜像；处理 Secret 内编码的生成器配置；补充拉取凭证及已知 OTLP 修正 |
| `customer-reset.yml` → `reset/` | 默认预演；证明资源归属；确认计划 hash；带 UID 前提删除命名空间；清理专属数据并记录可续跑日志 |

这里只使用 `ansible.builtin` 模块，不需要在离线现场访问 Galaxy。文件、配置、检查和等待分别是有名称的任务；oc、Helm、Podman、OpenSSL、tar 等使用 `command` 的 `argv` 调用。S3 探针和数据库探针中的短 Shell 只在测试容器内执行，不承担安装流程编排。

Ansible 本身和内置模块仍使用 Python。`python -m venv`、`python -m pip` 用于建立 Ansible 控制端及收集其 wheel 依赖，不是另一个安装控制器。客户控制端约定 Python 3.12，远程目标可以使用系统 Python 3.9；具体主机前提见 [OS 准备](host-prerequisites.md)。

## 镜像与 Chart 怎样离线化

联网端用本地 Helm 渲染依赖层和厂商 Chart，收集 Pod 镜像以及插件连接器的运行时/构建基础镜像。每个镜像导出为单平台 OCI 归档，核对 index、manifest 和 config 的 SHA256，使用可被 Podman load 的 config digest 作为 image ID。

离线端从归档加载并通过校验 CA 的 TLS 连接推入客户 Registry，以目标仓库返回的 manifest digest 建立映射。Kaniko/Python 构建基础镜像会先在无网络、禁止拉取的本地构建中加入私有 CA，映射记录派生后的目标 digest。

安装时先解析稳定 Secret 引用并映射连接器运行时镜像，再渲染厂商 Chart，使 Pod 模板校验和反映真实配置；YAML 任务继续替换资源镜像和编码配置，生成临时派生 Chart。它保留原始 hook 注解，由 Helm 管理 hook 生命周期，不再依赖 post-renderer；Secret 和 Pod 模板中的凭证引用保留。原厂 Chart 与封包不修改。临时 Chart 和已解析 values 权限为 0600，安装结束或失败均移除。

高级外部 S3/ODF 和 arm64 使用同一客户 Role 的配置分支，见 [高级配置](disconnected-manual.md)。默认 Quick Start 和 RHDP 适配保持 amd64、本地 S3；扩展分支的本地渲染测试不代表对应架构或存储已现场验收。

## 状态与清理保护

介质保持不可变；状态、密钥、导入记录和诊断留在独立私有目录。日志和异步目录在首次提权异步任务前按状态目录所有者准备为 0700；兼容旧 root 所有目录的修正仅限这两个经过校验的目录，不递归接管文件。失败时先保留原始任务及结果，再尝试写私有诊断；诊断写入失败仍保留原始任务名并返回失败。目录拒绝 Git 祖先、符号链接和系统根目录；安装/清理共用原子目录租约，不能强行接管另一次运行。重跑核对集群 ID、context、配置和镜像锁；已有应用密钥、Registry/S3 凭证和数据复用，不隐式轮换。

清理从真实 Chart 和完整 API 资源清单证明归属：明确管理的资源、UID ownerReferences 链、Service selector/Pod UID/IP 对应的 Endpoints，以及 Dify CRD controller 持有的特定 Lease。遇到不明归属立即拒绝。执行需要预演得到的完整计划 hash，删除使用命名空间 UID 前提；PV 回收策略在预演中单列，不强制移除 finalizer。

清理宿主数据前重新核对容器 ID、挂载路径、其他容器是否共享数据、目录 inode/device 和嵌套挂载。保留介质、CA/凭证、共享集群资源和镜像缓存。支持同一次已确认清理的显式续跑；使用相同 run ID 和确认值，不自动生成新的删除范围。

## 验证边界

原生化需要分别验证 YAML 任务、真实 Chart 渲染、介质封包及故障恢复，再在 RHDP 运行客户安装链路。历史 Python 实现的部署记录不能直接作为这次重构已经现场通过的证据。License、插件依赖、模型调用和 RAG 仍需独立验收。

原 Python 逻辑仅保留在 `tests/reference/` 作为行为对照，既不是可用操作入口，也不会进入客户介质。当前本地安装测试执行客户入口和真实 Chart；清理测试执行完整删除/数据清理/续跑 YAML，并注入 UID 竞争和共享目录拒绝情形。系统、集群和云边界使用临时目录及命令模拟，不修改真实环境。
