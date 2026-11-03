# 客户 Quick Start：单机 Podman + 离线 OpenShift

**准备介质用 `customer-prepare.yml`，离线安装用 `customer-install.yml`。** 不需要客户 Git 服务、现成镜像仓库、RHDP 账号、AWS CLI 或 Workshop 的 `ready.json`。这两个 Playbook 在各自主机本地运行；源码可以通过压缩包传入。

本 Quick Start 安装 Dify Enterprise **3.9.8**，namespace/release 固定为 `dify`。单台 Podman 主机同时提供 TLS Registry、HTTPS wheel 索引和持久化本地 S3；适合功能验证与单机交付演练。外部 S3、其他 CPU 架构和高可用拓扑参见[手动适配参考](disconnected-manual.md)，不在本 Quick Start 自动化范围内。Enterprise License、插件依赖和模型介质需要另行准备。

## 0. 准备两侧主机与现场参数

| 项目 | 要求 |
|---|---|
| 联网制作机 | RHEL 9 兼容系统、Linux amd64、Python 3.12、至少 30 GiB 可用磁盘；建议预留 60 GiB，镜像导出和压缩包会同时占用空间 |
| 离线 Podman 主机 | 同样的 OS/架构/Python；数据盘建议至少 60 GiB，后续镜像、S3 和运行数据按需扩容；本 Quick Start 以 root 执行，有 sudo 权限的管理员先 `sudo -i` |
| 两侧 OS 软件 | 预先安装 `podman openssl httpd-tools tar python3.12 python3.12-pip`，使用 systemd；`httpd-tools` 提供 `htpasswd`。离线机的软件包须由客户 OS 安装介质或内网软件源提前提供，本交付包不含 RPM |
| OpenShift | **已有并健康**的 amd64 集群、4.20.x 或 4.21.x；提供匹配补丁版本、可用 StorageClass 和 cluster-admin kubeconfig；默认数据层申请 30 GiB PostgreSQL、5 GiB Redis、30 GiB Qdrant 三个 PVC。演练参考节点为 8 vCPU / 32 GiB，需预留集群运行开销；此流程不创建集群 |
| 域名 | OpenShift 实际 `apps` 域；Podman 主机有稳定 DNS 名称，必须从该主机、所有集群节点和 Pod 解析到可达的内网地址 |
| 网络 | 离线主机可访问集群 API 和 Route；节点与 Pod 可访问该主机 TCP **5000、9443、9444**。客户预先配置防火墙/NetworkPolicy，本流程不自动修改客户网络 |
| 源凭证 | 联网制作机上有效的容器认证 JSON，覆盖所需源仓库；Dify Enterprise 私有镜像须有合法拉取权限 |

可在**已有权限的客户集群终端**取得以下参数，再带到联网制作机；制作机本身无需连接客户集群：

```bash
oc get clusterversion version -o jsonpath='{.status.desired.version}{"\n"}'
oc get ingress.config.openshift.io cluster -o jsonpath='{.spec.domain}{"\n"}'
oc get storageclass
oc config current-context
```

不要直接使用本文的 `example.com` 域名或假定客户也有 `gp3-csi`。安装会核对真实集群域名、架构、StorageClass 和集群身份，错误参数将停止执行。

## 1. 联网制作机：安装 Ansible 并填写参数

