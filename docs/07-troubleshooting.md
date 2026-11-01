# 07 · 错误码、失败签名与已证伪路线

本文只列**本机真实遇到过**的东西。没遇到的猜测不写进来。

---

## 1. BROM / DA 阶段状态码

| 码 | 含义 | 处置 |
| --- | --- | --- |
| `0x1A1D` | 寄存器访问被拒 / cache issue | 未取得访问时的**基线**。未绕过前读 `0x40`、`0x9C2C`、`0x100A00` 全是它；出现不代表 SRAM 永久不可写 |
| `0x1B1D` | 长度/范围类拒绝 | 缩小单次读长度分块；读 `0x0` 的 0x40 字节请求就给这个 |
| `0x1D02` | INVALID_ADDRESS | CQDMA 路线（`0x10212C00`、APDMA `0x110001A0`）在本机被固件拒绝，**放弃 CQDMA** |
| `0x1D08` | 读鉴权拒绝（memread auth） | 不要用 READ32 直读，走 `brom_register_access` 原语 |
| `0x1D0A` | dumppreloader 失败 | 别依赖该路径取 preloader；preloader 从 eMMC `boot1` 拿 |
| `0x1D0D` | **SLA_ERROR** | 立即停。这一轮会话已废，签名 DA 不能重试 |
| `status 0000` + 读回全 0 | 地址错位 / 影子写 | **不是成功**。换 offset 重扫，回"证真实读写"那一步 |
| `Pipe error (Errno 32)` | 帧失步（0xDA 回显少发 1 字节）或设备 STALL | 停写；`0xDA` 回显是 **4 字节大端**，逐帧核对长度；之后通常要物理重进 |
| `Errno 13` / EACCES | 控制传输权限不足 | BROM 阶段需要 root；DA2 阶段用户态可以 |
| `Errno 60` Operation timed out | 设备已不在预期状态 | 检查是否已经重新枚举（`0e8d:2001` 而不是 `0003`） |
| `Entity not found` | USB 句柄失效 | 重新 `usb.core.find`，**不要复用旧句柄** |
| `out of resources` / `darwin_device_from_service` | macOS 配件授权被撤回 | 物理拔插，或 系统设置 → 隐私与安全性 → 配件 → 始终允许。不要循环重试 |
| `HS_RAW_FAIL` / `HS_RAW_OUT_ERR` | 握手无回应 | payload 未被执行或槽位错，回到"劫持"那一步重验 |
| `BIND_EP_MISSING` | 新句柄缺端点 | 重新 find/adopt |

## 2. 主机侧崩溃（这个坑很贵，别再踩）

**症状**：DA stage2 已经起来、`DA SLA is disabled` 也打印了，随后 Python 进程
**SIGSEGV**，死在 `DAXFlash.reinit()` 里的
`Reconnecting to stage2 with higher speed`（同一进程内 `port.close` →
`device.reset` → 重开）。

**根因**：macOS libusb 在同一进程里重置并重开 CDC 端点会踩到悬空句柄。

**处置**：把重连关掉再进 stage2——

```python
args = v.Args(..., noreconnect=True)   # v50 起就在 ArgHandler 里
config.reconnect = False
```

副作用是 stage2 停在低速，但备份 8 MB `seccfg` 只是慢，不会死。
另一个必须同时改的点：**备份门控失败时不要 reset 设备**，
保留 DA2 存活让用户态脚本接管（`da2_attach.py` 的 `DA_KEPT_ALIVE_FOR_ATTACH`）。

## 3. DA2 接管

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `AttributeError: 'NoneType' object has no attribute 'gpt_num_part_entries'` | `da.connect()` 后没有初始化 GPT 设置 | `mtk.daloader.daconfig.setup()`（`da2_attach.init_da` 里就是这么修的） |
| 10 行 `DA2_BACKUP_FAIL` | 同上 | 同上 |
| 备份门失败后设备消失 | 旧脚本在门控失败路径也 `shutdown()` | 已改为不 reset；重跑 `da2_unlock_seccfg.py` 会重新给出 DA2 |

## 4. seccfg 解锁

| 现象 | 含义 |
| --- | --- |
| `SECCFG_RESULT (False, 'Device has is either already unlocked or algo is unknown. Aborting.')` | mtkclient 的**通用** unlock 不认识本机算法。**没有写入**，无害，也不是解锁 |
| `V78_HASH sha=... stored=...` 两者不等 | SEJ 复现失败：`SW_OTP` 取错了。用 `boot1`（不是 `boot2`）偏移 `0xad0` 的 32 字节 |
| `V74_ALREADY_UNLOCKED` / `lock=3` | 已经解锁，**别再写** |
| `magic != 0x4D4D4D4D` 或 `endflag != 0x45454545` | 读到的不是 seccfg 头部，检查 `pagesize` 与 `part.sector` |

