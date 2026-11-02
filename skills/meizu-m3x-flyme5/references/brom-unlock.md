# BROM → DA2 → seccfg 解锁细节

## 会话规则

- 一次 BROM 会话只能有一个进程。子进程退出后设备不会自动回到可用状态，必须物理重进。
- root watcher 启动时若 BROM 已存在，视为上一轮死会话：**先等它消失再武装**，不要对死设备空转。
- 用户重进 BROM 后自动继续，不等用户说「继续」；检测到、阶段完成、失败都主动汇报日志路径 + 关键行。
- 每个阶段打带标记的日志（`K2_INIT`、`SENDPTR_READ`、`PAYWRITE_DONE`、`HIJACK_ACK`、`V78_GATE`、`FATAL`），异常写 `traceback` 到文件 + `agent_fatal.txt`；启动即 `faulthandler.enable(file=...)`。

## 进 BROM 与枚举

1. 长按电源 15–20 s 强制关机。
2. 按住音量+ 同时插 USB。
3. 保持 3–5 s 后松开，确认 `0e8d:0003`。

`0e8d:20ff` 是 LK/fastboot 下载模式，做不了 linecode 溢出。macOS 上配件授权被撤回会表现为 `out of resources` 或枚举为 0，物理拔插恢复；`Errno 13` 是权限不足，需要 root 或修配件授权。

## kamakiri2 布局【实机】

- 读 linecode：`ctrl_transfer(0xA1, 0x21, 0, 0, 7)`。本机 `0xA1/0x25`（8 B）STALL；`0x21` 返回 `00100e00000008`，失败用该常量兜底。
- 溢出：`linecode[:7]` 补 `0x00` 到 `offset`，接 `pack('<I', addr)`；`offset=9`，总长 13 B。
- 触发：`ctrl_transfer(0x80, 0x06, 0x0200 或 0x02FF, 0, 9)`；本机 `0x0200` 可用，`0x02FF` 依组合 STALL。**STALL 不等于参数错。**
- 扫描：`(9, 8, 7, 10) × ('02ff','0200','none')`，每组先读后写。只改 offset 而沿用旧 warm-up 等于没测。
- 访问序列：先 warm-up `ptr+5,+6,+7`（`ptr = brom_register_access[0][1] = 0xa0e8`）；目标 `<0x40` 用 `-2,-1,0,1`，否则 `-2,-1,0` 后访问 `address-0x40`。
- 本机第 30 次左右在 `off=8 / trig=02ff` 命中过，但 `offset=8` 事后判定为错位影子（状态 0、回读 150/150 一致仍不可信）。最终可用组合以现场读 `0x0`/`0x9C2C` 的非零合理值为准。

## 0xDA 语义

- 回显是 4 字节**大端**（`pack(">I", v)`），帧：`echo(0xDA)` + `echo(addr)` + `echo(len)` + `echo(mode)`；`mode=0` 读、`1` 写，`check_status=True` 再读 2 B 小端状态。
- 裸单字节 0xDA/0xD5 会失步 → `Pipe error`，USB 栈报废；只用 `brom_register_access`。
- 读 ROM/SRAM 可能返回零页（cache issue）；用 canary + 看门狗反证：`watchdog = 0x10007000`，`watchdog+0x50 ← 0x1971` 应触发复位。

## payload 与劫持

```text
sendptr = read(0x9C2C)              # 读 0 或不在 0x00100000–0x00180000 时用 0x1027B8
slot    = sendptr + 8
write_payload(0x100A00)             # 600 B，逐字写 + 逐字回读
brom_register_access(slot, 4, pack('<I', 0x100A00), check_status=False)
clear_rx(); ack = read(4)           # 期望 b'\xa1\xa2\xa3\xa4'
```

- `check_status=False` 是必需语义：多读最后 2 B 会把 payload 的 ACK 吃掉。
- 改 `0x1027B4/0x1027B8` 本身不是触发点（四种写全 `ACK False`）；正确位置是 `sendptr+8`。
- 触发后设备静默/复位都常见；一旦失联立即停，不循环刷错误。
- 不走 `0xD5` jump_da / 签名 DA：本机 `0x1D0D` SLA_ERROR 并把设备打失联。

## 握手与 DA

