# 05 · 修复打电话/收发短信（MTK VoLTE / IMS）

> 结论先说：**四个软件开关已全部打开，IMS PDN 建立、三个 volte 原生服务 running**
> （这些都【实机】）。但**真实通话与短信从未验证过**，本文不声称已修复；
> 末尾一节说明当前 SIM（电信 46011）与当时排障用的移动卡不一致这个事实，
> 以及为什么"缺一行 IMS APN"这个说法证据不足。

标注：**【实机】** = 本机执行并有日志/属性证据；**【源码】** = 从代码/文档推断；
**【未验证】** = 从未成功执行。

---

## 症状与判定

现象：插卡能上网（LTE 数据正常），但**打不出电话、收不到短信**。
这类机器的判定顺序：

```bash
adb shell su -c "getprop | grep -E 'ro.mtk_volte_support|ro.mtk_ims_support'"
# ro.mtk_volte_support = 1  → 固件本身支持，问题在配置不在硬件

adb shell su -c "dumpsys telephony.registry | grep -i 'Capabilities: IMS'"
# 有 IMS capability 的 cellular 连接 = IMS PDN 已经建起来

adb shell su -c "ip -6 addr show dev ccmni4"
# MTK 的 IMS 走专用链路 ccmni4；只看到 fe80:: 链路本地地址 = IMS PDN 没起来
```

**【实机】** 本机现在的状态（写本文时复核）：

```
[persist.mtk.volte.enable]: [1]
[persist.mtk_dynamic_ims_switch]: [1]
[persist.radio.imsallowmtsms]: [1]
[ro.mtk_volte_support]: [1]
[init.svc.volte_imcb]: [running]
[init.svc.volte_stack]: [running]
[init.svc.volte_ua]: [running]
volte_vt_enabled = 1

dumpsys connectivity:
  type: MOBILE[LTE], state: CONNECTED/CONNECTED, extra: ims, network{1001}
  InterfaceName: ccmni4
  Capabilities: IMS&TRUSTED&NOT_VPN&VALIDATED
  LinkAddresses: [240e:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx/64, fe80::.../128]
  DnsAddresses: [240e:xxxx:xxxx::1, 240e:xxxx:xxxx::33]
  PcscfAddresses: []

ps: com.mediatek.ims(1930)  /system/bin/volte_stack  /system/bin/volte_ua  /system/bin/volte_imcb
```

`everValidated{true}` + 三个 volte 服务 running + 状态栏 `4G+HD` 说明
**IMS 承载层已经建立**；但这不等于 SIP 注册成功，更不等于能打电话。

> **隐私警告**：`getprop | grep ims` 会把 **IMSI 和 ICCID 明文打出来**
> （`gsm.sim.operator.imsi`、`persist.radio.simswitch.iccid`）。
> 截图、贴日志、提 issue 之前先删掉这两行。
> 同理 `ip -6 addr` 会暴露你的 IPv6 前缀 + 接口 ID（能定位到城市和具体设备）。

---

## 三层开关，缺一不可

| 层 | 载体 | 值 | 作用 | 本机是否已验证 |
| --- | --- | --- | --- | --- |
| ① UI/设置 | `Settings.Global` | `volte_vt_enabled=1` | 拨号盘读这个才显示 VoLTE 开关 | 【实机】 |
| ② MTK 协议栈 | `persist.mtk.volte.enable` | `1` | volte_imcb / volte_stack / volte_ua 是否放行 | 【实机】 |
| ② MTK 协议栈 | `persist.mtk_dynamic_ims_switch` | `1` | 允许动态建立 IMS PDN | 【实机】 |
| ③ 短信回落 | `persist.radio.imsallowmtsms` | `1` | IMS 不可用时允许走 CS/MT 短信 | 【实机】（配置层，短信实收未验） |
| ④ 承载 | APN 数据库 | 一行 `type=ims` | 有些机型必须它才给 IMS PDN | **【未验证】见下** |

关于第 ④ 层，我必须把话说准：

当时排障**同时**做了"改上面四个开关"和"补 `type=ims` APN 行"两件事，
事后无法归因。今天复核发现：

- 当前 SIM 是电信 `46011`，APN 库里**没有** 460/11 的 `ims` 行；
- 可 `dumpsys connectivity` 显示 IMS PDN 照样是 `CONNECTED` 且
  `extra: ims`、`Capabilities: IMS&...&VALIDATED`。

