# 用原生 Ansible 任务准备主机前置软件

入口是 `ansible/host-prerequisites.yml`。它使用标准 `setup`、`dnf`、文件和校验任务，不调用本工程自定义 Python 模块或脚本；安装后检查全部所需 RPM、Python 3.12 与 pip。它只准备 OS 软件，不安装 Dify、创建集群或联网安装 pip 依赖。

**运行位置：已有的 Ansible 控制端。** 控制端本身已有可运行的 Ansible；目标是 RHEL 9.4+ 兼容 Linux amd64，具有系统 Python 3.9+、DNF Python 绑定及 sudo 权限。Playbook 通过已有系统 Python 安装版本化的 Python 3.12，不更改 `/usr/bin/python3` 链接。它不能在没有 Ansible 控制端的情况下自行建立控制端。

默认所需包：`podman openssl httpd-tools tar python3.12 python3.12-pip`。仓库模式使用 `state: present`，不主动执行整机升级；本地 RPM 模式按提供的版本安装并解析依赖。本地 RPM 集必须与目标 OS/架构匹配，包含所需软件及完整依赖，不应混入其他用途的包。

## Demo：在 Jump 上准备 Highside

先完成 [Quick Start 第 1.1～1.3 节](customer-quickstart.md#11-选择当前环境的路径)，在 Jump 的 `lab-user` shell 使用已设置的路径变量、现有控制端与 `inventory.example.yml` 的 `highside` SSH 别名：

```bash
cd "$DIFY_SOURCE_DIR/ansible"
"$DIFY_WORK_ROOT/dify-ansible/bin/ansible-playbook" -i inventory.example.yml host-prerequisites.yml -e prerequisite_target=highside -e prerequisite_mode=repositories
```

`repositories` 模式访问目标机已经配置的 OS 软件源，不添加新源。Demo 若可访问其 RHUI 软件源，可用来完成 Quick Start 第 0 步；客户完全隔离环境应使用内网 OS 源或下面的本地 RPM 模式。不能把 Demo 的 RHUI 访问例外当作物理隔离验证。

成功标志为 `failed=0`，输出包含 `Python 3.12.x` 和对应 pip 版本。若尚未制包，继续 Quick Start 第 2 步；若介质已制好并搬运，直接继续第 3 步，不必重做介质。离线端安装 Ansible 时继续使用交付包的 `controller-wheels` 和 `--no-index`，无需在 Highside 联网下载 Ansible。

## 完全离线：由已有控制端安装目标上的 RPM 集

由 OS 管理员准备与目标版本/架构匹配的可信 RPM 和依赖，搬到目标专用目录，例如 `/mnt/high-side-data/dify-os-rpms`。本 Playbook 不自动下载 RPM，也不自动导入未知签名公钥；目标需已有相应发行版的可信公钥。

```bash
cd "$DIFY_SOURCE_DIR/ansible"
"$DIFY_WORK_ROOT/dify-ansible/bin/ansible-playbook" -i inventory.example.yml host-prerequisites.yml -e prerequisite_target=highside -e prerequisite_mode=local_rpms -e prerequisite_rpm_dir=/mnt/high-side-data/dify-os-rpms
```

客户使用自己的 inventory 和现成控制端，将 `prerequisite_target` 替换为其中的目标主机/组。目录路径是**受管目标机上的路径**，不是控制端上的目录。

本地模式只安装目录直属的普通 `.rpm` 文件，禁用全部仓库和 DNF 插件，先检查已安装的系统 DNF 绑定，并用单次事务的临时配置强制 `localpkg_gpgcheck=1`；事务结束删除临时配置，不修改系统 DNF 配置。安装后检查所有必需包；缺依赖、签名不可信或目录不匹配会失败，不回退到在线仓库。机制见 [Ansible DNF 模块](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/dnf_module.html) 和 [DNF 签名配置](https://dnf.readthedocs.io/en/latest/conf_ref.html)。

## 预演与范围

在对应命令后加 `--check`。仓库模式由 DNF 进行检查，可能读取软件源元数据；本地 RPM 模式只检查目标平台、目录和输入清单，**不验证完整依赖闭包或签名，也不执行事务**。签名和安装结果须在正式执行时验证。

本入口只负责 OS 层。制包、镜像/Chart 处理、安装、清理与验收由 [客户原生 Role](native-ansible.md) 执行。当前 OS 任务只做过本地静态及只读预演检查，没有在真实 RHEL 主机执行软件包事务；正式安装仍需核实 RPM 依赖、签名和运行结果。
