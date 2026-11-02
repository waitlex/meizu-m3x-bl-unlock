# 设备档案（魅蓝 X / M3X）

## 硬件

| 项 | 值 |
| --- | --- |
| SoC | MediaTek MT6757D（Helio P20），hwcode `0x551` |
| DRAM | 3 GB / 2 rank |
| eMMC | Samsung `DX18MB`，29.1 GiB，HS400；Boot1/Boot2/RPMB 各 4 MiB |
| ABI | arm64-v8a，系统应用大量仍是 armeabi-v7a |
| 内核 | 3.18.31（2017-04-06） |
| 系统 | Flyme 5.2.8.3A / Android 6.0.1（API 23），`ro.build.display.id = Flyme 5.2.8.3A` |
| Treble | 不支持（`ro.treble.enabled` 为空，无 `vendor` / `system_b`） |

## 分区

- GPT 共 **32 项**，**没有 `preloader` 分区**（首项 recovery 从 LBA 64 开始）。
- `seccfg`：LBA `1376256`，8 MiB，pagesize 512；解锁前的其余区域全 0。
- 想从「preloader 分区」提 OTP 不成立；本机 OTP 在 eMMC **boot1** 的 preloader 镜像里（`MMM\x01` 命中偏移 `0xad0` 后 32 B）。
- 备份原始 boot1/boot2 各 4 MiB（内容相同）可用于核对。

## DA / 安全相关

- DA1 只有 BOOT_TO / SHUTDOWN / INIT_EXT_RAM / DEVICE_CTRL / GET_RAM / GET_DA_VERSION 等，**没有** GET_EMMC_INFO / READ_DATA / WRITE_DATA。
- 本机 DA SLA 被裁掉：日志 `DA SLA is disabled` 属正常。
- EMI 表必须含本机 eMMC：`preloader_s21v57c2k_dg_m1t.bin` 的 rec0 与本机 CID 逐字节一致；用 mz6757 的 preloader 会卡在 DRAM 初始化。
- mtkclient 对 hwcode `0x551` 默认 legacy=True（SECINIT|=2 → ACON2|=0x40000000 → poll bit31 → ACONK=0x10）。
  V2/V3/V4 变体与 SW AES 候选键（含零钥匙矩阵）均已离线证伪，不要重复投入。

## SEJ 寄存器实测【实机】

| 写值 | 回读 | 结论 |
| --- | --- | --- |
| `ACONK=0x3` | `0` | bit0/bit1 不自锁 |
| `ACONK=0x110` | `0x100` | bit4 不自锁，bit8 保留 |

- 因此 LK 的 `ACONK=0x10 → |=0x100` 在本机等效于只置 R2K。
- 基线上电态：`ACON=2`，`ACON2=0x8000`，`ACONK=0`。
- 顺序：`ACON2=2`（CLR）必须在写 IV/ACFG 之前；写反 → IV 为 0 → 全部 NOMATCH。

## 能力上限

- WebView：系统自带魅族魔改 Chromium 44/45；第三方 provider 上限 **Bromite 98.0.4758.116**。
- 视频：10-bit HEVC 只能软解（VLC 软解可 30 fps 实时）；其余格式见 `scripts/video/bench_decode.sh` 矩阵。
- 官方最高固件：Flyme 7.8.7.24 beta（2018-07-26）与 Flyme 6.3.0.3A（2019-03-15），**底层仍 Android 6.0.1**（`MMB29U/1532424378`）。
- 社区：没有 M3X 的 LineageOS / MoKee / GSI 移植；MT6757 跨机型移植内核/显示/相机驱动不通用。

## 升级结论

短时间升到更高 Android **没有现成路径**。可选项只有：Magisk root、刷官方 Flyme 7（壳新底仍 6.0.1）、
长期移植（低成功率）、换机。不要为 Android 15 编译投入时间。
