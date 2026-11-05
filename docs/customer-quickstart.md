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

### 源镜像认证：先选择已有文件或交互登录

**已有有效容器认证 JSON 时，直接把其实际绝对路径填写到 `customer_source_authfile`，无需重新登录或复制。** 认证应由镜像供应商提供或按其授权流程取得，并覆盖所需源仓库；不要把普通门户账号密码当作所有镜像仓库通用的凭证。

**RHDP Jump 先检查既有文件**，下面只检查可读性，不输出内容：

```bash
if test -r /home/lab-user/pull-secret-example.json; then
  printf '%s\n' '已有源认证文件可读；prepare.yml 使用 /home/lab-user/pull-secret-example.json'
else
  printf '%s\n' '既有认证文件不可读；先按 Workshop Guide 核对文件位置和授权，不执行后面的制包。' >&2
fi
```

文件可读时，在私有 `prepare.yml` 中填写：

```yaml
customer_source_authfile: /home/lab-user/pull-secret-example.json
```

使用这个文件的 RHDP 用户跳过下面的交互登录段，继续第 1.4 节。可读只说明文件存在且可读，实际镜像权限将在拉取时验证；若某个仓库拒绝访问，应补充该仓库的合法授权。

**只有没有现成认证 JSON，且供应商要求登录获取授权时，才执行这一段。** 按提示输入供应商提供的实际源仓库 DNS 名称，不含 `https://` 或路径。用户名及密码/Token 也使用该仓库要求的凭证，不写进命令行或 Git：

```bash
read -r -p '请输入供应商提供的源镜像仓库 DNS 名称：' DIFY_SOURCE_REGISTRY
if [[ "$DIFY_SOURCE_REGISTRY" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?$ ]]; then
  podman login --authfile "$DIFY_WORK_ROOT/dify-private/source-auth.json" "$DIFY_SOURCE_REGISTRY" &&
    chmod 0600 "$DIFY_WORK_ROOT/dify-private/source-auth.json"
else
  printf '%s\n' '仓库地址未填写或格式无效，未执行登录。请填写实际 DNS 名称，不含协议和路径。' >&2
fi
```

只有登录成功才会设置认证文件权限。生成的文件对应 `customer_source_authfile` 为 `$DIFY_WORK_ROOT/dify-private/source-auth.json` 展开后的实际绝对路径；登录失败时先处理失败原因，不继续制包。

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

### 1.5 确认离线主机 OS 前置软件

这是第 0 步要求的实际检查点；先完成第 1.3 节，保证控制端已有 Ansible。目标主机缺少 Python 3.12、Podman 等软件时，按 [主机前置准备](host-prerequisites.md) 安装；已有全部软件则跳过。

**RHDP：在 Jump 的 `lab-user` shell 执行**，通过 `highside` SSH 别名安装 Highside 所需 RPM：

```bash
cd "$DIFY_SOURCE_DIR/ansible"
"$DIFY_WORK_ROOT/dify-ansible/bin/ansible-playbook" \
  -i inventory.example.yml host-prerequisites.yml \
  -e prerequisite_target=highside \
  -e prerequisite_mode=repositories
```

成功标志为 `failed=0`，输出包含 Python 3.12 和对应 pip 版本。该模式使用 Highside 已配置的 RHUI/OS 源；它是操作系统准备，不属于应用介质的断网安装。完全离线客户应使用已准备好的内网 OS 源或指南中的 `local_rpms` 模式。

**若已完成第 2 步制包和搬运，现在才发现 Highside 缺软件，补做本节即可，成功后继续第 3 步；无需重新制包。**

## 2. 联网制作机：一条 Playbook 制作介质

```bash
cd "$DIFY_SOURCE_DIR/ansible"
"$DIFY_WORK_ROOT/dify-ansible/bin/ansible-playbook" -i localhost, customer-prepare.yml \
  -e "@$DIFY_WORK_ROOT/dify-private/prepare.yml" --ask-become-pass
```

`--ask-become-pass` 提示输入当前用户的 sudo 密码；已配置免密 sudo 或以 root 运行时可省略。不要使用 `sudo ansible-playbook` 切换整个控制端的用户。

依次执行前置检查、校验并下载 oc/Helm、渲染 Chart、导出 OCI 镜像、下载 Python 3.12 的 Ansible 运行依赖、封包。下载失败应先查工作目录的私有日志；不要把失败的半成品当作就绪介质。镜像导出中断且留下半成品时，保留旧目录排查，换一个 `customer_media_root` 重做，避免混合两次导出。

