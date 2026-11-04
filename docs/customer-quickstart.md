# 客户 Quick Start：单机 Podman + 离线 OpenShift

**准备介质用 `customer-prepare.yml`，离线安装用 `customer-install.yml`。** 不需要客户 Git 服务、现成镜像仓库、RHDP 账号、AWS CLI 或 Workshop 的 `ready.json`。这两个 Playbook 在各自主机本地运行；源码可以通过压缩包传入。

本 Quick Start 安装 Dify Enterprise **3.9.8**，namespace/release 固定为 `dify`。单台 Podman 主机同时提供 TLS Registry、HTTPS wheel 索引和持久化本地 S3；适合功能验证与单机交付演练。外部 S3、其他 CPU 架构和高可用拓扑参见[高级配置参考](disconnected-manual.md)，不在本 Quick Start 自动化范围内。Enterprise License、插件依赖和模型介质需要另行准备。

## 先理解自动化如何工作

整体思路是：**联网端把安装所需的公开制品收集并封包，离线端用单台 Podman 主机提供本地服务，再将已有 OpenShift 的 Dify 配置指向这些内网服务。** Ansible 在两端分别本地执行，不要求联网制作机跨越隔离网络连接客户集群。

```mermaid
flowchart LR
    A[联网制作机：Ansible 收集 Chart、镜像、工具及 wheels] --> B[离线介质：tar.gz + SHA256]
    B -->|人工或客户批准的方式搬运| C[离线 Podman 主机：校验介质并运行 Ansible]
    C --> D[本机 TLS Registry / wheel 索引 / S3]
    C -->|本地 Chart + oc/Helm 部署| E[客户已有 OpenShift]
    E -->|节点拉取镜像；Pod 访问 wheels 和 S3| D
```

### 联网端做什么

`customer-prepare.yml` 按顺序执行 `preflight → tools → export → controller → package`：

1. 根据客户提供的域名、StorageClass 和版本生成配置，校验源镜像认证及宿主条件。
2. 下载并校验 oc/Helm，取得 Dify 3.9.8 Chart，渲染安装清单，收集工作负载、本地服务及插件构建基础镜像的引用。
3. 用 Podman 拉取并导出 OCI 镜像归档，记录镜像身份及文件校验信息；同时准备 Chart、工具、客户安装 Playbook、Ansible 控制端 wheels，以及用户提供的插件/wheels。
4. 输出可搬运的压缩包及 SHA256 文件。源镜像认证文件留在联网制作机，不交给离线集群。

### 离线端如何完成安装

`customer-install.yml` 按顺序执行 `preflight → verify → services → import → install → check`：

1. 校验封包文件清单和 SHA256，并用明确的 kubeconfig/context 查询已有集群，核对实际 apps 域、架构、StorageClass 和集群身份。
2. 从包内镜像启动三个 rootful Podman 容器：Registry 保存镜像，wheel 索引提供 Python 依赖文件，本地 S3 保存 Dify 对象数据。它们是离线主机上的常驻服务，运行 Dify 时仍需保持可用。证书、认证及数据持久化到私有状态目录，systemd 单元负责主机开机时启动已有容器。
3. 加载包内 OCI 镜像，推入这台主机的 Registry，生成“源镜像 → 内网仓库 digest”映射。需要信任私有 CA 的 Kaniko/Python 构建基础镜像，在本机用 `--pull=never --network=none` 派生；不去公网重拉基础镜像。
4. 将 Registry CA 合并到 OpenShift 镜像信任配置，创建所需凭证引用，并使用包内 Chart 通过 oc/Helm 部署权限、数据层和 Dify。安装时把工作负载及构建所用镜像引用改为内网 digest，将 wheel/S3 配置指向这台离线主机；不依赖 ArgoCD、Git 服务或在线 Chart 下载。
5. 验收运行 Pod、内网镜像引用、数据库、Redis、认证 TLS S3 读写删除和 HTTPS 入口，将结果写入 `deployment-ready.json`。

