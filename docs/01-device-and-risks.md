# 01 · 机型、模式识别与 macOS 环境坑

## 1. 三种 USB 模式，别搞混

| VID:PID | 模式 | 能做什么 | 怎么进 |
| ------- | ---- | -------- | ------ |
| `0e8d:0003` | **BROM** | linecode 溢出 → 执行 payload → 上传 DA | 长按电源 15s 关机 → **按住音量+** 插 USB → 保持 3–5s |
| `0e8d:2001` | **DA**（Download Agent） | 读写任意分区、调用 SEJ 硬件 | 由 BROM 链打通后自动重枚举 |
| `0e8d:20ff` | **LK / fastboot 下载模式** | 只能刷官方认可的镜像，**做不了 BROM 溢出** | 关机后按音量− 插 USB |

判断当前在哪个模式：

```bash
system_profiler SPUSBDataType | grep -iE "product id|vendor id" | paste - -
# 或
ioreg -p IOUSB -l -w0 | grep -iE 'idVendor|idProduct' | tail -6
```

> **`0e8d:20ff` 不是 BROM。** 在这个模式下扫 linecode offset 是白费功夫，
> 本项目早期就因此在错误模式下反复重插过。

## 2. 会话模型：一次 BROM 只给一个进程

- BROM 会话在**发起它的那个进程退出后立即作废**，不能"接着上一个进程的状态继续"。
- 因此本项目的驱动方式是：**父 watcher 常驻 + 子脚本按会话运行**。
  子进程退出 → 等设备从枚举里消失 → 等下一次出现 → 拉起新的子脚本。
- 失败触发后设备**仍枚举但不响应**（`0e8d:0003` 挂着不动）说明 USB 栈已死，只能断电重进，
  不要对着死会话循环重试。

## 3. macOS 权限矩阵

| 场景 | 用户态（uid 501） | root |
| ---- | ----------------- | ---- |
| 枚举 / 检测 `0e8d:0003` | ✅ | ✅ |
| BROM 控制传输（`ctrl_transfer`） | 视机型/时机，本项目成功轮（`brom_kamakiri2.py`）是 **root** | ✅ |
| BROM 握手后重枚举成 CDC | ❌ `Errno 13`（被系统 `CDC` 驱动占用） | ✅ 可 `detach_kernel_driver` |
| **DA 阶段 `0e8d:2001`** | ✅ **实测用户态可直连并读写 eMMC** | ✅ |

结论：**只有 BROM 阶段需要 root，DA 阶段不需要。** 别一上来就 sudo，
先跑 30 秒最小探测：

```bash
/tmp/rootvenv/bin/python - <<'PY'
import usb.core, usb.util
d = usb.core.find(idVendor=0x0E8D, idProduct=0x0003)
print("found", d)
if d:
    try:
        d.open()
        print("ctrl:", d.ctrl_transfer(0xA1, 0x21, 0, 0, 7).tobytes().hex())
    except Exception as e:
        print("ERR", type(e).__name__, e)
PY
```

- `Errno 13` → 换 root 跑。
- 枚举为 0 但 `system_profiler` 看得到 → **配件授权**问题，见下节。

## 4. 配件授权（Accessory Fabric）

macOS 对 USB 配件有单独授权层。被撤回时的典型报错：

```
darwin_device_from_service ... out of resources
NeedsDeviceAccessEntitlement = Yes
```

处置：

1. 物理拔插（最有效）；
2. 或 **系统设置 → 隐私与安全性 → 配件**，把相关项设为"始终允许"。

**不要循环重试**——`out of resources` 时重试没有意义，只会浪费一轮物理操作。

## 5. root 进程的 TCC 限制

以 root 运行的脚本**读不了 `~/Documents`、`~/Downloads`**（`Operation not permitted`）。
所以本项目所有工作目录固定在 `/tmp`：

| 用途 | 路径 |
| ---- | ---- |
| mtkclient 检出 | `/tmp/mtknew/mtkclient` |
| 带 pyusb 的 venv | `/tmp/rootvenv` |
| 自编译 libusb | `/tmp/brom/libusb/lib`（配 `DYLD_FALLBACK_LIBRARY_PATH`） |
| 分区备份 | `/tmp/backup/` |
| 运行日志 | `/tmp/agent2.log`、`/tmp/agent_fault.log` |

`scripts/brom/setup_env.sh` 会把这套目录建好。

## 6. 系统 CDC kext 不能卸载

SIP 开启时 `kextunload` 返回 exit 71，**不要走卸载驱动这条路**。
正确做法是用 root 让 libusb `detach_kernel_driver()`，或者（更省事）
在 DA 模式下操作——DA 的 `0e8d:2001` 不会被系统 CDC 驱动抢占。
