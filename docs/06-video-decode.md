# 06 · 本地视频卡顿：解码能力实测

> 结论：**SoC 没问题**。这台机器的硬件解码矩阵覆盖 AVC/HEVC 8-bit 到 4K30，
> 唯一的真实缺口是 **HEVC Main10（10-bit）**——硬解不支持、软解能力又被写死在
> 1280x1280。所以"看本地视频卡"的实际内容几乎一定是 10-bit x265 片源。

标注：**【实机】** 本机实测；**【源码】** 从配置文件/代码核对。

---

## 1. 平台声明的解码能力【源码】

`/system/etc/media_codecs_mediatek_video.xml`（本机原文，grep 即可复核）：

```
OMX.MTK.VIDEO.DECODER.AVC    type=video/avc    size max="4096x2176"  concurrent=16
OMX.MTK.VIDEO.DECODER.HEVC   type=video/hevc   size max="4096x2176"  concurrent=16
OMX.MTK.VIDEO.DECODER.MPEG2 / MPEG4 / H263     size max="1920x1088" / "1408x1152"
```

软解兜底在 `media_codecs_google_video_le.xml`（`_le` = low end，专为低配机砍过）：

```
OMX.google.hevc.decoder   size max="1280x1280"  block-size 8x8
                          blocks-per-second range="1-432000"
                          bitrate range="1-5000000"
OMX.google.h264.decoder   size max="2048x2048"  blocks-per-second 1-489600
```

`432000 blocks/s ÷ (1920/8 × 1080/8 = 32400 blocks) ≈ 13.3 fps`——
**1080p 的 HEVC 软解在这台机器上连 15 fps 都跑不满**，这就是"卡"的算术来源。
（配置里的 `max=1280x1280` 只是尺寸上限，实际 1080p 请求会被 MediaCodec
按能力表拒绝或降速，两种表现都是卡顿。）

**MTK 硬解 HEVC 只声明 Main profile，没有 Main 10**——这是唯一的真缺口。

---

## 2. 实测矩阵【实机】

片源由 `scripts/video/make_samples.sh` 用 `testsrc2` 现场生成（无版权、可复现），
参数见脚本；测量脚本 `scripts/video/bench_decode.sh`。

| 片源 | 编码 | 结果 |
| --- | --- | --- |
| avc1080.mp4 | H.264 Baseline 1920x1080 30p | 流畅 |
| avc1080p60.mp4 | H.264 Baseline 1920x1080 **60p** | 流畅 |
| hevc1080.mp4 | HEVC Main 1920x1080 30p（yuv420p） | 流畅 |
| hevc1080p60.mp4 | HEVC Main 1920x1080 60p | 流畅 |
| hevc4k.mp4 | HEVC Main **3840x2160** 30p | 流畅（画面时间码核对播完） |
| hevc10_1080.mp4 | HEVC **Main 10** 1920x1080 30p（p010le） | **系统播放器卡顿/花屏** |

同一台机器、同一张卡、同一时段：8-bit 4K30 能放，10-bit 1080p30 不能放
→ 瓶颈与分辨率、码率、存储 IO、CPU 频率都无关，**只与 10-bit profile 有关**。

### 测量本身的坑（重要）

`dumpsys SurfaceFlinger --latency <layer>` 两次给出**完全相同**的数值
（33.12 / 49.7 ms），因为层名不对或播放器已暂停时它返回的是**上一次记录的时间戳**。
两次 4K 测试都被这个假数据骗过，最后靠"画面里烧录的时间码 + 截图"才确认
是否真在播放。`bench_decode.sh` 因此同时抓三样东西：

1. logcat 里实际选中的 codec 名（`OMX.MTK.VIDEO.DECODER.HEVC` vs `OMX.google.hevc.decoder`）
   ——这比 latency 表可靠得多；
2. SurfaceFlinger 帧间隔（仅参考）；
3. 截图，人工确认在播。

只凭第 2 项下结论会出错，别省第 1、3 步。

---

## 3. 两条出路

### A. 用带自己解码器的播放器（推荐，零转码）

VLC for Android 3.6.5 自带 libvlc/ffmpeg，**不走** Android MediaCodec，
所以 10-bit 是纯软件解码：

```bash
# 实测：10-bit 1080p30 实时，画面内烧录时间码与真实时间同步，CPU ~16%
adb install VLC-Android-3.6.5-arm64-v8a.apk
```

判据用"时间码是否追上真实时间"，不要用"是否报错"——卡顿不报错。

> 官方站 `get.videolan.org` 本机只有 5.7 MB / 300 s 的龟速；
> 用镜像：`https://mirrors.ustc.edu.cn/videolan-ftp/vlc-android/3.6.5/VLC-Android-3.6.5-arm64-v8a.apk`
> （实测 4 MB/s）。

其他自带软解的播放器同理（MX Player 的 HEVC 插件、mpv、nPlayer）。

### B. 转成 8-bit（要给系统播放器/其他 App 用时）

```bash
ffmpeg -i in_10bit.mp4 -c:v libx265 -pix_fmt yuv420p \
       -x265-params profile=main -crf 22 -preset medium \
       -c:a copy out_8bit.mp4
```

`-pix_fmt yuv420p` + `profile=main` 是关键，其余按口味。
批量转换就是 `for f in *.mp4; do ...; done`，不在本仓库脚本里代劳。

### 别在根上做的事

- **别升 Android**。这台机器官方上限 Flyme 7 / Android 6.0.1，MT6757D 的
  10-bit 能力不会因为换系统出现（缺的是硬件 profile 支持，不是驱动）。
- **别指望 Magisk 模块加解码器**。硬解缺口在 VPU 固件层，模块只能换软解配置
  （`media_codecs*.xml`），改小 `_le` 上限只会让软解更容易被选中，
  13 fps 的天花板不变。

---

## 4. 如果你遇到的不是 10-bit

同一套 `bench_decode.sh` 直接给出方向：logcat 里如果选中的是
`OMX.google.*`（而不是 `OMX.MTK.*`），说明**硬解被拒绝**了，常见原因：
分辨率/码率超出上表 limit、`profile` 不在声明范围、`concurrent-instances` 被占满。
那种情况先让片源落回 limit 内，再去怀疑性能。