节点从内网 Registry 拉取镜像；需要安装 Python 依赖的流程从本地 wheel 索引取文件；Dify 的对象数据写入本地 S3；PostgreSQL/Redis/Qdrant 使用 OpenShift 的 PVC。**wheel 索引只能提供介质中已带入的文件，不能自动补齐缺失的插件依赖；模型服务及 License 也不会因平台部署成功而自动具备。**

### Ansible 和底层工具如何分工

三个客户 Playbook 直接执行 `ansible/roles/dify_offline/tasks/` 的原生 YAML 任务，使用 Ansible 内置模块及 oc、Helm、Podman 等标准工具；不再调用自定义 Python 模块、Python 安装引擎或 Shell 安装器。RHDP 也调用同一个客户 Role，只负责验证环境的参数发现、传输和私网配置。具体任务分工、镜像与 Chart 处理、清理保护见 [原生 Ansible 实现说明](native-ansible.md)。Ansible 本身仍需要 Python 运行时。

封包 `bundle/` 保持不可变；凭证、证书、导入映射、日志和完成标记写入独立状态目录。重跑时会校验集群绑定与容器归属，复用相同配置的密钥和数据；不会自动接管不属于本安装器的服务或 Helm release。这是安全重跑保护，不代表任意失败都能无条件自动恢复，故障排查方法见第 2、5 步。

## 0. 准备两侧主机与现场参数

| 项目 | 要求 |
|---|---|
| 联网制作机 | RHEL 9 兼容系统、Linux amd64、Python 3.12；可使用普通用户，但须能通过 sudo 执行 rootful Podman。源码、venv、参数及介质放在该用户有写权限的目录；介质盘至少 30 GiB 可用，建议 60 GiB |
| 离线 Podman 主机 | 同样的 OS/架构/Python；数据盘建议至少 60 GiB，后续镜像、S3 和运行数据按需扩容；本 Quick Start 以 root 执行，有 sudo 权限的管理员先 `sudo -i` |
| 两侧 OS 软件 | 预先安装 `podman openssl httpd-tools tar python3.12 python3.12-pip`，使用 systemd；`httpd-tools` 提供 `htpasswd`。离线机的软件包须由客户 OS 安装介质或内网软件源提前提供，本交付包不含 RPM |
| OpenShift | **已有并健康**的 amd64 集群、4.20.x 或 4.21.x；提供匹配补丁版本、可用 StorageClass 和 cluster-admin kubeconfig；默认数据层申请 30 GiB PostgreSQL、5 GiB Redis、30 GiB Qdrant 三个 PVC。演练参考节点为 8 vCPU / 32 GiB，需预留集群运行开销；此流程不创建集群 |
| 域名 | OpenShift 实际 `apps` 域；Podman 主机有稳定 DNS 名称，必须从该主机、所有集群节点和 Pod 解析到可达的内网地址 |
| 网络 | 离线主机可访问集群 API 和 Route；节点与 Pod 可访问该主机 TCP **5000、9443、9444**。客户预先配置防火墙/NetworkPolicy，本流程不自动修改客户网络 |
| 源凭证 | 联网制作机上有效的容器认证 JSON，覆盖所需源仓库；Dify Enterprise 私有镜像须有合法拉取权限 |

若目标主机尚无 Python 3.12，可由已有 Ansible 控制端先执行 [主机前置准备](host-prerequisites.md)。Demo 使用已配置的 OS 源；完全离线客户可指定目标上的 RPM 目录。该入口是第 0 步的 OS 准备，不会代替第 3 步包内 Ansible wheels 的离线安装。

可在**已有权限的客户集群终端**取得以下参数，再带到联网制作机；制作机本身无需连接客户集群：