**升级到原生任务或更新源码后重新制作交付介质，请选择新的 `customer_media_root`。** 已封包的目录保持不可变，pull 源码不会更新旧包内的 Playbook；旧脚本版导出不能与新原生介质混合。既有安装的清理可从新版完整源码运行，具体路径见清理指南。

### 2.1 查看本次介质的镜像清单

文件名是 **`images.lock.yaml`**，位于 `customer_media_root` 指定的目录下面的 **`bundle/images.lock.yaml`**。它在联网端的 **export 阶段全部镜像导出成功后**生成；首次制包之前或导出中途可能尚不存在，不需要手工创建。

| 所在位置 | 镜像清单路径 |
|---|---|
| 客户联网制作机 | `customer_media_root` 的实际值下面的 `bundle/images.lock.yaml` |
| 本文 RHDP Jump 示例 | `/mnt/low-side-data/dify-customer-media-native-001/bundle/images.lock.yaml` |
| 离线 Podman 主机按第 3 步解包后 | `/opt/dify-media/bundle/images.lock.yaml` |

本次 RHDP 示例在 **Jump** 上查看：

```bash
cat /mnt/low-side-data/dify-customer-media-native-001/bundle/images.lock.yaml
```

若更改了 `customer_media_root`，查看命令也要使用相应的实际目录。清单中 `source` 是源镜像引用，`resolvedSource` 是补全仓库域名后的引用（如 `docker.io/...`），`archive` 是包内 OCI 归档的相对路径，还记录镜像身份及摘要。以本次文件为准，不能把某个版本的示例镜像表当作所有交付包的固定内容。

`pull-secret-example.json` 或 `source-auth.json` 用于提供仓库认证；`images.lock.yaml` 记录实际导出的镜像，两者用途不同。镜像清单只读查看，不应手工改写；改动会破坏封包校验。若导出失败、文件尚未生成，先查看本次工作目录的 `logs/last-error.json`，不要创建一个空清单继续安装。

### 2.2 搬运交付文件

完成后，从 `prepare.yml` 中 **`customer_media_root` 的实际目录**搬运两个文件。以上 RHDP 示例的输出位置是：

```text
/mnt/low-side-data/dify-customer-media-native-001/dify-offline-3.9.8-linux-amd64.tar.gz
/mnt/low-side-data/dify-customer-media-native-001/dify-offline-3.9.8-linux-amd64.tar.gz.sha256
```

客户将这两个文件搬入离线机管理员创建的接收目录。客户默认由管理员先在离线机执行 `sudo -i`，再执行 `install -d -m 0755 /opt/dify-media`，然后搬入文件；接收目录与 Jump 源码路径无关。离线机从第 3 步起使用 root。

**RHDP 搬运示例：在 Jump 的 `lab-user` shell 执行。** 下例使用第 2.3 节重试成功的 `native-002`；首次成功介质若为 `native-001`，两条路径都应改为实际编号。已经搬运完成则跳过，不必复制到 `/opt`：

```bash
ssh highside 'install -d -m 0700 /mnt/high-side-data/dify-customer-media-native-002'
scp /mnt/low-side-data/dify-customer-media-native-002/dify-offline-3.9.8-linux-amd64.tar.gz \
  /mnt/low-side-data/dify-customer-media-native-002/dify-offline-3.9.8-linux-amd64.tar.gz.sha256 \
  highside:/mnt/high-side-data/dify-customer-media-native-002/
```

两个文件均成功传输后继续第 3 步。

包内包含 Chart、OCI 镜像与锁文件、oc/Helm、客户 Ansible 入口、控制端 wheels、wheel 索引及文档。源 registry 认证、客户 kubeconfig、目标服务私钥和 Enterprise License 不会自动加入介质。校验文件须通过可信渠道一并交付。

### 2.3 制包失败后的处理

`customer_plugins_dir`、`customer_wheels_dir` 可以不填写；未提供插件 wheels 时仍会生成合法的空 HTML 索引，平台部署制包不应因此失败。若旧版本在 `Generate the public simple index root` 报 `src (or content) is required`，应更新源码使用修复后的任务，不要创建假 wheel 或手工改写 bundle 来绕过错误。

本版在提权异步任务启动前建立私有 `logs/` 和 `logs/async/`；旧运行留下的 root 所有日志目录，只在确认路径是专用普通目录后修正这两个目录的归属，不递归修改文件。正常失败将原始结果写到 `logs/last-error.json`（0600）。若诊断写入也失败，终端仍报告原始失败任务，不再用保存日志的错误替代它。

**失败留下的未封存 bundle 不用于离线安装，也不要在原目录手工补文件后继续。** 保留旧目录，按第 1.2 节更新源码后，在私有 `prepare.yml` 选择新的介质目录，例如本次 RHDP 从 `dify-customer-media-native-001` 改为：

