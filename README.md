# 魅蓝 X（Meizu M3X / MT6757D）解锁与修复工具集

在 **macOS（Apple Silicon）** 上，用 **MTK BootROM 漏洞**给一台 Bootloader 锁死、
官方已停更的魅蓝 X（**Flyme 5.2.8.3A / Android 6.0.1**）完成：

| # | 目标 | 结果 | 关键证据 | 文档 |
| - | ---- | ---- | -------- | ---- |
| 1 | Bootloader 解锁 | ✅ 已解锁 | LK 显示 `Orange State`；`ro.boot.verifiedbootstate=orange`；`seccfg.lock_state=3` | [docs/02-brom-da-unlock.md](docs/02-brom-da-unlock.md) |
| 2 | Magisk root | ✅ Magisk 27.0 | `su` → `uid=0 shell u:r:magisk:s0` | [docs/03-magisk-root.md](docs/03-magisk-root.md) |
| 3 | WebView 升级 | ✅ 44 → **98.0.4758.116**（版本二分选出的上限） | `dumpsys package com.android.webview` = `98.0.4758.116`；图库正常、Via 搜索真实渲染；**105/101/99 全部 `SIGBUS` 于 arm `.so`，98 通过** → 断崖在 98/99 之间 | [docs/04-webview-106.md](docs/04-webview-106.md) |
| 4 | VoLTE 通话 / 短信 | ✅ 开关已全开、IMS 承载已建立；真实通话/短信已验证 | `volte_imcb/stack/ua` 均 running；IMS PDN `ccmni4` = `CONNECTED + Capabilities: IMS&VALIDATED`；状态栏 `4G+HD` | [docs/05-volte-call-sms.md](docs/05-volte-call-sms.md) |
| 5 | 本地视频卡顿 | ✅ 已定位并绕过 | 唯一失败项是 10-bit HEVC；VLC 软解 30fps 实时 | [docs/06-video-decode.md](docs/06-video-decode.md) |

> **本文档记录的是"这台机器实测跑通的路径"，不是通用教程。**
> 每台 MTK 设备的 BROM 地址、黑名单、preloader 都不一样，直接照抄参数可能变砖。

---

## ⚠️ 风险与授权

- 本仓库内容仅适用于**你本人拥有或已获书面授权**的设备。
- 操作会**清空数据**并可能**永久变砖**（BROM 层写错 `preloader`/`lk`/`seccfg` 没有软恢复手段）。
- 所有写操作前必须先完成 `gpt` 与 `seccfg` 备份，**备份非空之前禁止写 `seccfg`**——
  仓库里的脚本内置了这道门（`BACKUP_GATE`），不要绕过。
- 只向 SRAM 的 `0x00100000–0x00180000` 写 payload；越界即中止。
- 作者不对变砖、数据丢失、运营商封号负责。

---

## 设备与系统参数

| 项 | 值 |
| --- | --- |
| 机型 | Meizu 魅蓝 X / M3X（`ro.product.model = M3X`） |
| SoC | MediaTek MT6757D（Helio P20），`hwcode = 0x551` |
| 当前系统 | **Flyme 5.2.8.3A / Android 6.0.1（API 23）**，`ro.build.display.id = Flyme 5.2.8.3A`，构建于 2017-04-06 |
| 官方最高可升 | Flyme 7.8.7.24 beta（2018-07-26），**底层仍是 Android 6.0.1**，本机未刷 |
| ABI | arm64-v8a（系统应用大量仍为 armeabi-v7a） |
| eMMC | `DX18MB`，USER 区 `0x747c00000`（≈29.3 GB） |
| BROM 枚举 | `0e8d:0003` |
| DA 枚举 | `0e8d:2001` |
| LK/fastboot 下载模式 | `0e8d:20ff`（**不是** BROM，做不了 linecode 溢出） |
| target config | `sbc=1, sla=1, daa=1, memread=1, memwrite=1, cmdC8=1` |

---

## 环境准备（macOS）

```bash
# 一键建好 mtkclient + venv + libusb，并自检 pyusb 能否枚举设备
bash scripts/brom/setup_env.sh

# setup_env.sh 最后会打印它找到的 libusb 目录，导出给 pyusb：
export DYLD_FALLBACK_LIBRARY_PATH=/tmp/libusb      # 或 /opt/homebrew/lib

# 脚本之间是互相 import 的，且路径硬编码在 /tmp，所以必须这样跑：
cp scripts/brom/*.py /tmp/

# 设备进 BROM：长按电源 15s 关机 → 按住音量+ 插 USB → 保持 3-5s
system_profiler SPUSBDataType | grep -i -A2 0e8d   # 应看到 0x0E8D:0x0003
```

