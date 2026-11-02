---
name: meizu-m3x-flyme5
description: End-to-end playbook for unlocking and modernizing a Meizu M3X (魅蓝 X, MT6757D, hwcode 0x551, Flyme 5.2.8.3A / Android 6.0.1) on macOS. Covers MTK BROM kamakiri2 linecode overflow → DA2 → seccfg unlock, Magisk root, WebView provider replacement (Bromite 98 is the ceiling on API 23), API 23 app rebuilds, Flyme status-bar meizuFlags patching, VoLTE/IMS enablement and video-decode limits. Use when the task mentions 魅蓝X / M3X / MT6757 / Helio P20 / hwcode 0x551, USB 0e8d:0003 BROM, 0e8d:2001 DA, kamakiri2, seccfg unlock, Orange State, Bromite WebView on Android 6, meizuFlags dark status bar icons, or repacking/rebuilding APKs for API 23.
---

# 魅蓝 X（M3X / MT6757D）Flyme 5 改造 Playbook

本仓库记录的是**这台机器实测跑通的路径**，不是通用教程：BROM 地址、黑名单、preloader/EMI 表都随机型甚至同机型批次不同。

## 硬约束（先读）

- 只对**本人拥有或已获书面授权**的设备执行。解锁会清数据，写错 preloader/lk/seccfg 可能永久变砖。
- **破坏性门**：`gpt` 与 `seccfg_before` 备份非空之前，禁止写 `seccfg`。仓库脚本内置 `BACKUP_GATE`，不要绕过。
- 只向 SRAM `0x00100000–0x00180000` 写 payload / 劫持槽；sendptr、槽位、payload 地址任一越界即中止。
- 管理员密码只由用户在 macOS GUI 弹窗输入，绝不捕获或落盘；越界的系统改动（kext、sudoers）先问。
- 每个写操作要有独立判据；`status 0000`、回读一致、设备仍枚举都**不算**成功。
- 标注分级：**【实机】** 本机实测；**【源码】** 上游/反汇编核对；**【未验证】** 从未成功执行。不要把【源码】写成【实机】。

## 阶段与门

| 阶段 | 动作 | 通过门 | 失败时 |
| --- | --- | --- | --- |
| 0 环境/会话 | 进 BROM、确认 `0e8d:0003`、一个会话一个进程 | `da.connect()` 成功，chipconfig 可打印 | `0e8d:20ff` 是 LK，重进；死会话先等消失 |
| 1 取得访问 | linecode 溢出 `offset=9` + trigger `0x0200` | 读 `0x0`/`0x9C2C` 得到合理非零值 | 换 offset/trigger；STALL ≠ 参数错 |
| 2 证明真实读写 | 向量表 + SRAM canary + WDT 复位探针 | 至少两项成立且 WDT 能复位 | 停在只读，不进 payload |
| 3 payload/劫持 | 600 B @ `0x100A00`，写 `sendptr+8` | 逐字校验通过 **且** 收到 `A1A2A3A4` | 无 ACK 立即停，回阶段 2 |
| 4 握手/DA | `A0 0A 50 05` → DA1 → DA2 | 回显 `5F F5 AF FA`、`0e8d:2001`、`DRAM setup passed` | 不重试 SLA 签名 DA（`0x1D0D` 毁会话） |
| 5 备份/seccfg | 备份 → 复现 LK 哈希 → 写 seccfg → 复查 | 备份非空 + 回读 match + 重启 `verifiedbootstate=orange` | 缺备份就停 |
| 6 系统改造 | root、WebView、状态栏、VoLTE | 见各主题门 | 回滚到上一稳定状态再诊断 |

## 本机关键参数

| 项 | 值 |
| --- | --- |
| BROM / DA / LK 下载模式 | `0e8d:0003` / `0e8d:2001` / `0e8d:20ff` |
| hwcode / target config | `0x551` / `0xe7`（SBC+SLA+DAA 全开） |
| 寄存器访问指针 | `chipconfig.brom_register_access[0][1]` = `0xa0e8` |
| sendptr / 劫持槽 | `0x1027B8`（或读 `0x9C2C`） / `sendptr+8` |
| payload | 600 B @ `0x100A00` |
| ACK / 握手 | 线上 `A1 A2 A3 A4` / `A0 0A 50 05`，期望取反回显 |
| seccfg | LBA `1376256`，8 MiB，pagesize 512；`lock_state=3` 即解锁 |
| 成功后 | LK 显示 Orange State，`ro.boot.verifiedbootstate=orange` |

## 流程要点

