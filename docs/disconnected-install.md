# 使用 Ansible 离线部署 Dify

**日常部署入口是 `ansible/dify.yml`，部署命令使用 `ansible-playbook`。** Ansible 负责主机连接、阶段顺序、制品传输、服务恢复和验收；`scripts/offline.py` 是内部调用的镜像/Helm 执行引擎，仍保留给底层维护与手动适配使用。迁移编排入口不意味着删除 Python 运行时。

原先本页的 Python、证书、导入和安装命令已完整迁移至 [底层工具手动操作参考](disconnected-manual.md)。本页作为 Ansible 主指南，不再要求操作者逐条执行这些 Python 部署命令。

## 1. 先确认适用范围

| 环境 | 当前入口 | 状态 |
|---|---|---|
| RHDP OpenShift Disconnected Workshop 的既有离线集群 | `ansible/dify.yml` | 已验证部署、重启恢复和验收 |
| 在该 Workshop 中创建离线 OpenShift | 私有仓库 [rhdp-demo-automation](https://github.com/SammZhu/rhdp-demo-automation) 的 `ansible/cluster.yml` | 独立维护，不在本仓库创建集群 |
| 任意客户离线 OpenShift、不同网络或对象存储拓扑 | [通用底层工具参考](disconnected-manual.md) | 尚未提供客户通用 Ansible Playbook；需要按现场适配与验证 |

**不能将目前的 RHDP Playbook 直接当作任意客户环境的通用 Ansible 安装器。** 它依赖 Workshop 的 Jump/Highside、集群就绪文件、AWS 身份及网络约定。通用交付引擎保留了客户侧能力，但这些客户拓扑没有随本次 RHDP 编排迁移一起实现通用 Ansible 自动化。

## 2. 控制端和工程准备

在联网的 Workshop Jump 主机执行，准备完整、干净的工程源码；允许通过源码压缩包传入，不要求现场安装 Git。

- Ansible 控制端：Python 3.12+，依赖见 `ansible/requirements.txt`；目标主机使用 RHEL Python 3.9+ 和已有 PyYAML。
- Jump/Highside 的 SSH、sudo、Podman、OpenSSL、rsync 和合法源镜像访问权限已就绪。
- OpenShift 集群创建流程已经验收，Highside `/mnt/high-side-data/rhdp-bootstrap` 下保留 `ready.json`、身份、工具和私有 kubeconfig。
- Portal 的有效期覆盖整个下载、部署和验收窗口；Playbook 不修改 Auto-Stop/Auto-Destroy。

首次安装 Ansible 控制端的依赖步骤见 [使用 Ansible](rhdp-dify-offline.md#使用-ansible)。其中 Python 的 venv/pip 命令只用于准备 Ansible 运行环境，不是部署流程入口。

下列命令均从完整工程的 `ansible/` 目录执行，不能在只有镜像交付包的目录内执行。控制端也不在 Highside 上运行。

## 3. 预演与完整执行

```bash
# 在 Jump 上进入工程，在 tmux 中执行，避免终端断开中止控制端
cd dify-enterprise-openshift/ansible
tmux new -s dify-ansible

# 以下命令在新 tmux 会话内执行；不要依赖旧 tmux 服务继承外层 PATH
. /mnt/low-side-data/ansible-venv/bin/activate

# 检查变量、阶段顺序并显示计划
ansible-playbook dify.yml --check

# 执行完整流程
ansible-playbook dify.yml
```

`--check` 不读取源凭证、不连接 AWS，也不执行现场容量预检、Helm 清单渲染或镜像导出。它不是部署成功或制品完整性的证明。

正常执行依次完成：

1. `preflight`：绑定当前集群和 Highside 身份、检查前提。
2. `tools`：准备校验后的 Helm、匹配版本 oc 和 Podman 包装器。
3. `prepare`：渲染 3.9.8 Chart、导出 OCI 镜像并生成封包校验清单。
4. `transfer`：搬运公开源码和交付包，在 Highside 完整复核。
5. `network`：仅允许当前集群节点安全组访问本地服务端口。
6. `services`：启动本地 TLS Registry、wheel 索引和 S3，安装校验过的 systemd 开机单元。
7. `import`：离线加载镜像、推入本地仓库并固定目标 digest。
8. `install`：通过本地 Chart 安装前置权限、数据层和 Dify。
9. `check`：等待运行 Pod 就绪，检查内部镜像、四个数据库、Redis、认证 TLS S3 和 HTTPS 入口。

这里的 Highside 不访问公共 Helm、镜像或 pip 仓库。Ansible 通过内部执行引擎完成操作，无需手工输入 `offline.py prepare/import/install/check`。

## 4. 状态、分阶段与重启恢复

```bash
# 读取两侧的历史完成标记
ansible-playbook status.yml

# 只完成联网侧制品准备
ansible-playbook dify.yml -e '{"rhdp_dify_stages":["preflight","tools","prepare"]}'

# 恢复旧安装的本地服务、补齐开机启动并验收
ansible-playbook dify.yml -e '{"rhdp_dify_stages":["preflight","services","check"]}'

# 已运行环境的现场验收，不手动启动服务
ansible-playbook dify.yml -e '{"rhdp_dify_stages":["preflight","check"]}'
```

阶段选择必须保持依赖顺序，后期阶段要求前置制品已经存在。`status.yml` 仅显示历史标记，`check` 才检查现场。`services` 复用归属匹配的容器和数据，不清空数据库、Registry 或 S3。

专属 `dify-offline-services.service` 等待网络和数据盘挂载后启动已有的三个容器。详见 [主机重启恢复](rhdp-dify-offline.md#主机重启恢复)；普通 `--restart` 标志本身不能代替开机恢复验收。

## 5. 配置和凭证

默认参数见 `ansible/group_vars/all.yml`，连接清单见 `ansible/inventory.example.yml`。本 RHDP 配置固定：

- Dify Enterprise 3.9.8、namespace/release `dify`、Linux amd64。
- 当前集群的真实 Ingress 域名，数据层使用既有 `gp3-csi`。
- Highside TLS Registry 5000、HTTPS wheels 9443、本地 TLS S3 9444。
- 原有 OpenShift mirror-registry 继续使用 8443。

覆盖文件放到工程之外并设为 0600，通过 `-e @/私有路径/profile.yml` 传入。不得改变上述固定拓扑；其他受支持布局参见 [手动适配参考](disconnected-manual.md)，不要在本 Playbook 中修改硬编码假设。

源镜像认证留在 Jump。Highside 的 Registry/S3 认证、私钥、日志及 kubeconfig 留在私有运行目录，不提交 Git。保持参数与集群身份不变才能复用状态；不要手工修改封包或强行抢占运行租约。

## 6. 验收范围与客户交付

当前实测记录见 [RHDP 部署与重启复验](rhdp-dify-offline.md#实测记录)，完整功能清单见 [离线安装验收记录](disconnected-validation.md)。已完成当前 Workshop 的部署、镜像内部化、数据层、TLS S3、HTTPS 和重启恢复验收。

Enterprise 离线 License、实际业务文件上传、插件构建、模型调用、RAG，以及第二套全新环境的完整 Ansible 链路仍未验收。Workshop 有 AWS/RHUI 等代理访问例外，并非物理隔离环境。

客户使用其他 StorageClass、外部 S3、不同架构、插件 wheels 或预置模型时，参考 [底层工具手动操作参考](disconnected-manual.md)。此参考文档保留已有能力，但不表示客户通用 Ansible 迁移已经完成。