- 握手逐字节发 `A0 0A 50 05`，期望取反回显 `5F F5 AF FA`；失败再试 `port.run_handshake()`。
- DA1 只支持 BOOT_TO/SHUTDOWN/INIT_EXT_RAM/DEVICE_CTRL/GET_RAM/GET_DA_VERSION 等，**没有** GET_EMMC_INFO / READ_DATA / WRITE_DATA。
- EMI/preloader 必须匹配本机 eMMC：`preloader_s21v57c2k_dg_m1t.bin` 的 rec0 与本机 CID 逐字节一致。EMI 不匹配会卡在 DRAM 初始化（mz6757 的 preloader 在本机失败就是这个原因）。
- 成功标志：`DRAM setup passed.` → `Successfully uploaded stage 2` → `DA SLA is disabled` → 设备变为 `0e8d:2001`。
- DA2 重连高速（DAXFlash.reinit）会 SIGSEGV（rc=-11）。用 `Args(noreconnect=True)`；门控失败保留 DA2 会话并退出码 5，**不要 shutdown**。
- 用户态 uid 501 可直接 attach DA2（`0e8d:2001` 不需要 root）；attach 前必须用 ArgHandler 初始化 config，否则 `gpt_settings=None`。

## 备份门与 seccfg

顺序：`printgpt` → 备份 `gpt/preloader/proinfo/nvram/protect1/protect2/lk/lk2/boot/recovery/seccfg` → 写 seccfg → 复查（`seccfg_after`）→ reset。

- 本机 GPT 共 32 项，**没有 `preloader` 分区**（首项 recovery 从 LBA 64 开始）；「读 preloader 分区提 OTP」不成立。
- seccfg 位于 LBA `1376256`（8 MiB，pagesize 512），其余区域原本全 0。
- 解锁后头部：`4d4d4d4d 04000000 3c000000 03000000 01000000 00000000 45454545` + 8 B hash（`lock_state=3`）。
- 哈希复现：从 eMMC boot1 中 `MMM\x01` 命中偏移 `0xad0` 的头后 32 B 是 SW_OTP（本机即 `"MEIZU"` 补零）；LK 驱动 A 的顺序是 `enc(sha256(原头))`。OTP 每台不同，**运行时从设备读，不写进仓库**。
- SEJ 寄存器顺序（LK Thumb 反汇编）：`CLK=0x20; AKEY0-7=0; ACON=2; ACONK=0x10 → |=0x100; ACON2=2; ACFG=CFG1; 3×(ASRC=RND, ACON2=1, poll bit15); ACON2=2; ACFG=CFG1; ACON=3/2; ACONK=3`；终止只写 `ACON2=2 + AKEY=0`（不清 OTP）。
- `ACON2=2`（CLR）会清 ACFG/ASRC/AOUT，**必须在写 IV 之前**。顺序写反 → IV 实际为 0 → 全部 NOMATCH。

## 错误码

| 现象 | 含义 | 处置 |
| --- | --- | --- |
| `0x1A1D` | 寄存器访问被拒 / cache issue | 重查 offset、trigger、warm-up |
| `0x1B1D` | 长度/范围拒绝 | 缩小读长度，分块探测 |
| `0x1D02` | INVALID_ADDRESS | CQDMA 不可用，回 kamakiri2 |
| `0x1D08` | 读鉴权拒绝 | 寄存器原语，不 READ32 直读 |
| `0x1D0A` | dumppreloader 失败 | 不依赖该路径 |
| `0x1D0D` | SLA_ERROR | 立即停，会话可能已死 |
| `Errno 13` | EACCES / 权限或配件授权 | root 或修授权 |
| `Errno 32` / Pipe error | 帧失步或 STALL | 查 4 B 回显/帧长，物理重进 |
| `Entity not found` | 句柄失效 | 重新 find，不复用 |
| `HS_RAW_FAIL` | 握手无回应 | 回阶段 3 确认 ACK/端点 |
| `BIND_EP_MISSING` | 新句柄缺端点 | 重新 find/adopt |

## 已证伪（不要重试，除非有新证据）

| 路线 | 结果 |
| --- | --- |
| CQDMA `0x10212C00` / APDMA `0x110001A0` | 全部 `INVALID_ADDRESS 0x1d02` |
| `0xD5` jump_da / 签名 DA | `0x1D0D`，设备失联 |
| 裸单字节 `0xDA`/`0xD5` | `RAWW_ECHO_MISMATCH` → Pipe error，设备死 |
| `offset=8` | 状态 0、回读一致，但属错位影子 |
| 上游 sendptr 槽假设（`0x1027C0` 等直接写） | 写后无 ACK；正确是 `sendptr+8` |
| 换 Windows | BROM 固件层 STALL，换主机不变 |
| 「必须 root」 | 早期需要；后期用户态可驱动 DA2，先探测再决定 |

## 复现 LK 哈希时的两个坑

1. 先把 `lk` 与 `lk2` 都 dump 出来：本机两份相同（md5 一致），且 LK 本身没有 ROM info 结构，OTP 不在 lk 里。
2. 失败时先核对常量与 CLR/IV 顺序，再怀疑密钥。v74（常量错）与 v75（顺序错）都是这么翻车的。