macOS 的两个坑（详见 [docs/01-device-and-risks.md](docs/01-device-and-risks.md)）：

1. **root 进程读不到 `~/Documents`**（TCC 限制）→ 脚本和 venv 必须放 `/tmp`。
2. **BROM 握手后设备重枚举成 CDC，被系统驱动占用** → 控制传输可能报 `Errno 13`，
   需要 root；但 DA 阶段（`0e8d:2001`）用户态即可直连，实测无需 root。

---

## 目录结构

```
docs/
  01-device-and-risks.md   机型参数、枚举识别、macOS 权限与配件授权坑
  02-brom-da-unlock.md     ★ 核心：BROM → kamakiri2 → DA → 备份 → seccfg 解锁
  03-magisk-root.md        修补 boot.img 并经 DA 写回
  04-webview-106.md        Bromite 106 伪装成 com.android.webview 的 Magisk 模块
  05-volte-call-sms.md     MTK VoLTE/IMS 开关链路与当前 SIM 的实际差距
  06-video-decode.md       硬解矩阵实测与 10-bit HEVC 结论
  07-troubleshooting.md    错误码表 + 失败签名 + 已证伪路线（别再走）
scripts/
  brom/
    setup_env.sh           一键建 /tmp/mtknew + /tmp/rootvenv + libusb，并自检 pyusb
    brom_base.py           基座：日志、Args、ArgHandler、chipconfig、brom_register_access
    brom_kamakiri2.py      linecode 溢出 → ACK → 握手 → DA1 → EMI → DA2，含健康检查与 patcher 触发
    da2_sej_tools.py       SEJ 原语 + GPT 找分区 + readflash/writeflash + 备份门控
    da2_unlock_seccfg.py   ★ 端到端解锁：ROM info 找密钥 → 复现 LK 哈希 → 写 seccfg
    da2_flash_boot.py      ★ 经 DA 写 Magisk 修补 boot（md5/shape/live 三重门控）
    da2_attach.py          DA2(0e8d:2001) 用户态接管 + 10 个分区备份，--probe 只探活
    da2_partition.py       按分区名读/写单个分区（回退与补备份用）
  webview/
    axml_patch.py          改 AndroidManifest AXML 字符串池里的包名，并剥旧签名
    patch_arsc.py          原位改 resources.arsc 包名 + 同步两处 CRC32
    sign_apk.sh            keytool 自签 + jarsigner(SHA-256) 重签名
    build_module.sh        铺 /data/adb/modules/webview106 并重启
    css_probe.html         验证内核真的是 106 的四项 CSS 特性页
  volte/
    enable_volte.sh        四个开关 + 按当前 numeric 补 IMS APN，--show/--revert
  video/
    make_samples.sh        生成 6 条解码矩阵基准片（需桌面 ffmpeg）
    bench_decode.sh        查实际选中的 codec + 帧间隔 + CPU + 截图核对
magisk-module/webview106/  模块骨架（module.prop + README，不含 APK/.so）
skills/meizu-m3x-flyme5/   ★ Agent skill：阶段/门控/坑 + 状态栏补丁脚本（见下节）
LICENSE                    GPL-3.0-or-later（scripts/ 与 magisk-module/）
CONTRIBUTING.md            反馈前的脱敏要求 + 证据判据规则
```

> 脚本按**实际用途**命名；日志标记里的 `V74_ / V78_ / V79_` 前缀是开发时的迭代号，
> 保留不动，这样文档里摘录的实跑日志仍能与脚本输出逐字对上。

---

## Agent Skill

`skills/meizu-m3x-flyme5/` 是给 AI agent 用的技能包，把本仓库的判据、门控和坑压缩成
可自动触发的流程（提到 魅蓝X / M3X / MT6757 / kamakiri2 / seccfg / meizuFlags 时命中）：

```bash
cp -r skills/meizu-m3x-flyme5 ~/.qoder-cn/skills/          # 用户级安装
# 或项目级：cp -r skills/meizu-m3x-flyme5 <project>/.qoder/skills/
/skills reload                                              # 然后 /meizu-m3x-flyme5
```

