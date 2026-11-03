# 清理 Dify 并重新验证离线安装

客户入口是 **`ansible/customer-reset.yml`**，在离线 Podman 主机以 root 运行；RHDP 使用 **`ansible/rhdp-reset.yml`**，在已有 Jump 控制端通过 SSH 管理 Highside。默认只连接现场读取清理计划，**不会删除资源**；只有显式启用执行并提供该计划的确认 hash 才会清理。新制介质会携带此入口；获取新版入口时应使用新的 `customer_media_root` 制包，旧工作目录的完成标记不会自动升级原封包。

本入口适用于本工程已有的 `dify` / 本地 S3 专用安装，接受客户入口的 `customer-binding.json` 或 RHDP Highside 的既有 `binding.json`。它不创建集群，不清理共享/外部 S3，也不接管无安装绑定或无匹配安装标记的未知安装。**命名空间与三个本地服务必须专供本项目使用**；应用模式会清空该本地 S3 服务的整个数据目录，而不只是某个对象前缀。

## 1. 选择清理范围

| 模式 | 删除 / 清空 | 保留 |
|---|---|---|
| `application`（默认） | 整个 `dify` 命名空间及其中的工作负载、Helm release 记录、Secret、数据库/Redis/Qdrant PVC；本地 S3 服务的数据目录；过期的安装验收标记。S3 和 wheel 容器会短暂停止再启动。 | Registry 容器与仓库镜像、三个本地服务容器、专用 systemd 单元、证书和认证、离线介质、导入映射及 Podman 镜像缓存。 |
| `full` | 在应用重置基础上，删除本项目三个 Podman 容器、专用 systemd 单元，清空 Registry 数据目录并删除旧导入映射。 | 离线介质、CA/证书、Registry/S3 认证、集群绑定、清理审计记录及 Podman 镜像缓存。后续重新导入镜像、创建服务并安装应用。 |

两种模式都保留 OpenShift 集群、节点、StorageClass、共享 CRD/SCC/集群级 RBAC、镜像 CA 信任配置，以及 Workshop 原有 mirror-registry。`full` 是本项目的应用与服务数据重置，不是把整个 OpenShift 恢复到从未部署过 Dify 的状态；也不清理节点镜像缓存。

**业务数据会丢失。** 清理前完成需要的数据库、S3 和卷数据备份。计划会列出 PVC 对应的 PV 及回收策略：`Delete` 的存储回收由 CSI 异步处理；`Retain` 的 PV/底层存储仍保留，需要管理员另行处置。本入口不直接删除 PV，也不承诺物理介质安全擦除。

## 2. 准备目标配置

客户本地入口使用已安装 Ansible 的 Python 3.12 环境，前提同 [客户 Quick Start](customer-quickstart.md)；RHDP 使用下述 Jump 控制端。目标集群可达、具有 cluster-admin 权限，三个服务及其数据目录仍属于当前安装。

- 新介质：进入 `/opt/dify-media/bundle/ansible`。
- 旧介质：不要修改已封包的 `bundle`。将包含清理入口的新版公开源码放到 `/opt/dify-enterprise-openshift`，从其 `ansible/` 目录运行，配置仍指向原来的介质与私有状态。旧包不会自动获得新 Playbook。

使用原 `/root/dify-private/install.yml`，最少包含：

```yaml
customer_bundle_dir: /opt/dify-media/bundle
customer_state_root: /var/lib/dify-offline
customer_kubeconfig: /root/dify-private/kubeconfig
customer_context: customer-admin  # 客户确认过的实际 context
```

### RHDP：继续使用 Jump 控制端

在 **Jump 的新版完整源码 `ansible/` 目录**执行，不在 Highside 安装 Ansible。控制端使用既有 Python 3.12 venv；Highside 使用既有 `/usr/bin/python3`（3.9+）、PyYAML 和免密 sudo。默认 inventory 沿用 Workshop 的 `highside` SSH 别名；可通过 `-i /私有路径/inventory.yml` 覆盖，但应保持一个 Highside 目标。

```bash
install -d -m 0700 /mnt/low-side-data/dify-private
cp rhdp-reset.example.yml /mnt/low-side-data/dify-private/reset-rhdp.yml
chmod 0600 /mnt/low-side-data/dify-private/reset-rhdp.yml
```

编辑这份 Jump 本地配置，路径是 **Highside 上的目标路径**：

```yaml
rhdp_dify_remote_dir: /mnt/high-side-data/dify-bootstrap
rhdp_cluster_remote_dir: /mnt/high-side-data/rhdp-bootstrap
rhdp_reset_context: 实际context名称
customer_reset_mode: application
customer_reset_run_id: dify-reset-rhdp-001
```

先读取指定 kubeconfig 的 context，再确认并填写，不依赖终端默认 context：

```bash
ssh highside '/mnt/high-side-data/dify-bootstrap/bundle/bin/oc --kubeconfig /mnt/high-side-data/rhdp-bootstrap/cluster/auth/kubeconfig config get-contexts'
```

在 Jump 预览：

```bash
/mnt/low-side-data/ansible-venv/bin/ansible-playbook rhdp-reset.yml \
  -e @/mnt/low-side-data/dify-private/reset-rhdp.yml --check
```

按第 3 步的核对要求审阅输出，复制 `confirmation` 后执行：

```bash
/mnt/low-side-data/ansible-venv/bin/ansible-playbook rhdp-reset.yml \
  -e @/mnt/low-side-data/dify-private/reset-rhdp.yml \
  -e customer_reset_execute=true \
  -e customer_reset_confirm=替换为预览输出的64位confirmation值
```