以下示例使用 root，在持久化终端会话（例如 tmux）执行。源码通过[GitHub 源码包](https://github.com/SammZhu/dify-enterprise-openshift/archive/refs/heads/main.tar.gz)下载解压后放到 `/opt/dify-enterprise-openshift`；不要求安装 Git。应记录实际使用的源码版本。

```bash
cd /opt/dify-enterprise-openshift/ansible
python3.12 -m venv /opt/dify-ansible
/opt/dify-ansible/bin/python -m pip install -r requirements.txt
install -d -m 0700 /root/dify-private
cp customer-prepare.example.yml /root/dify-private/prepare.yml
chmod 0600 /root/dify-private/prepare.yml
```

将源镜像认证文件放到 `/root/dify-private/source-auth.json`，权限设为 0600。可以使用厂商提供的 pull-secret，或通过 `podman login --authfile /root/dify-private/source-auth.json 源仓库` 交互登录；不要把密码写进命令行或 Git。

编辑 `/root/dify-private/prepare.yml`，最少填写以下内容：

```yaml
customer_media_root: /var/tmp/dify-customer-media
customer_source_authfile: /root/dify-private/source-auth.json
customer_ocp_version: '4.20.8'  # 换成客户真实补丁版本
customer_apps_domain: apps.cluster.customer.example.com  # 客户实际 apps 域
customer_registry_host: registry.customer.example.com    # 离线 Podman 主机 DNS
customer_storage_class: gp3-csi                         # 客户已有的 StorageClass
```

源认证文件、参数文件、工作目录都放在源码目录之外。可选参数见 `customer-prepare.example.yml`；`customer_plugins_dir` 接收审核后的 `.difypkg`，`customer_wheels_dir` 接收这些插件所需的完整 wheel 集合。**控制端 wheelhouse 不等于插件依赖闭包**；未提供插件和模型时只验收平台部署。

## 2. 联网制作机：一条 Playbook 制作介质

```bash
cd /opt/dify-enterprise-openshift/ansible
/opt/dify-ansible/bin/ansible-playbook -i localhost, customer-prepare.yml \
  -e @/root/dify-private/prepare.yml
```

依次执行前置检查、校验并下载 oc/Helm、渲染 Chart、导出 OCI 镜像、下载 Python 3.12 的 Ansible 运行依赖、封包。下载失败应先查工作目录的私有日志；不要把失败的半成品当作就绪介质。镜像导出中断且留下半成品时，保留旧目录排查，换一个 `customer_media_root` 重做，避免混合两次导出。

完成后，只需搬运这两个文件到客户离线机的 `/opt/dify-media/`：

```text
/var/tmp/dify-customer-media/dify-offline-3.9.8-linux-amd64.tar.gz
/var/tmp/dify-customer-media/dify-offline-3.9.8-linux-amd64.tar.gz.sha256
```

包内包含 Chart、OCI 镜像与锁文件、oc/Helm、客户 Ansible 入口、控制端 wheels、wheel 索引及文档。源 registry 认证、客户 kubeconfig、目标服务私钥和 Enterprise License 不会自动加入介质。校验文件须通过可信渠道一并交付。

## 3. 离线 Podman 主机：校验并安装 Ansible 运行环境

**从这里开始不访问公网，同样在 root shell 中执行。** 确认第 0 步的 OS 软件已经装好，先验 checksum，再解包：

```bash
cd /opt/dify-media
sha256sum -c dify-offline-3.9.8-linux-amd64.tar.gz.sha256
tar -xzf dify-offline-3.9.8-linux-amd64.tar.gz
python3.12 -m venv /opt/dify-ansible
/opt/dify-ansible/bin/python -m pip install --no-index \
  --find-links /opt/dify-media/bundle/controller-wheels \
  -r /opt/dify-media/bundle/ansible/requirements.txt
```

这些 Python 命令仅建立 Ansible 运行环境。不要修改解压后的 `bundle`：安装会再次校验完整文件清单及 SHA256。

## 4. 离线 Podman 主机：填写安装参数并运行

```bash
install -d -m 0700 /root/dify-private
cp /opt/dify-media/bundle/ansible/customer-install.example.yml /root/dify-private/install.yml
chmod 0600 /root/dify-private/install.yml
```

由客户管理员把目标集群 kubeconfig 保存到 `/root/dify-private/kubeconfig`（0600）。编辑 `/root/dify-private/install.yml`：

```yaml
customer_bundle_dir: /opt/dify-media/bundle
customer_state_root: /var/lib/dify-offline
customer_kubeconfig: /root/dify-private/kubeconfig
customer_context: customer-admin  # 换成 kubeconfig 中客户确认的实际 context
```

如使用客户签发的 TLS 证书，同时提供 `customer_tls_cert`、`customer_tls_key` 和 `customer_tls_ca`，证书 SAN 必须覆盖 Podman 主机 DNS。未指定时生成并持久保存私有 CA；安装把 CA 合并到 OpenShift 镜像信任配置，不覆盖已有 CA。证书轮换需单独安排，重跑不会自动更换旧密钥。

```bash
cd /opt/dify-media/bundle/ansible
/opt/dify-ansible/bin/ansible-playbook -i localhost, customer-install.yml \
  -e @/root/dify-private/install.yml
```

流程为 `preflight → verify → services → import → install → check`：绑定明确的集群身份，启动三个 TLS 服务，安装专用 systemd 开机单元，离线导入并固定镜像 digest，部署权限/数据层/Dify，最后进行在线状态验收。首次安装可能等待节点 CA 信任配置生效和 Pod 启动。主机上的服务使用 **rootful Podman**，查看时使用 `sudo podman ps`。

## 5. 查看结果与重跑验收

成功结果：`/var/lib/dify-offline/deployment-ready.json`。其中 `routes` 列出实际 HTTPS 地址和状态，`localS3Roundtrip` 表示认证 TLS S3 读写删除测试通过。浏览器使用实际 Dify Route 访问；私有签发的 Route 证书须按客户规范信任。

只重新验收：

```bash
cd /opt/dify-media/bundle/ansible
/opt/dify-ansible/bin/ansible-playbook -i localhost, customer-install.yml \
  -e @/root/dify-private/install.yml \
  -e '{"customer_install_stages":["preflight","verify","check"]}'
```

主机重启后的恢复与复验：将上述阶段列表改为 `["preflight","verify","services","check"]`。完整 Playbook 也可在相同介质、状态目录和集群上重跑；复用已有归属匹配的服务、凭证和数据，不自动清空 PVC。阶段必须按原顺序选择并以 `preflight` 开始；缺少前置制品的后期阶段会失败。

`--check` 只检查编排计划，不连接目标集群，也不能代替制品校验与现场验收。命令失败时，管理员在状态目录 `logs/` 查看私有日志；不要直接上传日志、`state/`、kubeconfig 或认证文件到 GitHub。并行运行会被租约阻止，异常中断后应先确认旧进程已经结束再处理残留租约。

验收包括 Pod Ready、内部镜像引用、四个 PostgreSQL 数据库、Redis、TLS S3 和 HTTPS 入口；**未提供离线 License 时不代表 Enterprise 激活成功，也不代表文件上传、插件、模型调用或 RAG 已验收**。完整业务检查项见[验收清单](disconnected-validation.md)。本单机拓扑没有高可用能力。

## 当前验证边界

共享镜像/部署引擎与三服务方案已有 RHDP OpenShift 4.20.8 的部署和重启复验记录，见 [RHDP 实测记录](rhdp-dify-offline.md)。客户入口增加了独立参数和本地编排，不依赖该平台的集群身份文件或云 API；客户全新环境和 4.21 的现场验证情况，尚未完成，不能沿用 RHDP 的实测结论。已通过 36 项本地回归（包含真实 3.9.8 Chart 渲染、封包/校验、集群绑定与状态保护；容器和集群边界采用模拟），以及两个客户 Playbook 的语法和无变更计划检查。这些检查不等于客户现场完整安装成功。
