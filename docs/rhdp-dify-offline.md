# RHDP 离线集群上的 Dify 部署自动化

入口是 `ansible/dify.yml`，在 **OpenShift Disconnected Workshop 的 Jump** 上执行。集群创建独立维护于私有仓库 [rhdp-demo-automation](https://github.com/SammZhu/rhdp-demo-automation)，本工程不再包含集群创建入口。客户侧的通用交付流程仍使用 [offline.py](disconnected-install.md)。

**部署验证状态：2026-11-02 已在当前真实 RHDP 环境完成部署验收。** 本轮没有 Enterprise 离线 License；覆盖工作负载、数据层、TLS S3 和 HTTPS 入口，模型、插件构建和 RAG 未验证。原部署为同一环境修复后续跑完成；没有在第二套全新环境重复验证。Ansible 迁移复验范围单独记录在下文。

## 使用 Ansible

Ansible 控制端在联网 Jump 上运行，要求 Python 3.12+；Highside 和本机任务仍使用 RHEL `/usr/bin/python3` 3.9+ 及已有 PyYAML。依赖固定在 `ansible/requirements.txt`，无需 Galaxy Collections。依据 [官方 Python 支持矩阵](https://docs.ansible.com/projects/ansible/latest/reference_appendices/release_and_maintenance.html) 选择版本。

```bash
# 在 Jump 的完整工程目录；允许从源码包展开，不要求 Git。
sudo dnf install -y python3.12 python3.12-pip
python3.12 -m venv /mnt/low-side-data/ansible-venv
. /mnt/low-side-data/ansible-venv/bin/activate
pip install -r ansible/requirements.txt
cd ansible
ansible-playbook dify.yml --check
# tmux 中完整执行
tmux new -s dify-ansible
ansible-playbook dify.yml
ansible-playbook status.yml
```

`--check` 显示计划、校验阶段顺序，不读取源认证或连接 AWS，也不代表现场预检通过。正常执行由 inventory 管理 Jump/Highside 连接。`inventory.example.yml` 默认使用 RHDP 的 `highside` SSH 别名和既有认证；不关闭 host-key 校验、不采集 Portal 密码。不要把真实 SSH Key、registry auth 或 License 放进 inventory/vars。

需要先创建集群时，先在 **rhdp-demo-automation** 的 `ansible/` 目录执行 `ansible-playbook cluster.yml`，成功后回到本工程的 `ansible/` 目录执行 `ansible-playbook dify.yml`。两个工程通过 Highside `/mnt/high-side-data/rhdp-bootstrap` 下的 `ready.json`、`environment.json`、`cluster/metadata.json`、`payload/oc` 和 `cluster/auth/kubeconfig` 衔接；私有文件不通过 Git 传递。脚本不自动延长 Portal 生命周期。

部署入口依赖完整工程中的 `ansible/`、`examples/`、`offline/`、`docs/`、`README.md` 以及三个通用交付助手。Ansible 仅快照公开依赖目录和 `offline.py` / `offline-post-render.sh` / `start-offline-services.sh`；不搬运 `.git`、本地私有 values、凭证或运行状态。应提供干净的公开源码包，不在依赖目录混入客户文件。

前提：Linux amd64 Jump、RHEL Highside、既有 SSH/sudo、Podman、OpenSSL、rsync；Jump 私有的 `~/pull-secret-example.json` 有合法镜像访问权限。流程校验 Linux Helm 3.16.4，使用集群创建阶段的匹配版本 oc 和固定 3.9.8 Chart。

Sandbox 镜像的 UID/GID 超出默认 rootless 映射范围，包内 `bin/podman` 使用 rootful Podman 保留元数据；针对本次 RHEL 9.6/systemd/crun 的 eBPF device-filter 错误，限定使用 `--cgroup-manager=cgroupfs`。SELinux/seccomp 保持启用，OpenShift 节点运行时配置不变。见 [Podman 全局选项](https://docs.podman.io/en/stable/markdown/podman.1.html)。其他客户若使用 rootless，需另验其用户命名空间映射。

## 自动阶段

| 阶段 | 动作 |
|---|---|
| preflight | 绑定当前集群 UUID、Highside 身份及参数，要求已有集群 ready.json，检查 Jump 容量 |
| tools | 下载校验 Linux Helm，准备匹配的 oc 和 rootful Podman 包装器 |
| prepare | 渲染真实 Chart、解析运行时镜像、导出 OCI 包并生成校验清单 |
| transfer | rsync 源码、交付包和非秘密参数，离线侧完整复核 |
| network | 仅允许当前集群节点安全组访问 Highside TCP 5000/9443/9444，不使用公网 CIDR，不更改出口策略 |
| services | 在 Highside 生成私有 TLS/认证材料，启动本地 Registry、wheel 索引和 S3 |
| import | 只从 OCI 归档加载镜像，推到本地 Registry，以目标 digest 固定引用 |
| install | 创建命名空间、SCC/RBAC、CRD、数据层和本地 Helm release，等待就绪 |
| check | 检查 Pod/镜像、四个数据库、Redis、认证 TLS S3 读写删除和 HTTPS 入口 |

联网侧只在 Jump 取材。Highside 不访问公共 Helm 仓库、容器仓库或 pip；CA 派生构建使用 `--network=none --pull=never`。本轮无插件 wheels，索引为空；插件依赖闭包及插件构建不属于已验收项。

Ansible 负责编排、rsync 白名单传输和阶段等待；领域操作由自定义模块完成，通用镜像/Helm 引擎仍为 offline.py，不再调用旧 Python 控制器。长任务以 async 和正数 poll 串行等待，参见 [官方说明](https://docs.ansible.com/projects/ansible/latest/playbooks_async.html)。

本入口使用：

- Dify Enterprise 3.9.8，namespace/release `dify`。
- 既有默认 `gp3-csi`，单实例 PostgreSQL、Redis、Qdrant。
- Highside `:5000` TLS Registry、`:9443` HTTPS wheels，避免与 OpenShift mirror-registry 的 `:8443` 冲突。
- Highside `:9444` TLS S3，nginx 反向代理到仅在 Podman 私有网络可达的 SeaweedFS 4.51 `weed mini`；原始 S3/Admin 端口不发布。
- 随机 S3 认证和持久化 `s3-data`，不依赖 ODF、客户已有 S3 或额外 AWS S3 bucket。

该 S3 是本工程的单机演示选择；CRUD 验证不等于完整 S3 API、HA、恢复和生产支持认证。机制参考 [SeaweedFS 官方 mini 文档](https://github.com/seaweedfs/seaweedfs/wiki/Quick-Start-with-weed-mini)。已有对象存储的客户继续使用通用手册的外部 S3 配置。

## 状态和恢复

Jump 默认状态 `/mnt/low-side-data/dify-bootstrap`；Highside 默认 `/mnt/high-side-data/dify-bootstrap`。目录 0700，凭证和原始日志 0600，拒绝把运行目录放进 Git。源仓库认证保留在 Jump，不传入包。Highside 私有材料包括 `state/auth.json`、TLS 私钥、`local-s3.json`、`local-s3.env` 和 Helm/命令错误日志，不能提交或共享。

同一参数和集群可重新运行。已有完整包会重新校验；Registry/数据目录保留，服务只有通过包身份标签检查才重用。仅同一包创建、处于从未启动的 `Created` 状态的部分容器可重新创建，全部绑定数据和凭证保留；运行过或归属不同的部分服务会停止并要求检查。不接管已有在线 Dify release，不删除 PVC、数据库或对象存储。`status.yml` 仅显示历史完成标记，现场健康以重新执行 `check` 阶段结果为准。

首次导出中断后，当前版本会保留部分包并停止，要求查看 `logs/prepare.log` 后选择新的 `rhdp_dify_work_dir`；不要把部分包当完整交付物。成功封包后的 rsync 可续传，导入可以重复。参数或集群变化会拒绝复用原状态。

参数见 `ansible/group_vars/all.yml`，客户覆盖文件放到仓库外并设置 0600，通过 `-e @/私有路径/profile.yml` 传入。保持参数不变才能复用原状态。可选择已具备前提的阶段，列表必须按原顺序：

```bash
ansible-playbook dify.yml -e '{"rhdp_dify_stages":["preflight","tools","prepare"]}'
# 现有部署的现场复验；仍会执行身份检查和公开源码同步
ansible-playbook dify.yml -e '{"rhdp_dify_stages":["preflight","check"]}'
```

每次执行在两侧创建原子的 `.ansible-run/owner.json`，阻止另一 Playbook 接管，具体阶段仍有文件锁。正常完成/失败时释放本次租约；强制杀掉控制端可能残留租约，必须先检查异步任务、owner.json 和私有日志，确认没有仍在运行的任务后再人工处理，禁止自动抢锁。不要混用旧 Python 控制器。

含敏感中间结果的 Ansible 任务使用 no_log，不 fetch 私有文件；原始异常保留为目标主机 `logs/ansible-<phase>.log`，模块只返回阶段状态与必要的非秘密身份。

真实 Podman 可能返回不带前缀的源 ID；Docker 转 OCI 时还可能改变 config 摘要。导出器将原始 ID 规范化存入 `sourceImageId`，从校验过的 OCI manifest/config 读取实际加载用的 `imageId`，两者不要求相同。本轮归档实测有 16/29 个转换后的 config ID 与源不同，已通过离线侧实际加载验证。

成功后 Highside 生成私有 `deployment-ready.json`。报告区分部署验收和未提供 License、插件/模型/RAG 未测试，不能据此宣称完整业务验收通过。应用对外可见的地址、状态和日志见这个报告及状态目录，不把客户凭证写进文档。

## 实测记录

本次使用 Guide 默认 `disco.lab` 单节点集群，实例为 `m6i.2xlarge`，OpenShift 4.20.8；没有为 Dify 扩容、安装 GitOps/ODF 或增加新的 AWS 实例。

| 检查 | 实测结果 |
|---|---|
| 镜像 | 29 个源引用从 OCI 包导入本地 TLS Registry；现场全部 Pod（含成功 Job）的 33 处主/初始化容器镜像引用均为私有仓库 digest |
| Helm / Pod | release `dify` 为 `deployed`；16 个应用 Pod 加 3 个数据层 Pod 全部 Ready，凭证生成 Job 成功 |
| 存储 | PostgreSQL、Redis、Qdrant 的 3 个 PVC 均为 gp3-csi / Bound |
| 数据层 | `dify`、`enterprise`、`audit`、`dify_plugin_daemon` 数据库存在，Redis 返回 PONG |
| S3 | 主机及集群内均完成认证 TLS 写入、读取比对、删除；匿名列桶被拒绝 |
| HTTPS | App、Console、Enterprise、Upload、API 入口 GET 最终返回 200；Trigger 根路径返回 404，无 5xx，触发业务未验证 |
| 平台健康 | Node Ready，无内存/磁盘/PID Pressure；34/34 个 ClusterOperator 稳定 |
| 私有材料 | `deployment-ready.json` 为 0600；认证、私钥、运行状态及原始日志保留在实验主机，不进入 Git |

本次修复已写入脚本：从实际 OCI config 读取加载 ID、rootful Podman 与 cgroupfs、公共 wheel 内容的读取权限、同一包从未启动容器的安全恢复，以及 AWS CLI 镜像缺少 `cmp` 时的 S3 内容/长度校验。S3 探针使用 Pod UID 作为唯一对象名，并通过退出 trap 清理自己的对象；回归测试覆盖内容篡改、额外换行和失败清理。迁移后的 Dify 工程本地 30 项测试通过，包含真实 3.9.8 Chart 渲染及 Ansible 状态保护；原集群测试已移至独立仓库，测试均不创建 RHDP 资源。

实测期间先修复原包中的 OCI ID 元数据并重校验归档，再继续导入；最终导出器会直接生成该元数据，但未另外重新导出全部镜像并新建第二套集群。首次 CA 分发曾造成短暂 `ErrImagePull`，在等待窗口内自动恢复，没有关闭 TLS 校验或重建集群。

该 Workshop 的离线性由代理和镜像策略限制实现，仍允许 AWS/RHUI 等特定访问；不是物理隔离环境。以上通过项不包括 License 激活、实际文件业务上传、插件安装/构建、模型调用、RAG、HA、性能或恢复验证。实验域名只能在具备相应 DNS 和网络连通性的实验网络内访问。

## Ansible 迁移复验

语法检查、无变更计划和 30 项测试已通过。当前集群的 Ansible `preflight` + `check` 已成功，recap 为 failed=0 / unreachable=0，包含集群内认证 TLS S3 写读删除和 HTTPS 入口检查；状态 Playbook 的 check-mode 在空状态目录验证为只读。未把现有集群复验等同于完整的全新 Ansible 导出/导入/安装验证。

Ansible 原生 transfer 已在当前 Workshop 成功复验：封包 rsync、Highside 完整校验和校验后标记均通过，复用既有目标制品；没有从空目标重新传输全部字节或创建第二套全新环境。
