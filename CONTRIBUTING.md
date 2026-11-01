# 贡献与反馈

## 提 Issue 前必读：请先脱敏

这些值能定位到你本人或你的设备，**一律不要贴进 Issue/日志/截图**：

| 泄露点 | 来自 | 处理 |
| --- | --- | --- |
| IMEI / MEID | `/tmp/backup/.state`、`getprop ro.ril.*`、飞梅弹窗 | 删 |
| IMSI / ICCID | `getprop \| grep ims`（`gsm.sim.operator.imsi`、`persist.radio.simswitch.iccid`） | 删 |
| eMMC CID | `DA2_REINIT_OK cid=...`、`emmc cid` | 删 |
| 你的 IPv6 | `ip -6 addr`、`dumpsys connectivity` 的 `LinkAddresses`/`DnsAddresses` | 写成 `240e:xxxx::…` |
| adb 序列号 | `adb devices` | 删 |
| 状态栏截图 | 通知内容、时间、号码 | 裁掉或打码 |

`git status` / `.gitignore` 已经把 `*.log`、`*.bin`、`*.img`、`*.png`、`*.keystore`
全部排除；请不要绕过它强行提交。

## 判据要求（这是本仓库的硬规则）

不接受"看起来好了"。每个结论必须给出**至少一条可复核的证据**：

- 日志行（文件名 + 时间戳 + 标记，如 `V78_SECCFG_VERIFY match=True`）；
- 或属性/设置的可读回显（`getprop`、`settings get`、`dumpsys` 片段）；
- 或设备侧事实（`ro.boot.verifiedbootstate = orange`、`dumpsys package` 版本号）。

同时请按仓库既有约定标注证据强度：

- **【实机】** 本机执行成功并有证据；
- **【源码】** 从代码/配置文件核对，本机未执行；
- **【未验证】** 从未成功执行。

把【源码】写成【实机】的 PR 会被直接拒绝——这条规则的存在是因为本项目真的
因为"`status 0000` + 回读一致"就误判过一次成功，浪费了一整天。

## 改动脚本时

- 每个阶段都要打印带前缀的标记（`V74_`、`V78_`、`V79_`、`DA2_`、`PART_`），
  便于人肉/脚本轮询；异常要把 `traceback.format_exc()` 落盘再 `os._exit`，
  不要 `print_exc()` 后直接退出（stderr 会被缓冲，证据丢失）。
- 任何写分区的分支都必须先过备份门（`gpt` + 目标分区备份非空），
  且**失败路径不要 reset 设备**——保留 DA 存活才能抢救。
- 只允许向 `0x00100000–0x00180000` 写 payload；越界直接中止。
- `da2_flash_boot.py` 里的 `STOCK_MD5/PATCHED_MD5/BOOT_LEN` 是本机常量，
  换机型必须改。

## 结构建议

新增内容请放进既有目录，不要新建顶层目录：

```
docs/NN-*.md          一个主题一篇，编号递增
scripts/<area>/       可执行脚本，保持能在 /tmp 直接跑（脚本之间互相 import）
magisk-module/<id>/   模块骨架：只放 module.prop 与说明，不放二进制
```