- `SKILL.md`：阶段与通过门、本机关键参数、判定速查表
- `references/brom-unlock.md`：kamakiri2 逐帧、DA2 attach、seccfg/LK 哈希、错误码、已证伪路线
- `references/webview-and-apps.md`：WebView 三处包名对齐与版本二分、状态栏 `meizuFlags` 补丁、Via/API 23 坑
- `references/device-profile.md`：硬件、分区、SEJ 寄存器、能力上限
- `scripts/patch_statusbar.py`：给反编译 Activity 注入 `sbarFix`（`--self-test` 离线可跑）

---

## 最短复现路径

```bash
# 0) 设备进 BROM（0e8d:0003），确认 mtkclient 就位
ls /tmp/mtknew/mtkclient/payloads/mt6757_payload.bin
cp scripts/brom/*.py /tmp/

# 1) 打通 BROM → DA2。首轮 /tmp/backup 里还没有备份，脚本会备份门失败但
#    保留 DA 存活（日志打印 DA_KEPT_ALIVE_FOR_ATTACH 后退出），这是预期行为。
sudo env DYLD_FALLBACK_LIBRARY_PATH=/tmp/libusb /tmp/rootvenv/bin/python /tmp/da2_unlock_seccfg.py

# 2) 设备此时是 0e8d:2001（DA2），用户态直连即可，备份 10 个关键分区
env DYLD_FALLBACK_LIBRARY_PATH=/tmp/libusb /tmp/rootvenv/bin/python /tmp/da2_attach.py
ls -l /tmp/backup/gpt.bin /tmp/backup/seccfg_before.bin   # 必须都非空
#    注意：da2_attach.py 末尾还会试 mtkclient 的通用 seccfg unlock，本机
#    返回 "algo is unknown"（不写入、无害）。真正生效的是下一步
#    da2_unlock_seccfg.py 的 LK 复现哈希，不要指望这一步。

# 3) 重新进 BROM，再跑一次 da2_unlock_seccfg.py：门控通过后才会走
#    SEJ 复现哈希并写 seccfg
sudo env DYLD_FALLBACK_LIBRARY_PATH=/tmp/libusb /tmp/rootvenv/bin/python /tmp/da2_unlock_seccfg.py
#    期望日志：V78_GATE gpt=True seccfg_before=True
#              V78_MATCH lk3+otp:boot10A dir=enc
#              V78_SECCFG_WRITE_OK / V78_SECCFG_VERIFY match=True
#              AGENT_V78_DONE reason=unlocked

# 4) 重启进系统，确认 orange
adb shell getprop ro.boot.verifiedbootstate                      # orange

# 5) root / WebView / VoLTE / 解码
#    WebView 一轮 = 下载+改包名+重签+部署+重启+四道判据；
#    ⚠ 106 这类 ≥99 的构建会让所有 32 位应用（图库、全局搜索）打不开，
#      上限是 98.0.4758.116，详见 docs/04 的「版本二分」。
bash scripts/webview/bromite_round.sh 98.0.4758.116
# 只要手工装配（已有签好名的 APK）：
bash scripts/webview/build_module.sh signed.apk bromite_arm64.apk bromite_arm.apk
bash scripts/volte/enable_volte.sh
bash scripts/video/bench_decode.sh
```

---

## 许可与致谢

- `scripts/` 与 `magisk-module/`：**GPL-3.0-or-later**（这些脚本以库方式调用
  [mtkclient](https://github.com/bkerler/mtkclient)，与其许可证保持兼容）。
- `docs/`、`README.md`：**CC-BY-4.0**（署名即可自由转发、改写）。
- 提 Issue / PR 前请读 [CONTRIBUTING.md](CONTRIBUTING.md)：**先脱敏**（IMSI/ICCID/
  IMEI/eMMC CID/IPv6/序列号），并且每个结论都要带可复核证据。
- BROM 漏洞由 **xyz** 发现、**bkerler (Chaosmaster)** 在 mtkclient 中实现（kamakiri / kamakiri2）。
- WebView 内核来自 [Bromite](https://www.bromite.org/)（GPL-3.0）。本仓库**不包含任何二进制**，
  Bromite APK、Magisk APK、MTK preloader 均需你自行下载。
