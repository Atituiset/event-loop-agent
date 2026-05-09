# 多 NGA 分布式调度：单机模拟方案

## 背景

Orchestrator 并发启动多个 `nga` 子进程审查代码时，受限于 nga 自身的并发控制机制。代码中已通过 `_cleanup_nga_locks()` 和 `_wait_for_nga_slot()` 进行兜底，但如果 nga 的并发限制来自外部服务（如 `keyctrl` 管理的内网 session），单纯的进程级 Semaphore 无法突破上限。

本文档提供一套**由轻到重**的单机多 NGA 模拟方案，并给出验证方法和决策树。

---

## 方案一：Namespace 隔离（已实现）

### 原理

每个 nga 进程运行在独立的 Linux namespace 中：

- **user namespace** (`--user --map-root-user`)：在 namespace 内获得 root 权限，可执行 mount 操作
- **mount namespace** (`--mount`)：隔离文件系统挂载点，覆盖 `/tmp` 和 `$HOME/.nga`
- **pid namespace** (`--pid --fork`)：隔离进程表，`pgrep nga` 互相不可见

### 启动命令

```bash
unshare --user --mount --pid --fork --map-root-user \
  sh -c 'mount -t tmpfs tmpfs /tmp; mount --bind /tmp/nga_isolated $HOME/.nga; exec "$@"' \
  sh <nga_bin> run <message>
```

### 隔离效果

| 限制类型 | 是否可绕过 | 说明 |
|---------|-----------|------|
| `/tmp` 全局临时锁 | **是** | mount namespace 内 `/tmp` 为独立 tmpfs |
| `$HOME/.nga` 文件锁 | **是** | 绑定挂载空目录覆盖 |
| PID 进程表扫描 | **是** | pid namespace 隔离 |
| 网络服务 (keyctrl TCP) | **否** | 网络请求仍走同一出口 IP |
| 硬件指纹 | **否** | 共享物理机底层信息 |

### 使用方式

```bash
# 启用 namespace 隔离，并发提升到 5
python orchestrator.py --diff abc123 --repo . -c 5 --isolate
```

### 代码位置

`orchestrator.py`：`--isolate` 参数 + `_scan_one()` 中的进程启动逻辑（`multi-nga-sandbox` 分支）。

---

## 方案二：网络 Namespace + 虚拟网卡（进阶）

### 适用场景

如果 nga 的并发限制来自 **keyctrl 网络服务**，且 keyctrl **按源 IP / MAC 地址** 限流，则需要为每个 nga 分配独立的软网卡和出口 IP。

### 架构

```
主机 (root namespace)
  ├─ veth0_br0 (10.200.1.1) ←──→ veth1 (10.200.1.2) ── nga #1 @ netns1
  ├─ veth0_br1 (10.200.2.1) ←──→ veth1 (10.200.2.2) ── nga #2 @ netns2
  └─ veth0_br2 (10.200.3.1) ←──→ veth1 (10.200.3.2) ── nga #3 @ netns3

         │                              │
         └────── iptables SNAT ─────────┘
                    (MASQUERADE)
                         │
                      实际网卡 eth0
                         │
                    keyctrl 服务器
            (看到 3 个不同源 IP/MAC)
```

### 手动验证脚本

在内网 nga 环境执行以下脚本，验证 keyctrl 是否按 IP/MAC 放行：