```yaml
customer_media_root: /mnt/low-side-data/dify-customer-media-native-002
```

然后按第 2 步重新制包。原 rootful Podman 缓存不需要清理；新一轮仍会按镜像引用检查源并重新导出、校验和封包。若 `last-error.json` 未能写入，以终端中最先出现的业务任务错误及最后报告的原始任务名定位，不把“日志不存在”视为已恢复。

## 3. 离线 Podman 主机：校验并安装 Ansible 运行环境

### 3.1 登录离线主机并选择路径

客户管理员在离线 Podman 主机执行 `sudo -i`。**RHDP 则从 Jump 执行下面一行，进入 Highside 的 root shell**：

```bash
ssh -t highside 'sudo -i'
```

**以下两组路径选一组。** 客户通用默认路径（介质已按第 2.2 节放好）：

```bash
export DIFY_MEDIA_DIR=/opt/dify-media
export DIFY_CONTROLLER_DIR=/opt/dify-ansible
export DIFY_PRIVATE_DIR=/root/dify-private
export DIFY_STATE_ROOT=/var/lib/dify-offline
```

**RHDP Highside 使用这一组**（以下为已成功搬运的 `native-002`）：

```bash
export DIFY_MEDIA_DIR=/mnt/high-side-data/dify-customer-media-native-002
export DIFY_CONTROLLER_DIR=/mnt/high-side-data/dify-ansible
export DIFY_PRIVATE_DIR=/mnt/high-side-data/dify-private
export DIFY_STATE_ROOT=/mnt/high-side-data/dify-bootstrap
```

`DIFY_MEDIA_DIR` 指向本次成功介质所在目录，不能指向已失败或已删除的 `native-001`。`DIFY_STATE_ROOT` 保存服务、证书、凭证及安装状态，和介质目录用途不同。RHDP 清理后重装沿用原 `/mnt/high-side-data/dify-bootstrap`，保留的证书和认证由安装器校验后复用；已有安装须先完成第 6 步清理，不能用切换状态目录的方式绕过归属检查。

第 3～5 步在同一个 Highside/离线机 root shell 中执行；重新登录或 `sudo -i` 后先重新执行所选的四条 `export`。这些变量只用于终端命令，不要把变量名写入 YAML。

### 3.2 校验介质并离线安装控制端

确认第 1.5 节 OS 软件已就绪。以下命令不访问公网；**任一命令失败即停止，不继续后面的命令**：

```bash
cd "$DIFY_MEDIA_DIR"
sha256sum -c dify-offline-3.9.8-linux-amd64.tar.gz.sha256
tar -xzf dify-offline-3.9.8-linux-amd64.tar.gz
python3.12 -m venv "$DIFY_CONTROLLER_DIR"
"$DIFY_CONTROLLER_DIR/bin/python" -m pip install --no-index \
  --find-links "$DIFY_MEDIA_DIR/bundle/controller-wheels" \
  -r "$DIFY_MEDIA_DIR/bundle/ansible/requirements.txt"
"$DIFY_CONTROLLER_DIR/bin/ansible-playbook" --version
```

成功标志：SHA256 输出 `OK`；依赖安装成功，最后显示 `ansible-playbook [core 2.20.10]`。

本次只是文档路径说明更新，已成功封存并搬运的介质可继续使用，不需要为更新说明而重新制包。旧包内的说明不会自动更新；继续本次验证时以本页第 3～5 步为准，使用包内原有安装入口，不向已封存 bundle 复制新版文档。以后重新制包会自动携带更新后的指南。

这些 Python 命令仅建立 Ansible 运行环境。不要修改解压后的 `bundle`：安装会再次校验完整文件清单及 SHA256。

## 4. 离线 Podman 主机：填写安装参数并运行

```bash
install -d -m 0700 "$DIFY_PRIVATE_DIR"
cp -n "$DIFY_MEDIA_DIR/bundle/ansible/customer-install.example.yml" "$DIFY_PRIVATE_DIR/install.yml"
chmod 0600 "$DIFY_PRIVATE_DIR/install.yml"
vi "$DIFY_PRIVATE_DIR/install.yml"
```

`cp -n` 保留已有私有参数文件；重装时仍须核对下面四项。客户默认路径由客户管理员把目标集群 kubeconfig 保存到 `/root/dify-private/kubeconfig`（0600）。编辑 `/root/dify-private/install.yml`：

```yaml
customer_bundle_dir: /opt/dify-media/bundle
customer_state_root: /var/lib/dify-offline
customer_kubeconfig: /root/dify-private/kubeconfig
customer_context: customer-admin  # 换成 kubeconfig 中客户确认的实际 context
```