两种入口调用同一组清理任务和归属检查。RHDP 接受原 Highside 绑定，不会清理 Jump 的联网制作目录。RHDP 清理/续跑/重装均在 Jump 控制，客户独立安装则在离线 Podman 主机本地执行。

## 3. 客户本地入口：预览并执行应用重置

在上述选定的 `ansible/` 目录执行。为本次操作选择唯一 run ID（12–80 个字母、数字或连字符），示例为 `dify-reset-lab-001`：

```bash
/opt/dify-ansible/bin/ansible-playbook -i localhost, customer-reset.yml \
  -e @/root/dify-private/install.yml \
  -e customer_reset_mode=application \
  -e customer_reset_run_id=dify-reset-lab-001 --check
```

**此处 `--check` 会读取真实集群和 Podman 状态**，与安装 Playbook 只显示计划的 `--check` 不同；不会创建清理状态或执行删除。也可以不加 `--check`，在默认 `customer_reset_execute=false` 下同样只预览。

核对输出中的 cluster ID、context、命名空间 UID、对象清单、PV 策略、本地目录和容器 ID。没有外来资源、目标确实是本次实验且备份已完成后，复制输出的 `confirmation` 值，执行：

```bash
/opt/dify-ansible/bin/ansible-playbook -i localhost, customer-reset.yml \
  -e @/root/dify-private/install.yml \
  -e customer_reset_mode=application \
  -e customer_reset_run_id=dify-reset-lab-001 \
  -e customer_reset_execute=true \
  -e customer_reset_confirm=替换为预览输出的64位confirmation值
```

执行前会重新获取计划并比较 hash；命名空间或容器被替换、配置变化、增加资源等情况会阻止旧计划执行。动态控制器可能导致对象清单变化，此时需要重新预览，不能复用旧确认值。

## 4. 完整重置

客户第 3 步和 RHDP 上述操作的**预览、执行两条命令**均改用：

```text
customer_reset_mode=full
customer_reset_run_id=dify-reset-full-001
```

完整模式的确认值不同，必须重新预览。它先等待命名空间删除完成，再停止并移除归属匹配的专用 systemd 单元和三个容器，清空专用 S3/Registry 数据目录。不执行全局 Podman prune、不删除其他容器或存储卷。

## 5. 重新安装和验收

成功后查看私有状态目录中的 `reset-report.json`（客户为 `customer_state_root`；RHDP 为 Highside 上的 `rhdp_dify_remote_dir`）。这是清理报告，不是安装成功报告；原 `deployment-ready.json` 会删除。

客户入口：

```bash
/opt/dify-ansible/bin/ansible-playbook -i localhost, customer-install.yml \
  -e @/root/dify-private/install.yml
```

RHDP 原入口仍在 **Jump 的完整源码 `ansible/` 目录**执行；Highside 上的清理不要求在 Jump 安装 customer Playbook：

```bash
# 在 Jump 的完整工程 ansible/ 目录，激活原控制端环境后执行
ansible-playbook dify.yml \
  -e '{"rhdp_dify_stages":["preflight","services","import","install","check"]}'
```

RHDP 应使用原私有配置（如有则继续加 `-e @/原私有路径/profile.yml`）并保留既有源码快照、两侧状态及原 bundle；以上阶段不重新联网下载、制作或搬运介质。完整重置后必须重新 `services → import → install → check`，不能只跑 `check`。客户完整安装入口会自动按这个顺序执行。

通过重新生成的 `deployment-ready.json` 验收结果确认部署。应用模式保留 Registry 缓存，因此不属于空仓库的镜像导入测试；完整模式清空 Registry，但仍保留 Podman/节点缓存。需要证明全新集群冷启动时，应在独立新环境验证。License、业务上传、插件、模型和 RAG 仍按 [验收清单](disconnected-validation.md) 单独验证。

## 6. 中断恢复与归属保护

清理记录在 `customer_state_root/reset-runs/<run-id>/`，与安装器使用相同的运行租约，防止同时安装/清理。每个阶段成功后写入标记。命名空间删除超时会停止，**不强制移除 finalizer**，也不会继续清空主机数据。

检查并处理故障后，使用原模式、run ID 和 confirmation，额外加：

```text
-e customer_reset_resume=true
```

建议先对该续跑命令加 `--check` 读取已确认计划，再去掉 `--check`、保留执行参数续跑。不得把原 run ID 用于另一轮清理。若命名空间已由重装生成新的 UID，旧清理不能继续。

并发或进程被强制终止时，可能保留 `.ansible-run` 租约；先确认旧控制端和异步任务均已结束，再由管理员处理残留租约，脚本不会自动抢占。命令错误保存在私有 `reset-runs/<run-id>/last-error.log`，归属/阶段错误见 `logs/customer-reset-error.json`。不要上传这些私有运行目录。

清理前会检查安装绑定、匹配的安装标记、命名空间内资源归属、容器标签/ID/挂载、专用单元内容和数据目录身份。无法识别的外来资源、其他容器共享数据目录、数据目录本身为符号链接或其下存在嵌套挂载等情况需要先由管理员核实，不会被当作“可忽略错误”跳过。命名空间删除带 UID 前置条件；实现依据见 [Kubernetes delete CLI](https://github.com/kubernetes/kubectl/blob/master/pkg/cmd/delete/delete.go)。

## 验证边界

清理逻辑通过临时目录与模拟集群/Podman 边界验证；本轮没有在真实客户或 Demo 集群执行破坏性清理与重装。它也不能自动证明独立于缓存的冷启动、存储物理擦除或完整业务验收。