判据只有一个：`V78_SECCFG_VERIFY match=True` + 重启后 LK 显示 `Orange State`
+ `getprop ro.boot.verifiedbootstate` = `orange`。

## 5. Magisk / root

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `Magisk environment incomplete, abort` | 开机后 Magisk 弹窗"修复运行环境"被点了**继续**，或 `/data/adb/magisk` 缺 `busybox`/`magiskboot` | **要点"取消"**；模块不挂载就是这里 |
| `su -c id` 返回 `uid=0 ... u:r:init:s0` | root 不完整（策略没打对） | 重装 Magisk 补丁包，检查 `V79_PATCH_SHAPE` 里 ramdisk 是否变大 |
| ramdisk 大小没变 | Magisk 修补没生效 | 别刷，重修补 |

## 6. WebView

| 现象 | 原因 |
| --- | --- |
| 装上后仍是旧内核 | AXML 包名、`resources.arsc` 包名、`/system/app/<目录>` 三者**必须完全一致**，差一个 Android 6 就不认 |
| `PackageManager` 拒收 / 解析失败 | 改了 `resources.arsc` 却没同步 CRC32（本地头 `+14` 与中央目录 `+16` 都要改） |
| 网页白屏、`UnsatisfiedLinkError` | `.so` 没解到磁盘。魅族定制的 `nativeLibraryDirectories` 含 `/system/app/webview/lib/arm64`，APK 内的 `lib/` 条目不会被加载 |
| 签名报错 | 缺 `-digestalg SHA-256 -sigalg SHA256withRSA`（新 JDK 禁 SHA1）；或用了纯 v2 签名（Android 6 不认） |
| 107+ 装不上 | Bromite 107 起 `minSdkVersion=24`，Android 6 到顶就是 **106** |
| 自带浏览器没变化 | 魅蓝 X 自带浏览器用**自带**的 Chromium 45，不走系统 WebView。用 Via/Chrome 验证 |

## 7. 已证伪路线（除非有新证据，别重试）

| 路线 | 实测结果 | 结论 |
| --- | --- | --- |
| CQDMA `0x10212C00` / APDMA `0x110001A0` | 写入全部 `INVALID_ADDRESS 0x1d02`，`CQ_DEAD rc=6` | 本机不可用 |
| 清黑名单 `0x102774` / `0x105704` | 那只是 chipconfig 里的值，CQDMA 在清之前就死了 | 从未执行，不是步骤 |
| 改写发送指针 `0x1027B4` / `0x1027B8` | 四种写法都 `ACK False` | 不是触发点；槽位是 `sendptr+8` |
| 裸单字节 `0xDA` / `0xD5` | `RAWW_ECHO_MISMATCH` → `Pipe error` → 设备死 | 只走 `brom_register_access` |
| `0xD5` jump_da / 签名 DA | `0x1D0D SLA_ERROR`，设备失联 | 不走 |
| offset=8 是"错位"的猜测 | 早期把 off=8 判成影子写；**实际本机就是 off=8 + trigger=0x02FF 命中**，canary `0xdeadbeef` 读回一致 | 上游文档的 offset=9 不适用于本机，别照抄 |
| "回读一致就等于写对" | off=8 曾 150/150 一致却无 ACK | 必须有双锚点 + WDT 复位探针 |
| 换 Windows / 换主机 | BROM 固件层的 STALL 与主机无关 | 不解决 |
| "必须 root" | BROM 阶段要 root；**DA2（`0e8d:2001`）用户态实测可用** | 先探测，不预设 |
| 升级系统解决 10-bit 解码 | MT6757D 缺的是硬件 profile 支持 | 无效，官方上限 Flyme 7 / Android 6.0.1 |

---

## 8. 汇报与复跑约定

每轮跑完请把这四样贴出来，便于对照：

```
阶段: 6/7（备份+解锁）
证据: /tmp/agent2.log:38351  V78_SECCFG_WRITE_OK
结论: 已打通 / 未打通（缺一环就如实说未打通）
下一步: <一条物理动作或一条命令>
```

- 一个 BROM 会话只给一个进程；进程退出后会话作废，必须**重新物理进 BROM**。
- 设备状态一变（`0003` → `2001` → 消失）就自动接下一步，不需要人工催。
- 失败先定位到具体原因（缺变量、漏 `main()`、日志被缓冲、帧少发 1 字节）再重跑；
  不要让人把同一段操作再做一遍。
