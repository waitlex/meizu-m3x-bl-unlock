# 02 · BROM → DA → seccfg 解锁（核心链路）

这是整件事最难的一段。**MT6757D + SBC/SLA/DAA 全开**，官方工具无法读 `seccfg`，
唯一入口是 BootROM 的 linecode 整数溢出（kamakiri2）。

```
0e8d:0003 (BROM)
   │  ① 读 linecode + 溢出布局 + GET_STATUS 触发
   ▼
K2 访问原语可用（0xDA 寄存器读写，mode2 读 / mode3 写）
   │  ② canary 证明"真实写入"（不是 cache 影子）
   ▼
写 1336B patcher payload @ 0x100A00 → 0xE0 装载 → GET_STATUS(wIndex=0x0A) 触发
   │  ③ payload 主动回 4B ACK ×2
   ▼
A1A2A3A4 → 握手 A0 0A 50 05 → 上传 DA stage1 → 送 EMI/preloader → DRAM init → stage2
   │  ④ 关键：关掉 stage2 的"高速重连"，否则主机 SIGSEGV
   ▼
0e8d:2001 (DA)   ← 从这里开始用户态就能直连
   │  ⑤ 备份 10 个分区（门控）
   ▼
用设备自己的 SEJ 复现 LK 的 seccfg 哈希 → 写 512B → lock_state=3
   │
   ▼
重启 → LK 显示 Orange State → ro.boot.verifiedbootstate=orange
```

---

## 步骤 ①：拿到 K2 访问（kamakiri2 linecode 溢出）

### 本机型参数

| 项 | 值 |
| --- | --- |
| hwcode / target config | `0x551` / `0xe7`（sbc+sla+daa 全开） |
| `chipconfig.brom_register_access[0][1]` (`ptr_da`) | `0xa0e8` |
| `chipconfig.send_ptr` | `((0x1027B8, 0x9C2C),)` |
| `chipconfig.blacklist` | `[(0x102774, 0), (0x105704, 0)]` |
| `cmd_handler` | `43259` (0xA90B) |
| watchdog | `0x10007000`，复位寄存器 `+0x50` |
| payload 地址 | `0x100A00` |
| **命中组合** | **`offset = 8`，`trigger = 0x02FF`** |
| linecode 实测值 | `00c20100000008`（`0xA1/0x21` 读 7 字节） |

### 溢出帧构造

```python
# linecode 只有 7 字节可读（0xA1/0x25 的 8 字节请求在本机 STALL）
lc = dev.ctrl_transfer(0xA1, 0x21, 0, 0, 7)          # -> 00c20100000008
buf = bytearray(8 + 4)                                # offset=8 之后接地址
buf[:7] = lc[:7]
buf[7] = 0x00
buf[8:12] = struct.pack("<I", 0x100E00)               # 被覆盖出去的槽位地址
dev.ctrl_transfer(0x21, 0x20, 0, 0, buf)              # SET_LINE_CODING
dev.ctrl_transfer(0x80, 0x06, 0x02FF, 0, 9)           # GET_STATUS 触发（本机 02FF 可用）
```

