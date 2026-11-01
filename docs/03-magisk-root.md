# 03 · Magisk root（Android 6.0.1 / Flyme 5.2.8.3A）

前提：已完成 [02 · seccfg 解锁](02-brom-da-unlock.md)，设备能进 DA（`0e8d:2001`）。

## 为什么走 DA 而不是 fastboot

Flyme 的 LK 虽然解锁了，但 `fastboot flash boot` 在这台机器上不稳定（部分批次
`fastboot` 直接不识别 `boot` 分区）。既然 DA 已经打通，直接经 DA 写 `boot` 最省事，
而且**同一条通道我们已经用来备份过 boot 了**。

## 步骤

### 1. 取出 stock boot

解锁阶段已经 dump 过：`/tmp/backup/boot.img`（16 MB）。
如果没备份，先跑 `da2_unlock_seccfg.py` 让设备停在 DA2，再用用户态接管现读：

```bash
cp scripts/brom/*.py /tmp/          # 脚本之间互相 import，必须同放 /tmp
python3 /tmp/da2_partition.py read boot /tmp/backup/boot.img
# 期望：V74_PART / PART_READ_OK boot.img 16777216 <md5>
```

### 2. 用 Magisk 27.0 修补

```bash
adb push /tmp/backup/boot.img /sdcard/Download/
# 在手机上：Magisk App → 安装 → 修补一个文件 → 选 Download/boot.img
adb pull /sdcard/Download/magisk_patched-*.img /tmp/magisk/magisk_patched.img
```

修补是否正常的判据（本项目实测）：

| 项 | stock | patched |
| --- | --- | --- |
| kernel 大小 | 不变 | 不变 |
| ramdisk | 2.11 MB | **2.52 MB（+409 KB）** |

ramdisk 没变大 = 没打上，别刷。

### 3. 经 DA 写回

```bash
# da2_flash_boot.py 里写死了本机两个 md5，换机/换 ROM 必须先改：
#   STOCK_MD5 / PATCHED_MD5 / BOOT_LEN，STOCK/PATCHED 两个路径
sudo env DYLD_FALLBACK_LIBRARY_PATH=/tmp/libusb /tmp/rootvenv/bin/python /tmp/da2_flash_boot.py
```

本机成功日志（`/tmp/agent2.log`，16:36）：

```
V79_PATCH_SHAPE kernel 7751831->7751831 ramdisk 2112869->2522288
V79_BOOT_PART sector=1597952 sectors=32768 addr=0x30c40000
V79_READ liveboot 0x30c40000 len=16777216 md5=35b69ea...      ← 与 stock 一致才继续
V79_LIVE_SAVED /tmp/backup/boot_live_before_flash.img
V79_WRITE_FLASH 0x30c40000 len=16777216 ret=True
V79_VERIFY readback_md5=08f05c4... match=True
AGENT_V79_DONE reason=flashed
```

写回前脚本会自动把当前 boot 另存为 `/tmp/backup/boot_live_before_flash.img`（回退用）。
设备必须重新进 BROM；若 live boot 的 md5 既不等于 stock 也不等于 patched，
脚本打印 `V79_ABORT_UNKNOWN_BOOT` 并保留 DA，不会写。

### 4. 开机后必须手动补一次运行环境

Magisk App 会弹 **"需要修复运行环境（Fix environment）"**。

> **直接点"取消"，不要点修复**——本机的修复流程会失败，而且失败后模块挂载会静默不生效。
> 根因：`/data/adb/magisk/` 下缺 `busybox`、`magiskboot`、`magiskpolicy` 等文件，
> `magiskd` 在 post-fs-data 阶段判定
> `Magisk environment incomplete, abort`，于是**跳过模块列表构建**，
> 表现就是"root 有、模块全不生效"。

修复办法（等价于 App 的 `fix_env`，但可控）：

```bash
adb shell su -c 'ls /data/adb/magisk'            # 看缺哪些
# 从 Magisk APK 里解出 util_functions.sh / boot_patch.sh / 各二进制补进去
adb shell su -c 'cp -a /data/adb/magisk /data/adb/magisk.bak'
adb reboot
```

补完后 `magisk --sqlite "SELECT * FROM modules"` 应能列出模块。

### 5. 验证

```bash
adb shell su -c id
# uid=0(root) gid=0(root) ... context=u:r:magisk:s0     ← 这才算 root 成功
adb shell su -c getenforce                                # Enforcing（Permissive 说明策略没打对）
```

`uid=0` 但 context 是 `u:r:init:s0` 属于**不完整**：部分系统服务读不到 root 状态。

## 回退

`da2_flash_boot.py` 只写"stock→patched"这一个方向，没有回退开关。
去 root 用同一个 DA 写原语反着写一次：

```bash
# 1) 让设备进 BROM 跑 da2_flash_boot.py（或 da2_unlock_seccfg.py），停在 DA2
# 2) 用户态接管，把原始 boot 写回去
python3 /tmp/da2_partition.py write boot /tmp/backup/boot.img --yes
```

`boot_live_before_flash.img` 与 `boot.img` 是同一内容（写入前后各存了一份，
可互相核对）。**回退方向本机没有实跑过**，务必先留好两个文件再动手。

## 备注：这台机器的 Android 版本上限

魅蓝 X 官方最高就是 **Flyme 7 / Android 6.0.1**，没有 Android 8+ 的官方包，
LineageOS 也没有 MT6757D 的可用构建。想上高版本只能换机，root 不改变这一点。