1. **环境（macOS）**：root watcher 必须用带 pyusb 的 venv（`/tmp/rootvenv/bin/python`）；Homebrew 的 `python3` 没有 pyusb，`import usb.core` 直接崩且无日志。常驻进程用 double-fork + setsid，普通 `nohup &` 在无 tty 的 root 会话里会被带走。
2. **进 BROM【实机】**：长按电源 15–20 s 关机 → 按住音量+ 插 USB → 保持 3–5 s 松开 → `0e8d:0003`。一个会话只能给一个进程；进程退出后会话作废，必须物理重进。
3. **linecode 溢出**：布局是 `linecode[:7]` 补 `0x00` 到 `offset` 再拼 `<I` 地址；上游与本机都用 **offset=9**，SET_LINE_CODING 共 13 B。触发用 `ctrl_transfer(0x80, 0x06, 0x0200/0x02FF, 0, 9)`。扫描顺序 `(9,8,7,10) × (02ff, 0200, none)`，每组先读后写。
4. **0xDA 语义**：回显是 4 字节**大端**；帧为 `echo(0xDA)+addr+len+mode`，少发 1 字节就失步 → `Pipe error`，USB 栈报废。只走 `brom_register_access`，不手拼裸命令。访问序列：warm-up `ptr+5,+6,+7`，地址 `<0x40` 用 `-2,-1,0,1`，否则 `-2,-1,0` 后访问 `address-0x40`。
5. **payload/劫持**：逐字 4 B 写 + 逐字回读（bulk 写状态 OK 但回读全 0，不可信）。劫持必须 `check_status=False`，否则最后 2 B 状态会把 payload 的 ACK 吃掉，表现为「写成功但永远没 ACK」。收 ACK 前清 RX；可用 `0xFD`/`0xFC` 主动触发。
6. **DA**：EMI 表必须含本机 eMMC（Samsung `DX18MB`）；本机 DA SLA 被裁掉，`DA SLA is disabled` 是正常现象。DA2 重连高速时会 SIGSEGV，用 `noreconnect=True`；门控失败时**保留 DA2 不要 shutdown**，否则丢掉一个可用会话。
7. **seccfg**：新头 `4d4d4d4d 04000000 3c000000 03000000 ...` + 8 字节 hash；哈希复现依赖从设备 boot1 `0xad0` 读出的 32 B SW_OTP 密钥（每台不同，**不要**写进仓库）。SEJ 顺序关键：`ACON2=2`（CLR）必须在写 IV/ACFG **之前**，否则 IV 实际为 0。
8. **WebView**：provider 位置是三处包名一致（AXML manifest、`resources.arsc`、签名）。本机 **Bromite 98 是上限**，99 起 32 位进程必崩。判据不只看进程存活，必须 `maps` 到 `lib/arm/libwebviewchromium.so`。
9. **状态栏**：Flyme 图标颜色看 `WindowManager.LayoutParams.meizuFlags`（`MEIZU_FLAG_DARK_STATUS_BAR_ICON = 0x200`），框架自动映射成 `SYSTEM_UI_FLAG_LIGHT_STATUS_BAR`。Magisk 模块改不了别人的窗口，只能改 APK 或上 Xposed（本机有 bootloop 风险）。
10. **VoLTE / 解码**：IMS 开关全开后需真实通话/短信验证；10-bit HEVC 只能软解（VLC）。升级到更高 Android 没有现成路径（非 Treble、无 vendor 分区、官方最高仍是 6.0.1）。

## 判定速查

| 现象 | 判定 | 下一步 |
| --- | --- | --- |
| `0x1A1D` | 寄存器访问被拒 / cache issue | 重查 offset、trigger、warm-up |
| `0x1D02` | INVALID_ADDRESS（CQDMA 路线） | 放弃 CQDMA，回 kamakiri2 |
| `0x1D08` | 读鉴权拒绝 | 用寄存器原语，不 READ32 直读 |
| `0x1D0D` | SLA_ERROR | 立即停，会话可能已死 |
| `Errno 32` / Pipe error | 帧失步或 STALL | 停写，查 4 B 回显/帧长，物理重进 |
| `out of resources` | macOS 配件授权被撤回 | 物理拔插，不循环重试 |
| status 0000 + 读全 0 | 地址错位/影子写 | 换 offset，回阶段 2 |
| WebView 装上但 32 位应用崩 | 该版本不兼容 API 23 | 降到 98 或更低，勿改配置硬凑 |

## 资源

- `references/brom-unlock.md` — kamakiri2 逐帧、DA2 attach、seccfg/LK 哈希复现、错误码与已证伪路线。
- `references/webview-and-apps.md` — WebView 三处包名对齐、版本二分、状态栏 `meizuFlags` 补丁与 API 23 重编。
- `references/device-profile.md` — 硬件/分区/SEJ 寄存器/解码矩阵/升级上限。
- `scripts/patch_statusbar.py` — 给反编译后的 Activity 注入 `sbarFix` 深色图标补丁（`--self-test` 可离线验证）。
- 仓库根目录 `docs/01–07` 是逐条实跑日志与证据；`scripts/brom|webview|volte|video` 是可跑脚本。本 skill 与仓库同源，**先把仓库克隆到本地**再按文中路径调用。