> **`0x02FF` 会 STALL 不等于失败。** 本机 `0x0200` 也能返回描述符，但**真正打通的是 `0x02FF`**。
> 上游 kamakiri2 默认 offset=9（8 字节 linecode + 1 个 0），本机是 **offset=8**——
> 这个差异是本项目最大的坑，见 [07-troubleshooting.md](07-troubleshooting.md#offset-之争)。

### 步骤 ②：证明"真实读写"（硬门，别跳）

`status 0000` 和"回读一致"都**不能**单独当证据——错位/影子写会同时给出这两个假象。
本项目的判据是**双锚点 + canary**：

```python
# 1) 用 BROM 自己的 D4(WRITE32) 在 WDT scratch 打一个物理锚点
pre.write32(0x10007050, [0x11223344])          # V70_D4 prime ret=True
# 2) 让 K2 去读同一个地址：读到 = 物理地址空间已对齐
# 3) 用 K2 (mode3) 往 SRAM canary 写 0xDEADBEEF，再用 mode2 读回
k2.access(0x100D00, 4, pack("<I", 0xDEADBEEF), True, mode=3)
r = k2.access(0x100D00, 4, None, True, mode=2)
# V70_K2_TRY k2a n=30 off=8 trig=02ff ok=True r=0xdeadbeef   ← 这一行才是"打通"
```

日志判据：`V70_K2_HEALTHY`。没有这一行就不要往下写 payload。

> **注意 mode 语义**：`0xDA` 的 `mode0=读 / mode1=写 / mode2=读 / mode3=写`。
> 把 `mode1` 当读用会吞掉 4 字节写数据 → 帧失步 → `Pipe error (Errno 32)` → USB 栈报废。
> 本项目至少三次"莫名失联"是这个 bug 造成的。

### 步骤 ③：写 payload 与劫持

```python
PATCHER = "/tmp/mtknew/mtkclient/payloads/generic_patcher_payload.bin"   # 1336 B
# 逐字（4B）写 + 逐字回读，bad=0 才算过
# V70_PW_DONE patcher-0xa 0x100a00 bytes=1336 bad=0 secs=0.6
# 校验头 4 个字：01308fe2 13ff2fe1 2de9f04f 002387b0
# 劫持回调槽：rev = 0xa1000（ARM 跳转编码）
# 装载：0xE0 + 长度；触发：GET_STATUS(wIndex=0x0A)
```

触发后 BROM 的 `GET_STATUS` 会**超时约 1.3 秒**（正常现象，说明 payload 抢到了通道），
约 6 秒后收到 ACK：

```
[AGENT 15:44:42] V70_TRIG 0xa timeout ms=1305
[AGENT 15:44:48] V70_RX 0xa a1a2a3a4a1a2a3a4
[AGENT 15:44:48] V70_ACK a1a2a3a4a1a2a3a4        ← 两个 ACK
```

**必须清空 RX 再等**，否则上一条命令残留的 2 字节状态会被当成 ACK 前缀。
劫持写槽时 `check_status=False`：多读那 2 字节状态会把 payload 发出的 ACK 吃掉，
表现为"写成功但永远等不到 ACK"。

### 步骤 ④：握手 → DA → 关掉高速重连

```
HS = A0 0A 50 05   → 期望逐字节按位取反回显 5F F5 AF FA
HANDSHAKE_OK v70
DAXFlash - DRAM setup passed.
DAXFlash - Successfully uploaded stage 2
DAXFlash - DA SLA is disabled          ← SLA/DAA 已被 payload 清掉
```

**这一步的坑是主机侧的，不是手机侧的：**

> v69 已经跑通了整条链，但主机在 `DAXFlash.reinit()` 的
> *"Reconnecting to stage2 with higher speed"*（同一进程内 `port.close()` +
> `device.reset()` + 重新打开）里 **SIGSEGV，`rc=-11`**。
> `brom_kamakiri2.py`（开发期迭代号 v70）相对上一版的唯一改动，就是在 `ArgHandler` 里传 `noreconnect=True`，
> 让 DA2 保持在线而不是重连。**换手机没用，换 Windows 也没用。**

对应日志：`V70_RECONNECT reconnect=False`。

### 步骤 ⑤：DA2 直连接管（可选但极省事）

一旦 `0e8d:2001` 在线，**不需要重做 BROM**，用户态就能接管：

```bash
cp scripts/brom/*.py /tmp/          # 脚本互相 import，运行位置必须在 /tmp
python3 /tmp/da2_attach.py          # uid=501 即可，无需 sudo
python3 /tmp/da2_attach.py --probe  # 只想确认能接管、不做备份时
```

> 这里踩过一个坑：`da.connect()` 后必须显式给 `gpt_settings`，
> 否则所有 `readflash` 都报
> `AttributeError: 'NoneType' object has no attribute 'gpt_num_part_entries'`，
> 10 个分区备份全失败（`da2_run.log` 里那 10 行 `DA2_BACKUP_FAIL` 就是这个）。
> 而且**旧脚本在备份门控失败时还会 reset 设备**，把 DA2 会话打死——已改为不 reset。

---

## 端到端命令（本机实跑顺序）

```bash
# 准备
bash scripts/brom/setup_env.sh
cp scripts/brom/*.py /tmp/
export DYLD_FALLBACK_LIBRARY_PATH=/tmp/libusb

# 第一轮：打 BROM → DA。首轮没有备份，脚本会停在门控并保留 DA2
sudo env DYLD_FALLBACK_LIBRARY_PATH=$DYLD_FALLBACK_LIBRARY_PATH \
  /tmp/rootvenv/bin/python /tmp/da2_unlock_seccfg.py
# 期望：V78_DA_UPLOAD True → V78_GATE gpt=False ... → DA_KEPT_ALIVE_FOR_ATTACH

# 第二轮：用户态接管，只做备份
python3 /tmp/da2_attach.py
# 期望：DA2_REINIT_OK → 10 行 DA2_BACKUP_OK → DA2_BACKUP_GATE ok=True
# 注：这一步末尾还会试 mtkclient 的通用 seccfg unlock，本机返回
#     SECCFG_RESULT (False, 'Device has is either already unlocked or algo
#     is unknown. Aborting.')——不写入、无害，不要把它当解锁。

# 第三轮：重新进 BROM，同样一条命令，这次门控通过
sudo env DYLD_FALLBACK_LIBRARY_PATH=$DYLD_FALLBACK_LIBRARY_PATH \
  /tmp/rootvenv/bin/python /tmp/da2_unlock_seccfg.py
# 期望：V78_MATCH lk3+otp:boot10A dir=enc → V78_SECCFG_WRITE_OK
#       → V78_SECCFG_VERIFY match=True → AGENT_V78_DONE reason=unlocked
```

只有 `0e8d:0003` 阶段需要 root；DA2（`0e8d:2001`）用户态实测可用。
每轮之间设备会重新枚举，**必须重新物理进 BROM**，不能对死会话重试。

---

## 备份（写 seccfg 前的硬门）

```
/tmp/backup/
  gpt.bin            90 KB     ← 门控必需
  seccfg_before.bin  8 MB      ← 门控必需
  lk.bin             1 MB      （LK 反汇编用，见下节）
  boot1.bin          4 MB      （preloader，含 ROM info 密钥）
  boot2.bin          4 MB
  proinfo.bin        7.3 MB
  nvram.bin          5 MB
  protect1.bin       8 MB
  protect2.bin       12 MB
  recovery.img       31 MB
  boot.img           16 MB
```

门控逻辑：**`gpt` 与 `seccfg_before` 任一缺失/为空 → 拒绝写 seccfg 并且不复位设备**
（`DA2_BACKUP_GATE ok=False` → `DA2_UNLOCK_SKIPPED_BACKUP_MISSING`）。

---

## seccfg 解锁：复现 LK 自己的哈希

`seccfg` 不是"改一个字节就行"——LK 启动时会校验头部的 32 字节哈希，
哈希是**用 SoC 内的 SEJ 硬件引擎**算出来的，主机拿不到密钥。

### 结构

```python
# 头部 0x1C 字节参与哈希，整头 0x3C 字节
hdr = struct.pack("<IIIIIII",
    0x4D4D4D4D,   # 'MMMM' magic
    4,            # version
    0x3C,         # seccfg_size
    3,            # lock_state  ← 1=locked, 3=unlocked
    1,            # dm_verity_state
    0,            # secure_boot
    0x45454545,   # 'EEEE' tail
)
stored_hash = SEJ_AES_encrypt(sha256(hdr[:0x1C]))     # 32 B，LK 里 memcmp 0x20
```

LK 侧的确认点（从 `/tmp/backup/lk.bin` 反汇编）：
`0x2bb10` 处 `movs r1, #0x1c` → 哈希输入长度 28 字节；随后 `memcmp` 0x20 字节。

### 密钥：藏在 preloader 的 ROM info 里

`boot1.bin`（4 MB preloader）偏移 `0xad0` 处的 ROM info 结构里取出 32 字节，
内容是 `4d 45 49 5a 55` + 全零填充，即 ASCII **"MEIZU"** 补零。
把它作为 `SW_OTP` 送进 SEJ：

```
AES_enc(sha256(原始头)) == 设备上原存的哈希     # 逐字节相同
V78_MATCH lk3+otp:boot10A
```

**算法与密钥链被完整复现**，这一步是"能不能解锁"的真正判据——
不是"看起来能开机"，而是"能逐字节重现厂商自己算的哈希"。

### 写入与校验

1. 用同一 SEJ oracle 算新头的哈希；
2. 写 `512 B`（一个扇区）到 `seccfg` 起始扇区，分区其余部分原本全 0，未破坏其他数据；
3. 双重校验：
   - `V78_SECCFG_VERIFY match=True`（设备回读比对）
   - `V78_SECCFG_VERIFY_CRYPT ok=True`（SEJ 解密回环）
4. `shutdown(bootmode=0)` → 手动长按电源开机。

### 成功判据

```
LK 屏幕：Orange State — Your device has been unlocked and can't be trusted
adb:    ro.boot.verifiedbootstate = orange
        ro.boot.flash.locked      = (Android 6 无此属性，以 orange 为准)
```

`Orange State` 是 LK 读到 `lock_state=3` **且哈希校验通过**才会显示的，
和写入的 seccfg 互为印证。

---

## 回退

`seccfg_before.bin` 就是回退包。回退用 `da2_partition.py`（复用 `da2_sej_tools.py` 已验证的
`find_part` + `readflash/writeflash`，前提是 DA2 还活着）：

```bash
# 先跑 da2_unlock_seccfg.py 让设备停在 0e8d:2001，然后：
python3 /tmp/da2_partition.py write seccfg /tmp/backup/seccfg_before.bin --yes
```

写入前脚本要求在 `/tmp/backup/` 里已经存在该分区的备份（接受
`<part>.bin|<part>.img|<part>_before.bin|<part>_before.img` 任一非空文件），
写完做整区回读 md5 校验。**注意：本机执行的是"上锁→解锁"方向，
反方向的回退没有实机跑过，属于未验证路径**，请把 `seccfg_before.bin` 另存一份。

`lk.bin` / `boot1.bin` / `boot2.bin` 是变砖时的最后手段（需要 BROM 重写 preloader，
风险极高，本仓库不提供一键脚本）。
