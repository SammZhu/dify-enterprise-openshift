# 离线安装的高级配置

客户默认使用 [Quick Start](customer-quickstart.md)；本页是同一套原生 Ansible Role 的扩展参数，不是第二套脚本操作。入口、准备和安装步骤不变，详见 [实现原理](native-ansible.md)。

## 默认与扩展的边界

默认是 RHEL 9、Linux amd64、Python 3.12 控制端、已有 OpenShift 4.20/4.21、Dify Enterprise 3.9.8、固定 namespace/release `dify`。单台 Podman 主机提供 TLS Registry 和 wheel 索引，默认还提供本地 S3。客户提供已有 StorageClass、内网 DNS/连通性、cluster-admin kubeconfig、源镜像授权及 License。

Role 接受 `linux/arm64` 和外部 S3/ODF 分支；它们尚未在真实对应环境验收。所有扩展都应使用新介质目录重新制作，并单独执行 [交付验收](disconnected-validation.md)。其他 namespace、多主机 Registry、高可用存储和平台 Operator 自动安装仍不在范围内。

## 填写位置

联网机的私有 `/root/dify-private/prepare.yml` 中填写：

```yaml
customer_values_overlay: /root/dify-private/values-overlay.yml
```

覆盖文件权限设为 0600，保存在源码目录之外。它在公共默认值和 Quick Start 拓扑参数之后合并；vendor 细项通过 `offline.chartValues` 覆盖。不要修改封好的 `bundle`。秘密写成 `@@secret:name/key@@`，由安装从目标 `dify` namespace 读取；客户需事先以自己的秘密管理流程创建 Secret，明文密码不能放入覆盖文件或 Git。

## 使用已有外部 S3

下面内容填写到上述 `values-overlay.yml`（endpoint、region、bucket、寻址方式应由对象存储管理员确认）：

```yaml
offline:
  localS3:
    enabled: false
  chartValues:
    persistence:
      type: s3
      s3:
        endpoint: https://objects.customer.internal
        bucketName: dify-customer
        region: us-east-1
        accessKey: '@@secret:customer-s3/accessKey@@'
        secretKey: '@@secret:customer-s3/secretKey@@'
        addressType: ''
        useAwsManagedIam: false
        useAwsS3: false
components:
  objectStorage:
    enabled: false
  minio:
    enabled: false
```

安装只启动 Registry 和 wheel 索引两个 Podman 服务，不生成本地 S3 认证，不创建本地 S3 容器，也不执行本地 S3 探针。客户须事先建好 bucket，并确认节点/Pod 可访问 endpoint、证书受信任、权限及认证。外部私有 CA 需要按客户规范配到应用的信任链；Role 不自动接管外部服务。`deployment-ready.json` 会明确外部对象存储仍需独立功能验收。

已有 ODF/RGW 可以通过 `components.objectStorage` 使用仓库内的 OBC Chart；需要客户已安装并配置相应 Operator/StorageClass。关闭 `offline.localS3.enabled` 后再启用该组件，按 `examples/helm/components/objectstorage/values.yaml` 设置，Role 会等待 OBC Bound，之后解析引用的 Secret。不要同时启用 objectStorage 与 minio。

`customer-reset.yml` 目前只支持默认本地 S3 拓扑；不会删除外部 bucket。扩展拓扑的清理需单独审核，不能直接套用默认全量清理。

## arm64

在覆盖文件中设置：

```yaml
offline:
  platform: linux/arm64
```

联网制作机、离线 Podman 主机和目标节点需同为 arm64，使用同一 Python 3.12 ABI 制作控制端 wheels。Role 按平台下载官方 oc/Helm、核对导出镜像架构，并产出 `dify-offline-3.9.8-linux-arm64.tar.gz`；Quick Start 中的压缩包名需相应替换。

必须确认所有 Dify、数据库、服务、插件构建基础镜像及模型镜像都有 arm64 内容；任何一个缺失都应停止，不能强行复用 amd64 归档。wheel 输入同样需匹配插件的 Python ABI 和平台。对应架构的实际部署、插件构建、模型及重启测试仍须现场执行。
