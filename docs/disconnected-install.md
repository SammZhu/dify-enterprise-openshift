# 单机 Podman → 离线 OpenShift 安装

本路径不要求客户已有 Git、Harbor、Quay 或 OpenShift GitOps。联网侧准备一个固定版本的交付包；客户侧一台 Linux Podman 主机提供 TLS Registry 和 Python wheel 索引，通过本地 Helm Chart 安装到已运行的离线 OpenShift。

RHDP 已有离线集群时，可使用 [Dify 部署自动化入口](rhdp-dify-offline.md) 串联以下阶段。该入口使用 9443 wheel 端口，另提供 9444 TLS S3，以适配 Workshop 的现有 8443 mirror-registry。

**验证状态：2026-11-02 已在真实 RHDP OpenShift Disconnected Workshop 的 OpenShift 4.20.8 上完成 Dify Enterprise 3.9.8 部署验收：19 个运行 Pod Ready、四个 PostgreSQL 数据库、Redis、集群内认证 TLS S3 写读删除及 HTTPS 入口检查通过。实测使用单机 rootful Podman、本地 S3 和 gp3-csi，详见 [实测记录](rhdp-dify-offline.md#实测记录)。该环境有代理出口限制及 AWS/RHUI 例外，并非物理断网；其他客户拓扑、4.21/ARM、License 离线激活、模型、插件及 RAG 尚未据此验收。**

## 1. 边界与前提

```text
联网准备机：源码 + 官方 Chart + 镜像 + 插件包 + Linux wheels + 可选模型
                            │ OCI archives / 校验文件 / 本地源码
                            ▼ 受控介质传递
客户 Linux Podman 主机
  ├─ HTTPS Registry :5000 ────────── OpenShift 节点拉镜像
  │                         └───── Kaniko 拉构建基础镜像、推插件镜像
  ├─ HTTPS Python 索引 :8443 ─────── 插件构建下载预置 wheels
  └─ 本地 oc / Helm ─────────────── OpenShift API :6443
                                        └─ 内网 LLM / Embedding / S3
```

- 第一版固定 Dify Enterprise 3.9.8、release/namespace `dify`；面向全新安装。不会接管既有在线 Helm release，也不自动迁移数据库、存储或 GitOps 管理权。
- OpenShift 4.20 / 4.21 集群、Router、DNS、StorageClass 已就绪。集群安装与升级、离线 Operator Catalog 不在本脚本范围。
- 客户侧建议 RHEL Linux，已经安装 Podman、Python **3.9+**、venv/pip、OpenSSL、`htpasswd`（RHEL 的 `httpd-tools`）。这些 OS 软件包需要客户 OS 的离线安装介质提供。
- `oc`、Helm 3 和 PyYAML 6.0.3 可以预装，也可以通过交付包附带。准备机需要相同工具及 Podman。`oc` 使用与集群匹配的版本；Helm 本地验证使用 3.16.4。
- 第一版仅支持单一 Linux CPU 架构：`linux/amd64` 或 `linux/arm64`。集群及客户 Podman 服务主机应使用相同架构；镜像没有对应架构时导出直接失败。不要假定所有插件镜像都支持 ARM。
- Podman 主机的真实 DNS 名必须能被所有节点、Pod 和准备导入的主机解析和访问；不要使用 `localhost` 或仅修改运维笔记本的 hosts。开放 TCP 5000、8443，以及主机到 OpenShift API 的访问。
- Podman 主机、Registry 存储与 CA/认证资料需要保留。它是单点服务，故障时新 Pod 拉取、扩容和插件构建会受影响。磁盘至少容纳镜像缓存、OCI 交付包、Registry 三份内容以及插件增长量，按实际清单测算。
- 存储默认复用**已有 ODF MCG**。没有 ODF 时，按第 7 节连接客户可用的内网 S3；本方案不自动安装 ODF。PostgreSQL、Redis、Qdrant 沿用本项目的单实例示例，不代表客户生产环境的 HA 设计。
- Dify Enterprise License、镜像访问权限及插件材料需要合法取得。离线激活、续期、集群重建后的授权迁移由 Dify 确认，不假设点击在线激活在断网时可用。

## 2. 联网侧准备

在工程根目录操作。安装 Python 依赖，获取官方 Chart；此后所有离线命令仅使用本地 `.tgz`，不再使用 `--repo`。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r offline/requirements.txt
helm pull dify-enterprise --repo https://charts.openshift.io --version 3.9.8 -d /tmp
cp examples/helm/values-disconnected.yaml customer.yaml
```

编辑 `customer.yaml`：

```yaml
deployer:
  domain: apps.ocp.customer.internal
offline:
  platform: linux/amd64
  registry: registry.customer.internal:5000
  pipMirror: https://registry.customer.internal:8443/simple/
components:
  postgresql:
    storage:
      storageClassName: customer-rwo
  redis:
    storage:
      storageClassName: customer-rwo
  qdrant:
    storage:
      storageClassName: customer-rwo
```

域名、Registry 和模型模式在准备时确定；封包后不直接编辑 `config.yaml`。需要修改时重新准备交付包，保留旧包用于追溯。原有在线入口和默认配置继续使用。

### 2.1 固定插件清单和依赖

将已批准的 `.difypkg` 放到 `approved-plugins/`，将插件需要的**全部传递依赖** wheel 放到 `plugin-wheels/`。以实际插件的 Python 版本、CPU 架构、基础镜像 ABI 和 lockfile 为准，在相同构建环境准备并验证。

示例：为 Python 3.12 / x86_64 的插件解析 wheels（`plugin-requirements.lock` 是该插件已核定的完整锁定依赖）：

```bash
python3 -m pip download --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 312 --abi cp312 \
  -r plugin-requirements.lock -d plugin-wheels
```

3.9.8 Chart 默认还支持 Python 3.13、3.14 构建基础镜像；需要使用时分别准备对应 ABI 的 wheels。某些依赖需要更高 manylinux 基线或原生库，必须以实际基础镜像验证。没有 wheel 的依赖应在联网、同构建环境生成；不允许到了客户现场才尝试从公网编译下载。直接 Git URL、HTTP URL、模型下载及外部系统库也要逐项消除或内网化。

工具不会从 `.difypkg` 自动解析、证明依赖闭包，也不会绕过插件签名校验。它只搬运已准备材料，并生成静态 PEP 503 索引。**没有提供 wheels 时允许准备平台包，但会打印插件依赖尚未准备的提示，不能视为插件功能已验收。**

### 2.2 附带客户主机工具

客户没有 Helm/oc/PyYAML 时，准备：

```text
target-tools/        # 客户 Linux CPU 架构的可执行文件，保留执行权限
  helm
  oc
target-tool-wheels/  # 客户主机 Python ABI 的 PyYAML wheel
```

例如客户 RHEL 9 使用 Python 3.9 / x86_64：

```bash
python3 -m pip download --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 39 --abi cp39 \
  -r offline/requirements.txt -d target-tool-wheels
```

下载 Helm/oc 时核对供应商校验值；不要把准备机 macOS 二进制作为客户 Linux 工具交付。

### 2.3 先审清单，再导出

仅渲染清单，不拉镜像：

```bash
python3 scripts/offline.py prepare \
  --values customer.yaml --chart /tmp/dify-enterprise-3.9.8.tgz \
  --output /tmp/dify-offline-plan
```

检查 `images.lock.yaml`。它包含 Pod/initContainer 镜像及不会直接出现在 Deployment 中的 Kaniko、插件 gateway、Python 构建基础镜像等。默认无内置 Embedding 的配置，目前真实 Chart 渲染产生 **28 个源镜像引用**，包含不同短名/全名别名；组件选择变化后数量会变化，不作为固定总数。

登录受限的源镜像仓库后，导出完整包：

```bash
python3 scripts/offline.py prepare --export \
  --values customer.yaml --chart /tmp/dify-enterprise-3.9.8.tgz \
  --plugins approved-plugins --wheels plugin-wheels \
  --tools target-tools --tool-wheels target-tool-wheels \
  --output offline-bundle-customer-3.9.8
python3 scripts/offline.py verify --bundle offline-bundle-customer-3.9.8
tar czf dify-offline-customer-3.9.8.tar.gz offline-bundle-customer-3.9.8
sha256sum dify-offline-customer-3.9.8.tar.gz > dify-offline-customer-3.9.8.tar.gz.sha256
```

不需要某个可选目录时，省略对应参数。输出目录必须不存在，失败后不要把部分导出的目录当作交付物；换新目录重试。

`images.lock.yaml` 记录每个引用、实际解析地址、原始 `sourceImageId`、归档实际加载用的 `imageId`、单平台 OCI manifest digest，以及可取得的源 Registry digest。Docker 转 OCI 可能改变 config 摘要，因此源 ID 与加载 ID 不要求相同；导出器会校验归档 manifest/config 的内容摘要。`plugins.lock.yaml` 记录插件包的 SHA256 和可读取的名称/版本元数据；`checksums.json` 覆盖 Chart、配置、源码、镜像归档、插件包、wheels 和附带工具。安装手册与验收清单也随包携带。`planned` 状态的清单会被 `verify`、`import` 和 `install` 拒绝。

## 3. 客户侧启动 Podman 服务

搬运压缩包及通过可信渠道取得的外部校验值。先校验，再解压；校验值用于检测内容变化，不替代来源验证。

```bash
sha256sum -c dify-offline-customer-3.9.8.tar.gz.sha256
tar xzf dify-offline-customer-3.9.8.tar.gz
export BUNDLE="$PWD/offline-bundle-customer-3.9.8"
export STATE="$PWD/offline-state-customer"
export PATH="$BUNDLE/bin:$PATH"
python3 -m venv offline-venv
. offline-venv/bin/activate
pip install --no-index --find-links "$BUNDLE/tool-wheels" -r "$BUNDLE/requirements.txt"
python3 "$BUNDLE/source/scripts/offline.py" verify --bundle "$BUNDLE"
```

已经预装依赖时可省略 venv 安装步骤。`STATE` 必须位于 `BUNDLE` 外，否则新增运行文件会破坏交付包文件列表校验。

### 3.1 TLS 与认证

优先使用客户 CA 签发的证书。准备 `server.crt`（含证书链）、`server.key`、`ca.crt`，证书 SAN 必须包含 `offline.registry` 的真实主机名；证书也用于同主机的 HTTPS wheel 服务。

没有企业 CA 的测试环境，可以**在客户侧**生成一个专用 CA 和服务证书：

```bash
umask 077
mkdir -p "$STATE/certificates"
export REGISTRY_HOST=registry.customer.internal
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -keyout "$STATE/certificates/ca.key" -out "$STATE/certificates/ca.crt" \
  -subj '/CN=Dify Offline Registry CA' -addext 'basicConstraints=critical,CA:TRUE'
openssl req -new -newkey rsa:3072 -nodes \
  -keyout "$STATE/certificates/server.key" -out "$STATE/certificates/server.csr" \
  -subj "/CN=$REGISTRY_HOST"
cat > "$STATE/certificates/server.ext" <<EOF
subjectAltName=DNS:$REGISTRY_HOST
basicConstraints=critical,CA:FALSE
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -days 365 -in "$STATE/certificates/server.csr" \
  -CA "$STATE/certificates/ca.crt" -CAkey "$STATE/certificates/ca.key" -CAcreateserial \
  -extfile "$STATE/certificates/server.ext" -out "$STATE/certificates/server.crt"
htpasswd -Bc "$STATE/htpasswd" offline
```

`htpasswd` 交互输入 Registry 账号密码；使用 bcrypt。证书、私钥、认证文件仅存客户现场，**不放进交付包或公共 Git**。

```bash
bash "$BUNDLE/source/scripts/start-offline-services.sh" \
  "$BUNDLE" "$STATE" \
  "$STATE/certificates/server.crt" "$STATE/certificates/server.key" "$STATE/htpasswd"
curl --cacert "$STATE/certificates/ca.crt" -I "https://$REGISTRY_HOST:5000/v2/"
curl --cacert "$STATE/certificates/ca.crt" "https://$REGISTRY_HOST:8443/simple/"
mkdir -p "$STATE/podman-certs"
cp "$STATE/certificates/ca.crt" "$STATE/podman-certs/ca.crt"
podman login --authfile "$STATE/auth.json" --cert-dir "$STATE/podman-certs" "$REGISTRY_HOST:5000"
chmod 600 "$STATE/auth.json"
```

Registry 未带认证时返回 `401` 是正常的 TLS 连通结果；wheel 索引应返回 HTML。进一步用 `curl --user offline` 交互认证确认 `/v2/` 返回 200。Registry 需要认证；wheel 索引提供内网只读下载，不启用匿名写入。

服务脚本只加载包内的 Registry/nginx 镜像，不在线拉取。若同名容器已经存在，它会停止并提示检查，**不会删除已有 Registry 或存储**。重启现有服务用 `podman start dify-offline-registry dify-offline-pypi`。正式长期运行时由客户将容器纳入 Quadlet/systemd、证书续期和主机备份；`--restart` 本身不等同于已配置开机启动。

wheel 服务端口从 `offline.pipMirror` 读取，可改成同一主机的 `https://<host>:9443/simple/`；必须与 Registry 端口及其他服务端口不同。目录中的公开 wheels 会设置为容器 nginx 用户可读，交付包外层和私有状态目录仍保留私有权限。

## 4. 导入镜像

```bash
python3 "$BUNDLE/source/scripts/offline.py" import \
  --bundle "$BUNDLE" --state "$STATE" \
  --authfile "$STATE/auth.json" --ca "$STATE/certificates/ca.crt"
```

工具加载 OCI 归档，再推送到 `registry.customer.internal:5000/dify-offline/`。以**目标 Registry 返回的 digest**生成 `STATE/image-map.json`，不假定源、目标 digest 必然相同。

对于 Kaniko 和插件 Python 构建基础镜像，会在离线主机上使用 `--pull=never --network=none` 构建 CA 派生镜像：Kaniko 增加证书目录；Python 增加 SSL/pip/uv 的内网 CA 配置。派生镜像的目标 digest 同样写入映射。这些构建镜像不是 Dify 的 UBI 主服务镜像，不宣称新增派生镜像获得了厂商认证。

`ca.crt` 应包含插件将访问的全部内部 CA。证书信任分三层：节点拉镜像使用 `additionalTrustedCA`，Dify 服务使用 Chart 的 `global.customCA`，插件构建使用上述派生镜像。只有节点信任配置不足以证明 Kaniko/pip 已可用。

导入可重复执行，已有层由 Registry 去重；导入失败时不会生成新的完整映射。不要手工修改映射或把它用于另一个交付包。

## 5. 本地安装，不使用 GitOps

准备客户 kubeconfig 并选择明确的 context。安装需要创建 SCC/CRD、命名空间、RBAC 和更新 Registry CA 信任的权限；不要对现有在线 Demo 集群直接执行。

```bash
oc config get-contexts
export OCP_CONTEXT=customer-offline
oc --context "$OCP_CONTEXT" whoami --show-server
python3 "$BUNDLE/source/scripts/offline.py" install \
  --bundle "$BUNDLE" --state "$STATE" --context "$OCP_CONTEXT" \
  --authfile "$STATE/auth.json" --ca "$STATE/certificates/ca.crt"
```

安装顺序：

1. 校验包及导入记录、CPU 架构和目标 context。
2. 保留原有 Registry CA，合并当前 CA；创建 namespace 和拉取/推送凭据。
3. 本地渲染并应用 SCC、RBAC、凭据生成 Job、DifyPlugin CRD；等待 Job/CRD 就绪。
4. 安装数据库、对象存储声明和可选 Embedding，等待各自就绪。
5. 从集群 Secret 解析 Dify values，通过本地 Chart 安装 Dify；等待工作负载就绪。

脚本不创建 `Application`、`Subscription` 或 `CatalogSource`。无需 GitHub、公共 Helm 仓库或客户 Git 服务。既有 Registry CA 不会被整份覆盖。Registry 节点信任下发可能需要时间，若最初拉取报 x509，应检查 CA 配置传播和节点状态，修复后重复安装，不关闭 TLS 校验。

Dify 主服务及运行时插件配置中的镜像引用都替换为内网地址。插件 generator 的 base64 Secret 也会处理。Helm 的 post-renderer 不处理 hooks，因此安装时还会生成临时派生 Chart，替换原 Chart 中测试 hook 的硬编码镜像，并加入拉取 Secret；交付包中的官方原始 Chart 保留不动。

凭据生成 Job 保留既有数据库密码；额外的应用、内部 API 和 Dashboard 签名密钥生成于集群，重装时复用。解析后的 values 仅写入权限 0600 的临时文件，随后清理；Helm release Secret 仍包含安装 values，需要客户正常管理 Secret 读取权限。错误输出不回显 Helm/oc 的完整 stderr，以免输出凭据；失败时依据失败的阶段检查 Job、Pod、Events 和 Helm 状态。

重复安装可重建本项目已完成/失败的初始化 Job，不会清空数据库或 PVC。不会替换正在运行的 Job，不会接管非本项目 Job。现有非离线安装的 `dify` Helm release 会被拒绝；已有 ArgoCD 管理的环境先另行规划迁移。

## 6. 模型、插件、License

- 默认关闭内置 Embedding：在 Dify 控制台配置客户内网 LLM 和 Embedding 端点。若需要 Rerank，也使用内网服务；不能继续使用 Demo 的 LiteMaaS 地址。
- 在 Dify UI 中从交付包的 `plugins/` 安装批准的插件；保留官方签名校验。检查每个插件的构建 Pod、内部基础镜像、HTTPS wheel 下载和最终镜像推送。
- 通过 Dify 确认的离线流程激活 License，并验证重启后仍有效；这一步不是脚本可以替代的。
- 关闭 Marketplace 只关闭公共入口，不保证插件自身不会访问公网 API。模型、工具、文档解析、插件自带模型和工作流中的每个外部端点都要检查。

### 可选：内置离线 Embedding

需要时在联网的同架构 Podman 环境导出完整 Ollama cache（包含 manifests 和 blobs）：

```bash
mkdir -p model-context/models
podman pull --platform linux/amd64 docker.io/ollama/ollama:latest
podman run --rm --name dify-model-download \
  -e OLLAMA_MODELS=/models -v "$PWD/model-context/models:/models:Z" \
  --entrypoint /bin/sh docker.io/ollama/ollama:latest -ec '
    ollama serve &
    pid=$!
    trap "kill $pid 2>/dev/null || true" EXIT
    until ollama list >/dev/null 2>&1; do sleep 2; done
    ollama pull nomic-embed-text
  '
podman build --pull=never --network=none \
  --build-arg BASE_IMAGE=docker.io/ollama/ollama:latest \
  -f offline/Containerfile.models -t localhost/dify-offline-model:v1 model-context
```

然后在封包前更新客户配置：

```yaml
components:
  embedder:
    enabled: true
    pullModel: false
    modelImage: localhost/dify-offline-model:v1
```

`localhost/` 镜像必须已存在于准备机本地；导出时不会尝试远程拉取。运行侧的 initContainer 把模型拷贝到 PVC，再启动 Ollama；模型缺失就报错，不回退公网下载。模型镜像可能很大，需为模型镜像、PVC 预留容量。模型语义/维度变化时应评估并重建索引，不仅比较向量维度。

## 7. 没有 ODF 时使用其他内网 S3

保持数据层组件启用，关闭 ODF 声明；在 `offline.chartValues` 中完整指定 S3。使用 Secret 引用，不将凭据写入配置或交付包：

```yaml
components:
  objectStorage:
    enabled: false
  minio:
    enabled: false
offline:
  chartValues:
    persistence:
      type: s3
      s3:
        endpoint: https://s3.customer.internal
        accessKey: '@@secret:customer-s3/accessKey@@'
        secretKey: '@@secret:customer-s3/secretKey@@'
        bucketName: dify
        region: us-east-1
        addressType: ''
        useAwsManagedIam: false
        useAwsS3: false
```

安装前由客户创建 namespace `dify`、`customer-s3` Secret 和 bucket；S3 CA 需纳入传入的 CA bundle。路径寻址等设置以实际 S3 服务及当前 Chart 验证为准。

项目保留的 MinIO 公共镜像当前不能假设可下载。没有现成 S3 时，应先选择客户可取得、可支持的内网对象存储；不要直接开启旧 `minio` 示例并认为镜像一定可用。

### 可选：同一 Podman 主机上的测试 S3

没有 ODF/现成 S3 的实验环境可在封包前设置：

```yaml
components:
  objectStorage:
    enabled: false
  minio:
    enabled: false
offline:
  localS3:
    enabled: true
    image: docker.io/chrislusf/seaweedfs:4.51
    port: 9444
    bucket: dify
```

封包会携带这张额外镜像。服务脚本生成随机认证，保存在工程外的 `STATE/local-s3.json` / `local-s3.env`，通过同一证书的 nginx 提供 TLS 9444，内部 SeaweedFS 8333 及管理端口不发布。安装器从私有状态创建 Dify 所需的 S3 Secret。Registry、wheel、S3 端口必须互不冲突。持久化数据在 `STATE/s3-data`，应与认证一起保留和备份。

这是单机演示配置，未声明客户生产 S3 支持认证；按实际客户对象存储验证 API、容量和恢复。镜像原始 UID/GID 也需要保留：本轮 RHDP 中 Sandbox 的文件所有权超出默认 rootless 映射范围，自动化入口在两侧一致使用 rootful Podman。通用交付应按客户的主机管理方式核定执行身份和用户命名空间映射。

## 8. 验收及故障定位

```bash
python3 "$BUNDLE/source/scripts/offline.py" check --bundle "$BUNDLE" --context "$OCP_CONTEXT"
```

该命令只读检查 Pod Ready、Pod/initContainer 镜像全部在内网 Registry、四个 PostgreSQL 数据库和 Redis PING。它不调用旧 `preflight-check.sh --fix`，不会尝试修复 LiteMaaS 等在线 Demo 配置。

完整验收见 [disconnected-validation.md](disconnected-validation.md)。平台脚本检查通过后仍要执行插件安装、S3 读写、模型调用、知识库入库与问答。问题定位：

| 现象 | 优先检查 |
|---|---|
| ImagePullBackOff / DNS | Registry 主机名是否所有节点可解析；端口、防火墙、认证 Secret |
| 节点拉镜像 x509 | `image.config.openshift.io/cluster` 的 CA 引用和已有 CA 合并结果 |
| 插件构建 x509 | 是否使用导入后的 CA 派生 Kaniko/Python 镜像；CA 是否与服务证书一致 |
| 插件依赖找不到 | wheel 索引、版本/ABI/平台标记、传递依赖和直接 URL 引用 |
| PVC Pending | StorageClass、容量、访问模式、存储服务状态 |
| `@@secret` 无法解析 | 对象存储 Claim 是否 Bound、对应 Secret 是否具备键；不要手改占位符 |
| 重启后无法拉镜像 | Podman 主机及服务是否已恢复、证书/账号是否到期、Registry 数据是否保留 |

## 9. 更新与维护

相同 Dify 3.9.8 版本可重新准备带新插件/镜像的包，并导入后重复安装。升级到新 Dify/Chart 版本需要重新核对 schema、插件 generator、hooks、镜像与数据库迁移；第一版脚本会拒绝其他版本，不承诺跨版本升级自动安全。

生产使用前补充数据库/S3/PVC 备份恢复、容量与 HA 设计。此项目仍保留原有 [支持边界说明](../README.md#two-boundaries-stated-up-front)。

参考：[OpenShift Registry CA](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/registry/configuring-registry-operator)、[Podman push](https://docs.podman.io/en/stable/markdown/podman-push.1.html)。