```bash
# customer_ocp_version：客户实际 OpenShift 补丁版本
oc get clusterversion version -o jsonpath='{.status.desired.version}{"\n"}'
# customer_apps_domain：客户实际 apps 域名
oc get ingress.config.openshift.io cluster -o jsonpath='{.spec.domain}{"\n"}'
# customer_storage_class：从已有 StorageClass 中选择适用的数据存储类
oc get storageclass
# customer_context：后续填写离线机 install.yml，不是 prepare.yml 的参数
oc config current-context
```

不要直接使用本文的 `example.com` 域名或假定客户也有 `gp3-csi`。安装会核对真实集群域名、架构、StorageClass 和集群身份，错误参数将停止执行。

## 1. 联网制作机：安装 Ansible 并填写参数

联网制作阶段以当前用户执行，不要求在 `/opt` 写文件。`DIFY_WORK_ROOT` 是有写权限的持久化目录；`DIFY_SOURCE_DIR` 是实际解压后的工程目录。两者只是当前终端的路径变量，不是 Ansible 参数。源码、venv、私有文件和介质应分开放置，介质与私有文件不得位于源码或 Git 目录内。路径使用不含空格的绝对路径。

### 1.1 选择当前环境的路径

**以下两组选一组执行。** 客户通用示例使用当前用户的主目录；若空间不足，将 `DIFY_WORK_ROOT` 换成该用户可写、容量足够的挂载盘：

```bash
export DIFY_WORK_ROOT="$HOME"
export DIFY_SOURCE_DIR="$DIFY_WORK_ROOT/dify-enterprise-openshift"
```

**RHDP Jump 的 `lab-user` 使用这一组**，与当前实际下载解压目录一致：

```bash
export DIFY_WORK_ROOT=/mnt/low-side-data
export DIFY_SOURCE_DIR=/mnt/low-side-data/dify-enterprise-openshift
```

| 内容 | 本节使用的路径 | RHDP Jump 展开后的路径 |
|---|---|---|
| 工程源码 | `$DIFY_SOURCE_DIR` | `/mnt/low-side-data/dify-enterprise-openshift` |
| Ansible venv | `$DIFY_WORK_ROOT/dify-ansible` | `/mnt/low-side-data/dify-ansible` |
| 私有参数/源认证 | `$DIFY_WORK_ROOT/dify-private` | `/mnt/low-side-data/dify-private` |
| 本次介质工作目录 | `$DIFY_WORK_ROOT/dify-customer-media-native-001` | `/mnt/low-side-data/dify-customer-media-native-001` |

后续第 1、2 步在同一个 shell / tmux 会话执行。另开终端后，先重新执行所选的两条 `export`；若源码实际在别处，只修改 `DIFY_SOURCE_DIR`。这些目录均不要求预先存在 `/opt/dify-enterprise-openshift`。

### 1.2 下载源码（已有完整源码则跳过）

源码可以通过压缩包获取，不要求 Git；下面的联网下载示例需要 `curl`。**已有源码且与本次交付版本一致时可跳过下载；首次下载请选择尚不存在的 `DIFY_SOURCE_DIR`。** GitHub 合并不会更新此前下载的压缩包或源码目录；需要更新时，下载到新目录并修改 `DIFY_SOURCE_DIR`，不要覆盖混合新旧文件。以下命令只有下载成功且目标目录不存在时才解压：

```bash
DIFY_DOWNLOAD_DIR=$(mktemp -d)
curl -fL --retry 3 \
  https://github.com/SammZhu/dify-enterprise-openshift/archive/refs/heads/main.tar.gz \
  -o "$DIFY_DOWNLOAD_DIR/main.tar.gz" &&
  test ! -e "$DIFY_SOURCE_DIR" &&
  install -d -m 0755 "$DIFY_SOURCE_DIR" &&
  tar -xzf "$DIFY_DOWNLOAD_DIR/main.tar.gz" -C "$DIFY_SOURCE_DIR" --strip-components=1
```

无论下载还是复用源码，都先确认这两个文件存在：