**RHDP：在上面打开的 `install.yml` 中填写以下四项绝对路径/值**，使用现有集群管理员 kubeconfig，无需复制或在聊天中粘贴其内容：

```yaml
customer_bundle_dir: /mnt/high-side-data/dify-customer-media-native-002/bundle
customer_state_root: /mnt/high-side-data/dify-bootstrap
customer_kubeconfig: /mnt/high-side-data/rhdp-bootstrap/cluster/auth/kubeconfig
customer_context: admin
```

本例 `admin` 须与第 0 步查到的实际 context 一致。若介质编号或状态目录不同，填写现场实际值；`customer_bundle_dir` 和 `customer_state_root` 须分别对应第 3.1 节的介质和状态目录。保存并退出编辑器后执行本节安装命令。

如使用客户签发的 TLS 证书，同时提供 `customer_tls_cert`、`customer_tls_key` 和 `customer_tls_ca`，证书 SAN 必须覆盖 Podman 主机 DNS。未指定时生成并持久保存私有 CA；安装把 CA 合并到 OpenShift 镜像信任配置，不覆盖已有 CA。证书轮换需单独安排，重跑不会自动更换旧密钥。

HTTPS Route 验收使用主机系统根证书及可用的集群 router CA；如果客户 Route 使用另外的私有 CA，在同一 `install.yml` 填写 `customer_route_ca: /root/dify-private/route-ca.crt`。它是 OpenShift Route 的信任证书，与 Podman 服务的 `customer_tls_ca` 分开配置。

```bash
cd "$DIFY_MEDIA_DIR/bundle/ansible"
"$DIFY_CONTROLLER_DIR/bin/ansible-playbook" -i localhost, customer-install.yml \
  -e "@$DIFY_PRIVATE_DIR/install.yml"
```

流程为 `preflight → verify → services → import → install → check`：绑定明确的集群身份，启动三个 TLS 服务，安装专用 systemd 开机单元，离线导入并固定镜像 digest，部署权限/数据层/Dify，最后进行在线状态验收。首次安装可能等待节点 CA 信任配置生效和 Pod 启动。主机上的服务使用 **rootful Podman**，查看时使用 `sudo podman ps`。

## 5. 查看结果与重跑验收

成功标志：第 4 步 Play Recap 为 `failed=0`，并生成与本次安装对应的 `$DIFY_STATE_ROOT/deployment-ready.json`。客户默认位置为 `/var/lib/dify-offline/deployment-ready.json`，RHDP 为 `/mnt/high-side-data/dify-bootstrap/deployment-ready.json`。在同一 root shell 只读查看：

```bash
cat "$DIFY_STATE_ROOT/deployment-ready.json"
```

其中 `routes` 列出实际 HTTPS 地址和状态，`localS3Roundtrip` 表示认证 TLS S3 读写删除测试通过。浏览器使用实际 Dify Route 访问；私有签发的 Route 证书须按客户规范信任。

只重新验收：

```bash
cd "$DIFY_MEDIA_DIR/bundle/ansible"
"$DIFY_CONTROLLER_DIR/bin/ansible-playbook" -i localhost, customer-install.yml \
  -e "@$DIFY_PRIVATE_DIR/install.yml" \
  -e '{"customer_install_stages":["preflight","verify","check"]}'
```

主机重启后的恢复与复验：将上述阶段列表改为 `["preflight","verify","services","check"]`。完整 Playbook 也可在相同介质、状态目录和集群上重跑；复用已有归属匹配的服务、凭证和数据，不自动清空 PVC。阶段必须按原顺序选择并以 `preflight` 开始；缺少前置制品的后期阶段会失败。

`--check` 只检查编排计划，不连接目标集群，也不能代替制品校验与现场验收。命令失败时，管理员在状态目录 `logs/` 查看私有日志；不要直接上传日志、`state/`、kubeconfig 或认证文件到 GitHub。并行运行会被租约阻止，异常中断后应先确认旧进程已经结束再处理残留租约。

验收包括 Pod Ready、内部镜像引用、四个 PostgreSQL 数据库、Redis、TLS S3 和 HTTPS 入口；**未提供离线 License 时不代表 Enterprise 激活成功，也不代表文件上传、插件、模型调用或 RAG 已验收**。完整业务检查项见[验收清单](disconnected-validation.md)。本单机拓扑没有高可用能力。

### 5.1 已部署完成，更新验收代码后单独复验

