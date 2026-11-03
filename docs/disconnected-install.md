# 使用原生 Ansible 离线部署 Dify

**客户安装是主线，从 [客户 Quick Start](customer-quickstart.md) 开始。** 联网端执行 `customer-prepare.yml`，离线 Podman 主机执行 `customer-install.yml`，清理与重装执行 `customer-reset.yml`。具体机制见 [原生任务与保护逻辑](native-ansible.md)。

三个入口直接执行同一个原生 YAML Role，管理介质、镜像、TLS、Podman、Helm、清理和验收。没有另一个 Python/Shell 安装控制器。Python venv/pip 命令仅用于准备 Ansible 运行时。

## 客户准备与安装

Quick Start 默认需要已有且健康的 amd64 OpenShift、RHEL 9 兼容 Podman 主机、稳定 DNS、可用 StorageClass、明确的 kubeconfig/context，以及节点和 Pod 到该主机的内网访问。单机 Registry、wheel 索引和本地 S3 使用 TLS，不要求客户 Git 服务或已有镜像仓库。

完整可复制步骤、参数填写位置、介质搬运和离线启动见 [Quick Start](customer-quickstart.md)。缺少 OS 软件时先执行 [主机准备](host-prerequisites.md)；该步骤可从已有软件源安装，也可在禁用仓库的条件下使用本地完整 RPM 集合。

| 操作 | 入口 | 阶段 |
|---|---|---|
| 联网制作介质 | `ansible/customer-prepare.yml` | preflight → tools → export → controller → package |
| 客户离线部署 | `ansible/customer-install.yml` | preflight → verify → services → import → install → check |
| 历史状态 | `ansible/customer-status.yml` | 只读阶段标记，不证明现场健康 |
| 清理重装 | `ansible/customer-reset.yml` | 默认预演；确认完整计划 hash 后执行 |

Quick Start 默认配置为 Dify Enterprise 3.9.8、namespace/release `dify`、Linux amd64、单机 Podman/本地 S3，版本适配及现场验证边界见 Quick Start。同一 Role 的外部 S3/ODF、arm64 参数分支见 [高级配置及验证范围](disconnected-manual.md)；这些分支尚未现场验收，高可用仍需独立适配。

## RHDP 仅用于验证客户代码

RHDP 离线集群创建独立放在私有 [rhdp-demo-automation](https://github.com/SammZhu/rhdp-demo-automation)；本仓库不创建集群。`dify.yml` 在该验证环境中探测客户参数、传输同一份介质、配置 AWS 私网访问，然后调用客户 Role，不维护第二份安装实现。

在 Workshop Jump 的完整源码 `ansible/` 目录执行：

```bash
cd dify-enterprise-openshift/ansible
/mnt/low-side-data/ansible-venv/bin/ansible-playbook dify.yml --check
/mnt/low-side-data/ansible-venv/bin/ansible-playbook dify.yml
```

控制端、连接清单和阶段选择见 [RHDP 验证指南](rhdp-dify-offline.md)。`--check` 只检查编排选择，不下载介质、不修改 AWS 或集群，不能代替制品与现场验收。源凭证只留在联网端，私钥、目标认证、kubeconfig 和日志只留在私有目录。

## 验收与重跑

同一介质、状态目录及集群可以重跑；服务复用已有归属匹配的数据和凭证，不自动清空 PVC。只验收时选择 `[preflight, verify, check]`；重启后恢复服务并验收选择 `[preflight, verify, services, check]`。

安装验证 Pod Ready、内部 digest 镜像、四个数据库、Redis、认证 TLS S3 和 HTTPS 入口。License、业务上传、插件、模型和 RAG 必须另行验收，见 [验收清单](disconnected-validation.md)。历史实现的 RHDP 部署/重启记录不等于本次原生化已通过新一轮完整现场验证。