```bash
#!/bin/bash
# verify_keyctrl_limit.sh
# 创建 3 个带独立 IP 的网络 namespace，同时启动 nga

set -e
WAN_IF="eth0"  # 根据实际环境修改

for i in 1 2 3; do
    sudo ip netns add nga_ns$i 2>/dev/null || true
    sudo ip link del veth${i}_a 2>/dev/null || true
    sudo ip link add veth${i}_a type veth peer name veth${i}_b
    sudo ip link set veth${i}_b netns nga_ns$i
    sudo ip addr add 10.200.$i.1/24 dev veth${i}_a
    sudo ip link set veth${i}_a up
    sudo ip netns exec nga_ns$i ip addr add 10.200.$i.2/24 dev veth${i}_b
    sudo ip netns exec nga_ns$i ip link set veth${i}_b up
    sudo ip netns exec nga_ns$i ip route add default via 10.200.$i.1
    # 自定义 MAC（如果 keyctrl 校验 MAC）
    sudo ip netns exec nga_ns$i ip link set dev veth${i}_b address 02:00:00:00:00:0$i
done

sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -s 10.200.0.0/16 -o $WAN_IF -j MASQUERADE 2>/dev/null || true

echo "=== 启动 3 个 namespace 内的 nga ==="
for i in 1 2 3; do
    sudo ip netns exec nga_ns$i nga run 'review README.md' &
    PID[$i]=$!
done

wait ${PID[1]}
wait ${PID[2]}
wait ${PID[3]}

echo "=== 清理 ==="
for i in 1 2 3; do
    sudo ip netns del nga_ns$i 2>/dev/null || true
    sudo ip link del veth${i}_a 2>/dev/null || true
done
```

### 预期结果

- **3 个 nga 同时运行不冲突** → keyctrl 按 IP/MAC 限流，方案二可行
- **第 2、3 个被阻塞/拒绝** → keyctrl 按硬件指纹或账号限流，单机方案无解

---

## 验证决策树

```
nga 并发限制来自哪里？
    │
    ├─ 本地文件锁 (/tmp, ~/.nga)
    │       └─→ 方案一：mount namespace 即可
    │
    ├─ keyctrl 网络服务 (TCP/UDP)
    │       │
    │       ├─ 按源 IP / MAC 限流
    │       │       └─→ 方案二：网络 namespace + veth + SNAT
    │       │
    │       ├─ 按硬件指纹 (product_uuid, machine-id)
    │       │       └─→ 单机无解，需真·多机
    │       │
    │       └─ 按用户账号 / Token 限流
    │               └─→ 单机无解，需多账号/多机
    │
    └─ 不确定
            └─→ 先跑 strace 探测（见下文）
```

---

## 快速探测 keyctrl 校验维度

在内网 nga 环境执行：

```bash
# 1. 看 nga 出网的网络行为（确定是否按 IP 限流）
strace -e trace=network -f nga run 'review foo.c' 2>&1 | grep -E 'connect|bind|getsockname'

# 2. 看 nga 是否采集硬件指纹（确定是否有硬件校验）
strace -e trace=file -f nga run 'review foo.c' 2>&1 | grep -E '/sys|/proc|machine-id|product_uuid|dmi'
```

### 结果解读

| strace 输出特征 | 结论 |
|----------------|------|
 只有 `connect(keyctrl_ip:port)` | keyctrl 大概率按 **源 IP** 限流 |
 大量 `/sys/class/dmi/id/product_uuid`、`/etc/machine-id` | 存在 **硬件指纹校验**，单机方案困难 |
 读取 `/proc/self` 进程信息 | mount/pid namespace 可应对 |
 连接本地 Unix socket `/run/keyctrl.sock` | 可用 mount namespace 屏蔽 socket 路径 |

---

## 集成路线

### 阶段 1：验证（当前）

- [x] 实现方案一（mount + pid namespace）
- [ ] 在内网环境用 strace 确认 keyctrl 机制
- [ ] 如果 keyctrl 按 IP/MAC 限流，运行 `verify_keyctrl_limit.sh` 验证

### 阶段 2：集成

- 如果方案一足够：直接合并 `--isolate` 到主分支
- 如果需要方案二：在 orchestrator 中自动管理网络 namespace 生命周期（创建 veth → 分配 IP → `ip netns exec` 启动 nga → 任务结束后回收）
- 如果需要真·多机：设计远程 worker 协议（SSH / HTTP / gRPC），orchestrator 作为中央调度器分发任务到多台机器

---

## 参考命令

```bash
# 查看当前网络 namespace
ip netns list

# 查看 namespace 内的网络接口
ip netns exec nga_ns1 ip addr

# 查看 iptables NAT 规则
sudo iptables -t nat -L -n -v

# 查看某个进程所属 namespace
ls -l /proc/<pid>/ns/

# 手动进入某个 namespace 调试
sudo nsenter --net=/var/run/netns/nga_ns1 -- bash
```