```bash
ls "$DIFY_SOURCE_DIR/ansible/customer-prepare.yml" \
   "$DIFY_SOURCE_DIR/ansible/customer-prepare.example.yml"
```

### 1.3 安装 Ansible 并复制私有参数

以下就是环境准备的六条命令，全部使用上面选定的用户可写路径：

```bash
cd "$DIFY_SOURCE_DIR/ansible"
python3.12 -m venv "$DIFY_WORK_ROOT/dify-ansible"
"$DIFY_WORK_ROOT/dify-ansible/bin/python" -m pip install -r requirements.txt
install -d -m 0700 "$DIFY_WORK_ROOT/dify-private"
cp -n customer-prepare.example.yml "$DIFY_WORK_ROOT/dify-private/prepare.yml"
chmod 0600 "$DIFY_WORK_ROOT/dify-private/prepare.yml"
```

`cp -n` 保留已有参数文件；已有文件应继续编辑。以普通用户制包时，任务通过 sudo 使用 rootful Podman；先确认授权可用：

```bash
sudo -v
```

将源镜像认证保存到 **`$DIFY_WORK_ROOT/dify-private/source-auth.json`**（0600）。可以使用厂商提供的认证 JSON，或交互登录所需源仓库；将下面的“源仓库域名”替换为实际值，不要把密码写进命令行或 Git：

```bash
podman login --authfile "$DIFY_WORK_ROOT/dify-private/source-auth.json" 源仓库域名
chmod 0600 "$DIFY_WORK_ROOT/dify-private/source-auth.json"
```

RHDP Jump 已有的 `/home/lab-user/pull-secret-example.json` 可直接用于 `customer_source_authfile`，无需重新登录或复制；先确认当前用户可读。使用现有认证时跳过上面的登录命令。

### 1.4 编辑参数文件

**填写位置：联网制作机的 `$DIFY_WORK_ROOT/dify-private/prepare.yml`。** 运行时通过 `-e @文件路径` 读取。不要修改公共默认值或提交真实参数。

先打印所选路径对应的两个参数值，将输出填入 `prepare.yml` 的同名字段；若使用已有认证，第二项填写其实际绝对路径：

```bash
printf 'customer_media_root: %s/dify-customer-media-native-001\ncustomer_source_authfile: %s/dify-private/source-auth.json\n' \
  "$DIFY_WORK_ROOT" "$DIFY_WORK_ROOT"
```

**YAML 中填写实际绝对路径，不要填写 `$DIFY_WORK_ROOT` 字符串，它不会在参数文件中自动展开。** 客户通用填写格式如下；`customer` 是示例用户名，前两项使用上面打印的实际路径，版本、域名及存储类由客户管理员确认：

```yaml
customer_media_root: /home/customer/dify-customer-media-native-001
customer_source_authfile: /home/customer/dify-private/source-auth.json
customer_ocp_version: '4.20.8'
customer_apps_domain: apps.cluster.customer.example.com
customer_registry_host: registry.customer.example.com
customer_storage_class: gp3-csi
```

RHDP Jump 使用现有 pull-secret 时，对应配置如下。这里只是验证环境示例，客户不能照搬 `apps.disco.lab` 或假定已有 `gp3-csi`：

```yaml
customer_media_root: /mnt/low-side-data/dify-customer-media-native-001
customer_source_authfile: /home/lab-user/pull-secret-example.json
customer_ocp_version: '4.20.8'
customer_apps_domain: apps.disco.lab
customer_registry_host: 实际Highside主机DNS名称
customer_storage_class: gp3-csi
```

RHDP 的 Highside DNS 在 **Highside 上**执行 `hostname -f` 取得，填写到 `customer_registry_host`；不要填写 Jump 名称或 `api.disco.lab`。客户填写由管理员确认的离线 Podman 主机 DNS。

六个参数分别表示什么、对应哪一侧：