也就是说 **MTK 侧是按 APN 名字 `ims` 直接向 RIL 请求 PDN 的，
并不依赖 `TelephonyProvider` 数据库里那一行**。
所以"缺 IMS APN 行就是根因"这个说法**证据不足**，本文不采用。
补那一行仍然写进脚本，因为：某些运营商的 IMS PDN 需要正确的
`protocol=IPv4v6`/`bearer` 才给建链，而这在数据库行里配置。

> `PcscfAddresses: []` 是**正常**的。MTK 不靠 DHCP 下发 P-CSCF，而是拿 IMS 域
> 去做 DNS SRV 查询（`_sip._tcp.<realm>`）；不要因为它为空就判失败。

---

## 操作

```bash
bash scripts/volte/enable_volte.sh --show     # 只读，先看当前状态
bash scripts/volte/enable_volte.sh            # 应用（含 reboot）
bash scripts/volte/enable_volte.sh --revert   # 撤销
NO_REBOOT=1 bash scripts/volte/enable_volte.sh   # 改属性但不重启
```

脚本做的事与上面表格一一对应；APN 行按**当前 SIM 的 numeric** 插入，
`protocol=IPv4v6`、`carrier_enabled=1`、`bearer=-1`（不限定制接入技术）。

手工等价命令（不想跑脚本时）：

```bash
adb shell su -c "settings put global volte_vt_enabled 1"
adb shell su -c "setprop persist.mtk.volte.enable 1"
adb shell su -c "setprop persist.mtk_dynamic_ims_switch 1"
adb shell su -c "setprop persist.radio.imsallowmtsms 1"
adb reboot
```

`setprop` 对 `persist.*` 会写 `/data/property`，重启后仍在【实机】；
非 persist 的属性重启即丢，别用。

---

## 与当前 SIM 的警告（请读）

本文档写作时复核设备，实际插的是 **中国电信 46011**
（`gsm.sim.operator.numeric=46011`，`dumpsys` 里 `CSS not supported`），
而 APN 数据库里被补的两条 `type=ims` 行是 **460/00 与 460/02（中国移动）**：

```
_id=1188  name=ims  apn=ims  type=ims  mcc=460 mnc=00  numeric=46000
_id=1202  name=ims  apn=ims  type=ims  mcc=460 mnc=02  numeric=46002
```

也就是说：**当时排障是按移动卡走的，电信卡这一侧的 IMS APN 行至今没有补。**
`scripts/volte/enable_volte.sh --show` 现在也会直接告诉你
`(no IMS APN for this operator)`。
但如上一节所说，IMS PDN 在缺这一行的情况下也已经建起来了，
所以**不要把它当成"电话打不通的原因"**——它只是一个可能影响建链参数的待补项。

如果电信卡仍打不了电话，按顺序检查：

1. **运营商侧有没有开通 VoLTE 业务**（电信需要主动开通，未开通时终端怎么配都没用）。
   电信另有终端库要求，这台 2016 年的魅蓝 X（Flyme 5 / 7 时代）不在电信 VoLTE 支持列表里【未验证，属公开资料推断】。
2. **补一条匹配当前 numeric 的 IMS APN**（可能不必要，见上一节）：
   `bash scripts/volte/enable_volte.sh` 会按 `gsm.sim.operator.numeric` 自动插入
   （脚本从设备读 mcc/mnc，不会写死 46000）。
3. **短信走回落**：`persist.radio.imsallowmtsms=1` 已开，IMS 不通时应退回 MT 短信；
   若仍收不到，检查是否被"仅 IMS 短信"策略卡住（Flyme 的短信设置里也有独立开关）。
4. **CS 域**：`CSS not supported` 意味着这层 LTE 不支持电路交换回落【实机】，
   语音只能靠 VoLTE；VoLTE 不成 → 电话必失败。这种情况下唯一稳的路是
   运营商开通 VoLTE，或换一张已在白名单内的卡验证软件链路是否正确。

**验证判据（缺一不可，缺就不能说"修好了"）**：
拨一次真实号码并接通；收发一次真实短信；重启后 `enable_volte.sh --show`
仍显示 `1/1/1`，且 `dumpsys connectivity` 里 IMS 网络仍是
`CONNECTED` + `Capabilities: IMS&...&VALIDATED`。