若旧介质在 `Query PostgreSQL databases using container credential environment` 失败，先确认诊断原因。旧版本 SQL 引号转义可能让 `psql` 把查询拆成多个参数；这不是缺少数据库或密码错误的通用解决方法。修复版任务使用 YAML 参数列表和独立 Shell 字符串，SQL 作为单个参数传递，凭证仍只从容器环境读取。

**不要修改已封存 bundle 中的任务、重算其校验文件、清理数据或重新制作大介质。** 将已审核的修复版完整源码另行搬到离线机，在介质目录之外运行其客户入口，并继续使用第 4 步原 `install.yml`。源码只更新验收实现，原介质仍须通过其完整校验。若是应用部署本身失败，应先修复部署，不能用只运行 `check` 跳过未完成阶段。

**RHDP：本节修复合并到 `main` 后，在 Jump 的 `lab-user` shell 获取并搬运操作源码**（已有文件时用新文件名，避免覆盖其他正在使用的源码）：

```bash
curl -fL --retry 3 \
  https://github.com/SammZhu/dify-enterprise-openshift/archive/refs/heads/main.tar.gz \
  -o /mnt/low-side-data/dify-acceptance-source.tar.gz &&
scp /mnt/low-side-data/dify-acceptance-source.tar.gz \
  highside:/mnt/high-side-data/dify-acceptance-source.tar.gz
```

**随后在 Highside 的 root shell 执行**，先按第 3.1 节重新设置 RHDP 四个路径变量。下面新建独立的修复源码目录；如目录已存在应先确认其版本，或选择另一个尚不存在的目录，不要覆盖混合版本：

```bash
export DIFY_REPAIR_SOURCE_DIR=/mnt/high-side-data/dify-acceptance-source-pgsql
test ! -e "$DIFY_REPAIR_SOURCE_DIR" &&
  install -d -m 0700 "$DIFY_REPAIR_SOURCE_DIR" &&
  tar -xzf /mnt/high-side-data/dify-acceptance-source.tar.gz \
    -C "$DIFY_REPAIR_SOURCE_DIR" --strip-components=1
```

解包成功后，核对原 `install.yml` 的 `customer_bundle_dir` 仍为本次介质、`customer_state_root` 仍为原状态目录，然后单独重跑验收：

```bash
cd "$DIFY_REPAIR_SOURCE_DIR/ansible"
"$DIFY_CONTROLLER_DIR/bin/ansible-playbook" -i localhost, customer-install.yml \
  -e "@$DIFY_PRIVATE_DIR/install.yml" \
  -e '{"customer_install_stages":["preflight","verify","check"]}'
```

完全离线客户通过批准的交付渠道搬运同一修复源码，选择自己的独立源码目录和第 3.1 节客户路径；不需要离线机访问 GitHub、安装 Git 或联网安装 pip 包。此流程只重做前置检查、介质校验及验收，不重跑服务、镜像导入或 Helm 安装；验收会短暂创建 S3 测试 Job/对象，并在结束时清理。

成功标志仍是第 5 步的 `failed=0` 和本次生成的 `deployment-ready.json`。修复后新制作的介质已携带新版任务，正常按第 3～5 步操作，不需要本节的额外源码目录。

### 5.2 从浏览器访问离线 Dify Console

第 5 步的 HTTP 200 是离线主机侧验收结果；本机浏览器还需要内网访问路径、内网 DNS 和 Route 证书信任。客户已有 VPN、专线或运维桌面时，使用客户批准的访问方式；下面是 **RHDP Workshop** 的两种选择，不需要重新安装 Dify，也不需要开放集群公网入口。

#### 方式 A：Showroom 的 Jump 桌面（首选）

在本次环境的 Demo Portal 打开 **Lab Guide / Showroom → Desktop**（SSH Terminal 旁边），然后在 **Jump 桌面中的 Firefox** 打开第 5 步报告中的 Console 地址。本次集群为：