| 参数 | 作用 | 填什么 / 对应位置 | 获取或准备方式 |
|---|---|---|---|
| `customer_media_root` | 介质制作工作目录，保存工具、导出的镜像、最终压缩包、校验文件和私有日志。 | **联网制作机**上的绝对路径，例如 `/var/tmp/dify-customer-media`；必须在源码目录之外。它不是离线端的解压或安装目录。 | 选择有足够可用空间的目录，Playbook 会创建；空间要求见第 0 步。最终交付文件位于该目录内。 |
| `customer_source_authfile` | 提供联网拉取源镜像的认证，供 Podman 使用；不是离线端 Registry 的认证文件。 | **联网制作机**上已存在的认证 JSON 的绝对路径，例如 `/home/customer/dify-private/source-auth.json`。 | 使用有合法镜像访问权限的 pull-secret，或按上文 `podman login --authfile` 生成；设为 0600。该文件不会自动加入交付包，目标 Registry 认证在离线安装时另行生成。 |
| `customer_ocp_version` | 指定客户 OpenShift 版本，用于下载相应版本的 `oc` 并打入介质。 | **客户目标集群**的实际补丁版本，例如 `4.20.8`，不是制作机的 OS 版本。 | 由客户管理员执行第 0 步的 `oc get clusterversion` 查询；安装时还会核对包内 `oc` 与集群的主次版本匹配。 |
| `customer_apps_domain` | 指定 Dify 使用的 OpenShift 应用域名，生成应用访问配置。 | **客户目标集群**的实际 Ingress 域，例如 `apps.cluster.customer.example.com`；不含 `https://`、端口或路径。 | 由客户管理员执行第 0 步的 `oc get ingress.config.openshift.io cluster` 查询；安装时与真实集群域名核对。 |
| `customer_registry_host` | 指定后续承载 TLS Registry、wheel 索引和本地 S3 的单机地址。 | **离线 Podman 主机**的稳定 DNS 名称，例如 `registry.customer.example.com`；不含协议、端口或路径，不能填成联网制作机地址。默认服务端口为 5000 / 9443 / 9444。 | 由客户管理员提前分配 DNS 并解析到离线主机内网地址；主机自身、集群节点和 Pod 均须可解析并访问。制作介质时不要求连接该主机，但必须先确定名称。 |
| `customer_storage_class` | 为 PostgreSQL、Redis、Qdrant 的三个持久化卷指定 StorageClass。 | **客户目标集群**内已有且可用的存储类名称；`gp3-csi` 只是示例，不会由此参数自动安装存储驱动。 | 客户管理员执行第 0 步的 `oc get storageclass`，选择适用的存储类并确认容量；三个 PVC 的默认大小见第 0 步。 |

**这六项都写在联网制作机的同一份 `prepare.yml` 中，但取值分别来自制作机、离线 Podman 主机和目标集群。** 制作机不需要连接客户集群；客户管理员可以先提供版本、apps 域和存储类名称。涉及域名、存储类等部署配置的值会写入介质，填错后应修改私有配置并用新的工作目录重新制作，不要直接修改已封包的 `bundle/config.yaml`。

源认证文件、参数文件、工作目录都放在源码目录之外。可选参数见 `customer-prepare.example.yml`；`customer_plugins_dir` 接收审核后的 `.difypkg`，`customer_wheels_dir` 接收这些插件所需的完整 wheel 集合。**控制端 wheelhouse 不等于插件依赖闭包**；未提供插件和模型时只验收平台部署。

## 2. 联网制作机：一条 Playbook 制作介质

```bash
cd "$DIFY_SOURCE_DIR/ansible"
"$DIFY_WORK_ROOT/dify-ansible/bin/ansible-playbook" -i localhost, customer-prepare.yml \
  -e "@$DIFY_WORK_ROOT/dify-private/prepare.yml" --ask-become-pass
```

`--ask-become-pass` 提示输入当前用户的 sudo 密码；已配置免密 sudo 或以 root 运行时可省略。不要使用 `sudo ansible-playbook` 切换整个控制端的用户。

依次执行前置检查、校验并下载 oc/Helm、渲染 Chart、导出 OCI 镜像、下载 Python 3.12 的 Ansible 运行依赖、封包。下载失败应先查工作目录的私有日志；不要把失败的半成品当作就绪介质。镜像导出中断且留下半成品时，保留旧目录排查，换一个 `customer_media_root` 重做，避免混合两次导出。

**升级到原生任务或更新源码后重新制作交付介质，请选择新的 `customer_media_root`。** 已封包的目录保持不可变，pull 源码不会更新旧包内的 Playbook；旧脚本版导出不能与新原生介质混合。既有安装的清理可从新版完整源码运行，具体路径见清理指南。

完成后，从 `prepare.yml` 中 **`customer_media_root` 的实际目录**搬运两个文件。以上 RHDP 示例的输出位置是：

```text
/mnt/low-side-data/dify-customer-media-native-001/dify-offline-3.9.8-linux-amd64.tar.gz
/mnt/low-side-data/dify-customer-media-native-001/dify-offline-3.9.8-linux-amd64.tar.gz.sha256
```

离线机从第 3 步起使用 root。由离线机管理员先执行 `sudo -i`，再执行 `install -d -m 0755 /opt/dify-media`，然后将上述两个文件放入该目录。`/opt/dify-media` 是离线机管理员创建的接收目录，和 Jump 的源码路径没有关系。

包内包含 Chart、OCI 镜像与锁文件、oc/Helm、客户 Ansible 入口、控制端 wheels、wheel 索引及文档。源 registry 认证、客户 kubeconfig、目标服务私钥和 Enterprise License 不会自动加入介质。校验文件须通过可信渠道一并交付。

## 3. 离线 Podman 主机：校验并安装 Ansible 运行环境

**从这里开始在离线 Podman 主机执行，不访问公网，使用上一步已打开的 root shell。** 确认第 0 步的 OS 软件已经装好，先验 checksum，再解包：

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

HTTPS Route 验收使用主机系统根证书及可用的集群 router CA；如果客户 Route 使用另外的私有 CA，在同一 `install.yml` 填写 `customer_route_ca: /root/dify-private/route-ca.crt`。它是 OpenShift Route 的信任证书，与 Podman 服务的 `customer_tls_ca` 分开配置。

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

## 6. 清理后重新验证安装

使用新增的 `customer-reset.yml`，操作步骤见 [清理与重装指南](disconnected-reset.md)。默认只读取现场计划，执行需指定模式、启用执行并提供预览确认 hash：

- `application`：删除 Dify 命名空间及其中的 PVC/业务数据，清空专用本地 S3 数据，保留 Registry 镜像和本地服务。
- `full`：进一步删除本项目三个 Podman 容器、专用开机单元和 Registry 数据，再重新导入并安装。

两种模式保留离线介质、证书/认证、集群级共享资源和 Podman 镜像缓存。具体数据丢失范围、PV 回收策略、RHDP 目标路径和失败续跑方法均见指南；本入口不自动清理未知归属的安装。

## 当前验证边界

原 Python 实现的三服务部署、重启和清理曾在 RHDP OpenShift 4.20.8 验证，详见 [历史 RHDP 记录](rhdp-dify-offline.md)。本次原生 Ansible 重构的现场全链路重装尚未完成，不能沿用旧实现的现场结论。4.21、arm64、外部对象存储和客户全新环境也尚无本次重构的现场通过记录。

本地回归执行真实 YAML 任务，涵盖真实 3.9.8 Chart 渲染、派生 Chart/hooks、运行时镜像映射、OCI 摘要、封包与校验、客户安装入口、清理执行/续跑/UID 竞争及共享目录保护。Kubernetes、Podman、systemd 和云边界采用受控模拟；这些测试不等于真实 RHEL 软件包事务、容器启动、云资源创建或客户现场完整安装。License 激活、插件、模型调用和 RAG 需要另外验收。