[https://dify-console.apps.disco.lab/apps](https://dify-console.apps.disco.lab/apps)

这是 [Workshop Guide 第 5 章的浏览器访问方式](https://showroom-showroom-2c8jd.apps.cluster1.openshift.shared.redhatworkshops.io/content/modules/lab05.html#_log_in_to_the_openshift_web_console)：浏览器运行在 Jump，使用其内网 DNS 和网络路径，Guide 已配置实验集群 Web 证书信任。上述 Showroom 链接属于本次申请的环境；以后申请时必须从新环境 Portal 打开对应 Guide。此方式不需要在你本机配置代理；地址栏应保持真实 HTTPS 域名，不替换为 Jump IP 或 `localhost`。

#### 方式 B：本机浏览器经 Jump 的 SOCKS5 隧道

**运行位置：你自己的 Mac / Linux 终端，不是 Jump 或 Highside。** 需要本机已安装 OpenSSH 客户端，以及 Firefox 或下述 macOS Chrome。

**先查 Jump 地址，再执行下面的命令：**

1. 在 Demo Portal 打开本次申请的 **OpenShift Disconnected Workshop**，点击上方 **Info** 标签页（不是 Showroom 的 SSH Terminal）。
2. 在 **Access Details** 区域找到 **2 - Jump Instance** 表格，查看其中 **SSH** 一行。
3. 这一行显示连接命令：取 `ssh lab-user@` 后面的完整 IP 或 DNS。下面出现输入提示时，只输入这一部分，再按回车；不要粘贴整条 SSH 命令、`lab-user@` 或 Password 行。

选择 **Jump Instance** 的地址，不选择 **High Side Instance** 的内网地址；`highside` 是 Jump 内部使用的 SSH 别名，也不是这里的输入值。环境重新申请后地址可能变化，每次以当前 Info 页为准，不沿用旧聊天或其他环境的地址。

查到地址后，在本机终端执行：

```bash
printf '请输入 Info 页 Jump Instance 的 SSH 行中 @ 后面的 IP 或 DNS：'
read -r DIFY_JUMP_HOST
ssh -N -D 127.0.0.1:1080 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  "lab-user@$DIFY_JUMP_HOST"
```

按 SSH 提示使用当前环境凭证认证，不把密码写入命令或文件。首次连接须核对主机指纹；环境重建引起指纹变化时先核对目标，不关闭主机密钥检查。登录后前台没有 shell 提示符是正常现象，**保持这个终端打开**。本机 `127.0.0.1:1080` 只接收本机 SOCKS 请求，由 Jump 解析目标域名并连接内网服务。OpenSSH `-D` 的行为见 [官方手册](https://man.openbsd.org/ssh)。

**Firefox 用户：**建议先通过 Firefox 的 `about:profiles` 新建并启动一个专用实验配置文件，再进入 **设置 → 常规 → 网络设置 → 设置 → 手动配置代理**，填写：

| 项目 | 填写值 |
|---|---|
| HTTP 代理 / HTTPS 代理 | 留空 |
| SOCKS 主机 | `127.0.0.1` |
| 端口 | `1080` |
| SOCKS 版本 | **SOCKS v5** |
| 使用 SOCKS v5 时代理 DNS / Proxy DNS when using SOCKS v5 | **勾选** |
| 不使用代理的地址 / No proxy for | 本专用配置可保留 `localhost, 127.0.0.1`；不要加入 `.lab`、`disco.lab` 或 Console 域名 |

代理设置见 [Firefox 官方说明](https://support.mozilla.org/en-US/kb/connection-settings-firefox)，SOCKS 版本与代理 DNS 的含义见 [Mozilla Proxy 配置说明](https://firefox-admin-docs.mozilla.org/reference/policies/proxy/)。必须让内网 DNS 经 SOCKS 代理解析，不能依赖本机公网 DNS。此设置只作用于该 Firefox 配置文件，不会自动让其他浏览器或 Codex 内置浏览器经过代理。保存后使用该 Firefox 打开上面的 Console 地址。

**macOS Chrome 用户：**复用上面仍在运行的 SSH 隧道，跳过 Firefox 设置。在你自己的 Mac 另开一个终端执行以下命令，使用专用用户数据目录启动 Chrome：

```bash
install -d -m 0700 "$HOME/dify-browser-access"
open -na "Google Chrome" --args \
  --user-data-dir="$HOME/dify-browser-access/chrome-socks-test" \
  --proxy-server="socks5://127.0.0.1:1080" \
  --no-first-run \
  "https://dify-console.apps.disco.lab/apps"
```

`--user-data-dir` 将本次实验的代理参数、Cookie 和登录状态与日常 Chrome 分开；不使用日常 Chrome 的用户数据目录，也不需要登录 Google 账号。`open -na` 请求新实例，已有日常 Chrome 可继续使用。**Chrome 参数使用 `socks5://`，不是 curl 的 `socks5h://`**；SOCKS5 的 HTTP/HTTPS 目标域名由代理解析，依据 [Chromium SOCKS 说明](https://www.chromium.org/developers/design-documents/network-stack/socks-proxy/)。`--no-first-run` 跳过首次运行引导。本次主命令使用已验证的简化参数，不追加 host resolver 规则。本配置无需修改 macOS 系统代理。

在新开的实验 Chrome 打开 `chrome://version`，检查 **Profile Path / 个人资料路径** 位于 `dify-browser-access/chrome-socks-test` 下，**Command Line / 命令行**包含上述代理参数，然后在该窗口访问 Console。若改变端口或启动参数，先关闭使用这一专用目录的全部 Chrome 实例，再用新参数启动；不要只在已有窗口中开新标签页。此命令只覆盖 macOS，其他 OS 不照搬 `open` 命令。

#### Route 私有 CA：出现证书不可信时

SSH 隧道不改变 HTTPS 证书验证。本次 RHDP 使用集群 router CA；本机若尚未信任它，先取回**公开 CA 证书**并核对指纹。客户自签 Route 应使用客户管理员提供的 CA，而不是照搬 RHDP 的证书位置。这里的 Route CA 与 Podman Registry 的 CA 不同。

**先在 Jump 的 `lab-user` shell 执行**，只导出 `router-ca` Secret 的公开 `tls.crt` 字段，不导出私钥、kubeconfig 或认证文件。以下 oc 路径使用本次 `native-002`；其他介质目录应换成现场实际位置：

```bash
install -d -m 0700 "$HOME/dify-browser-access"
(set -o pipefail
  ssh highside "sudo -n /mnt/high-side-data/dify-customer-media-native-002/bundle/bin/oc --kubeconfig /mnt/high-side-data/rhdp-bootstrap/cluster/auth/kubeconfig -n openshift-ingress-operator get secret router-ca -o 'jsonpath={.data.tls\.crt}'" |
    base64 -d > "$HOME/dify-browser-access/route-ca.crt"
)
openssl x509 -in "$HOME/dify-browser-access/route-ca.crt" -noout -fingerprint -sha256
```

任一步失败先停止。记录上述 SHA256 指纹；不要把服务器叶子证书或来源不明的 CA 当作根证书导入。

**在本机另开一个终端**（隧道终端继续保持运行），按上面的 Portal 查找步骤重新输入同一个 Jump 地址，然后下载公开 CA 并核对指纹：

```bash
printf '请输入 Info 页 Jump Instance 的 SSH 行中 @ 后面的 IP 或 DNS：'
read -r DIFY_JUMP_HOST
install -d -m 0700 "$HOME/dify-browser-access"
scp "lab-user@${DIFY_JUMP_HOST}:dify-browser-access/route-ca.crt" \
  "$HOME/dify-browser-access/route-ca.crt"
openssl x509 -in "$HOME/dify-browser-access/route-ca.crt" -noout -fingerprint -sha256
```

两侧指纹一致后，按使用的浏览器配置证书信任：

- **Firefox：**在专用配置文件的 **设置 → 隐私与安全 → 证书 → 查看证书 → 证书颁发机构 → 导入** 中选择该文件，按实验用途确认信任其标识网站，再刷新 Console 页面；此路径不需要修改系统证书库。
- **macOS Chrome：**若已信任当前 Route CA，可直接访问；若显示 `NET::ERR_CERT_AUTHORITY_INVALID`，按客户/设备管理规范在 **钥匙串访问（Keychain Access）→ 登录（login）** 导入 `$HOME/dify-browser-access/route-ca.crt`。双击导入的 CA，再次比对 SHA256 指纹，展开 **信任（Trust）**，将 **安全套接字层（SSL）** 设为 **始终信任（Always Trust）**，保存时由你在系统提示中认证。关闭并重新启动实验 Chrome，再访问真实 HTTPS 域名。操作说明见 [Apple 证书信任指南](https://support.apple.com/guide/keychain-access/change-the-trust-settings-of-a-certificate-kyca11871/mac)，Chrome 对本地证书信任的处理见 [Chromium Root Store FAQ](https://chromium.googlesource.com/chromium/src/+/main/net/data/ssl/chrome_root_store/faq.md)。

**Chrome 的独立目录不隔离 macOS 钥匙串信任。** 导入 login 钥匙串的 CA 会影响当前用户使用该信任存储的其他应用；这里不导入系统（System）钥匙串。若设备策略不允许导入，使用已配好信任的 Showroom 桌面。只信任已核对的实验 CA，不使用忽略证书错误的启动参数，也不从证书告警页绕过校验。

#### 验证代理路径与结束使用

可以先在本机第二个终端验证 SOCKS、代理 DNS 和 TLS。以下使用上一步已核对的 Route CA；`socks5h` 表示把域名交给代理解析，`--noproxy ''` 防止本机已有 `NO_PROXY` 让本次测试绕过隧道，见 [curl 官方说明](https://curl.se/docs/manpage.html#--socks5-hostname)：

```bash
curl --noproxy '' --proxy socks5h://127.0.0.1:1080 \
  --cacert "$HOME/dify-browser-access/route-ca.crt" \
  --connect-timeout 10 --max-time 30 \
  -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  https://dify-console.apps.disco.lab/apps
```

成功标志为 `HTTP 200`，并且专用 Firefox 或实验 Chrome 能显示 Dify 页面（登录、初始化或授权页面均需按现场状态继续处理）；页面可达不代表 License 或业务验收完成。本次环境已实际验证 Jump 直连及本机 SOCKS5/代理 DNS/CA 校验访问，均返回 200；2026-11-05 用户已按上述简化命令自行启动 macOS Chrome 并确认 Console 页面加载成功。实际登录、License 激活及业务操作仍未验收。

| 现象 | 检查位置 |
|---|---|
| SSH 提示端口已占用 | 将隧道、所用浏览器和 curl 的端口一起改为未占用端口，例如 `1081` |
| 域名无法解析 | Firefox 是否勾选代理 DNS，Chrome 的专用窗口是否包含 SOCKS 参数；是否有绕过 `.lab` 的规则；curl 是否使用 `socks5h` |
| 代理拒绝连接或超时 | 本机隧道终端是否仍在运行；Portal 环境和集群是否启动；是否在本机而不是 Jump 建立监听 |
| 证书不可信或名称不符 | 核对当前环境的 Route CA、指纹和地址栏域名；不使用 `-k` 或关闭证书验证 |
| `ERR_INTERNET_DISCONNECTED`，但同一 SOCKS 路径 curl 返回 200 | 使用上面的简化 Chrome 命令及新的 `chrome-socks-test` 目录，不沿用旧 `chrome-profile` 窗口或附加 `--host-resolver-rules`；若该新目录也已有进程，先关闭对应实验实例再重开。现场此组合已恢复页面加载；同时更换了参数和配置目录，未单独证明某一个参数是唯一根因 |
| curl 成功但浏览器失败 | Firefox 是否使用配置了代理/CA 的专用配置；Chrome 是否使用实验目录及当前 Route CA 信任，`chrome://version` 参数是否正确 |

结束时先退出专用 Firefox 配置（或恢复该配置原代理设置）/关闭实验 Chrome，再在隧道终端按 `Ctrl+C`。若环境销毁后不再需要该 CA，Firefox 在专用配置中移除其信任；macOS Chrome 在 login 钥匙串按已记录的指纹定位，只删除本次导入的实验 CA 或恢复其先前信任设置。日常 Chrome 的代理设置未改动；再次测试时重新运行隧道和实验 Chrome 启动命令。操作不更改集群安装、Podman 服务或业务数据；已制好的介质无需更新。旧介质内文档不会自动变化，本节更新以在线指南为准，新制介质会随 Quick Start 一同携带本节。

## 6. 清理后重新验证安装

使用新增的 `customer-reset.yml`，操作步骤见 [清理与重装指南](disconnected-reset.md)。默认只读取现场计划，执行需指定模式、启用执行并提供预览确认 hash：

- `application`：删除 Dify 命名空间及其中的 PVC/业务数据，清空专用本地 S3 数据，保留 Registry 镜像和本地服务。
- `full`：进一步删除本项目三个 Podman 容器、专用开机单元和 Registry 数据，再重新导入并安装。

两种模式保留离线介质、证书/认证、集群级共享资源和 Podman 镜像缓存。具体数据丢失范围、PV 回收策略、RHDP 目标路径和失败续跑方法均见指南；本入口不自动清理未知归属的安装。

## 当前验证边界

2026-11-05 已在 RHDP OpenShift 4.20.8 使用客户原生入口完成制包、部署，并在修复 PostgreSQL 验收命令后定向复验通过，详见 [本次现场记录](rhdp-dify-offline.md#2026-11-05客户原生入口现场验证)。原 `native-002` 介质未修改，修复代码从独立源码目录运行；这不是修复版重新制包后一次不中断的完整安装记录。当前原生实现的重启恢复、清理后再重装、RHDP 适配入口全流程，以及 4.21、arm64、外部对象存储和客户全新环境仍需分别验证。旧实现的历史部署、重启和清理记录不能替代这些验证。

本地回归执行真实 YAML 任务，涵盖真实 3.9.8 Chart 渲染、派生 Chart/hooks、运行时镜像映射、OCI 摘要、封包与校验、客户安装入口、清理执行/续跑/UID 竞争及共享目录保护。Kubernetes、Podman、systemd 和云边界采用受控模拟；这些测试不等于真实 RHEL 软件包事务、容器启动、云资源创建或客户现场完整安装。License 激活、插件、模型调用和 RAG 需要另外验收。
