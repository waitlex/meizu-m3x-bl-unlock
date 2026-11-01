# 04 · WebView 升级：把 Bromite 伪装成 `com.android.webview`

> **结论（2026-11-01 版本二分后更新）：本机可用上限是 Bromite 98.0.4758.116，模块现已启用。**
> 106 能装上、64 位页面能渲染，但**所有 32 位应用 100% 打不开**（图库、全局搜索），
> Via 搜索时也会闪退；二分证明这不是 106 独有，**99 起就崩**，机制见
> 「实机副作用」与「版本二分」两节。
> 打包/挂载链路本身（AXML + `resources.arsc` + `lib/<isa>` 三处对齐）是打通的，可放心复用。

## 问题

Flyme 5.2.8.3A 自带的 WebView provider 是魅族魔改版（内核 Chromium 44/45），
Google Play 又不在，`Android System WebView` 无法更新 → 大量现代网页白屏/排版崩坏。

Android 6 的 WebView 加载链是：

```
WebViewFactory.loadInstalledWebViewPackages()
  → 读 <provider>.apk 的 resources.arsc，取 "assigned package identifiers"
  → 与 manifest 的 package name 三方比对
  → getPackageInfo(...).nativeLibraryDirectories  ← .so 的查找路径在这里定
```

**只要 APK 的 manifest 包名和 `resources.arsc` 里的包名不一致，就抛
`RuntimeException: Package not found: com.android.webview`。**
这是把第三方 WebView 塞进 provider 位置的头号障碍。

## 方案总览

不替换 `/system`（保持 systemless，可一键回退），改用 Magisk 模块把
Bromite WebView 106 挂到 `/system/app/webview/`，并让**三处包名统一为
`com.android.webview`**：

| 位置 | 原值 | 改成 | 工具 |
| --- | --- | --- | --- |
| `AndroidManifest.xml`（AXML）package | `org.bromite.webview` | `com.android.webview` | `axml_patch.py` |
| `resources.arsc` package chunk 名 | `org.bromite.webview` | `com.android.webview` | `patch_arsc.py` |
| 签名 | Bromite key | 自签 v1（SHA-256） | `sign_apk.sh` |

## 步骤

### 1. 下载 Bromite WebView

需要 **arm64 与 arm 两个包**（本机系统应用 32 位占多数，两个 ABI 都得有）：

```
https://www.bromite.org/fdroid/repo/          # 或 GitHub bromite/bromite releases
Bromite-106-*-webview-arm64-v8a.apk           # 本项目实测 99,330,457 B
Bromite-106-*-webview-armeabi-v7a.apk         # 53,180,574 B
```

> 版本选择：**106 是能在 Android 6 上跑的最高实用版本**（107+ 起 `minSdkVersion` 提到 24）。
> 不必先下满 100 MB：用 `curl -r` 只取 APK 尾部（zip 中央目录）和
> `AndroidManifest.xml` 的本地头，或直接看 F-Droid 页面标注的 `minSdkVersion`。

### 2. 改 AXML 包名

```bash
python3 scripts/webview/axml_patch.py Bromite-106-arm64.apk stage.apk \
        org.bromite.webview com.android.webview
# 重写的是 AXML 字符串池里的包名条目，同时剥掉 META-INF 旧签名
```

### 3. 改 `resources.arsc` 包名（关键）

```bash
python3 scripts/webview/patch_arsc.py out.apk fixed.apk
# package chunks: [(288, 288, 0x7f, 'com.android.webview')]
# OK
```

脚本要点（`patch_arsc.py` 已实现，这里说明**为什么**要这么做）：

- ARSC 布局：`ResTable_header(0x0002, 12B)` → 全局 string pool → `package chunk(0x0200)`；
  包名是 package header 偏移 `+12` 起的 **256 字节 UTF-16LE** 字段。
- **必须原位改写**：长度不变才不会移动后续任何偏移（否则要重建整个资源表）。
- `resources.arsc` 在 APK 里是 **STORED**（`compress_type == 0`），所以可以直接改磁盘字节。
- 改完必须**同步 CRC32 两处**：zip 本地头 `+14` 和中央目录项 `+16`。
  只改一处 → `zip` 校验失败 / `PackageManager` 拒收。

### 4. 重签名

```bash
bash scripts/webview/sign_apk.sh fixed.apk signed.apk
```

必须是 **v1（JAR）签名**——Android 6 不认纯 v2。且新版 JDK 禁用了 SHA1：

```bash
jarsigner -digestalg SHA-256 -sigalg SHA256withRSA ...   # 缺这两个参数会直接报错
```

### 5. 建模块并部署

```bash
# 参数：签名后的 APK + Bromite 原始 arm64/arm APK（用来抽 .so）
bash scripts/webview/build_module.sh signed.apk \
     Bromite-106-arm64.apk Bromite-106-arm.apk
# 脚本直接铺 /data/adb/modules/webview106/ 然后 reboot（不走 zip 安装）
# 想先看目录结构和文件大小、不碰设备：DRY=1 bash scripts/webview/build_module.sh ...
```

**模块目录结构是成败关键**：

```
webview106/
  module.prop
  system/app/webview/webview.apk
  system/app/webview/lib/arm64/libwebviewchromium.so            ← 64 位
  system/app/webview/lib/arm64/libcrashpad_handler_trampoline.so
  system/app/webview/lib/arm/libwebviewchromium.so              ← 32 位
  system/app/webview/lib/arm/libcrashpad_handler_trampoline.so
  system/lib64/webview/  system/lib/webview/                    （兼容旧路径，可选）
```

> **两个必踩的坑：**
>
> 1. **只放 `/system/lib64/webview/` 不够。** 实测 provider 的
>    `nativeLibraryDirectories` 是
>    `[/system/app/webview/lib/arm64, /system/app/webview/webview.apk!/lib/arm64-v8a,
>      /vendor/lib64, /system/lib64]`（魅族定制，**不是** AOSP 的 `/system/lib64/webview`）。
>    缺 `lib/arm64` 就是 `UnsatisfiedLinkError: couldn't find "libwebviewchromium.so"`。
> 2. **目录名是 `lib/arm64` 和 `lib/arm`，不是 `lib/arm64-v8a` / `lib/armeabi-v7a`。**
>    规则来自 `VMRuntime.getInstructionSet()`：`armeabi-v7a → arm`、`arm64-v8a → arm64`。
> 3. **`.so` 不能只留在 APK 里**：Bromite 的 `lib/*` 条目是 deflate 压缩的，
>    `apk!/lib/...` 这条路加载不了，必须解出来落到磁盘目录。

Magisk 的 magic mount 支持向只读 `/system` **新增目录**（把父目录变成 tmpfs 镜像再
bind mount），所以 `/system/app/webview/lib/` 这个原本不存在的目录能挂出来。

## 验证

```bash
# 1) provider 版本与路径
adb shell dumpsys package com.android.webview | grep -E 'versionName|codePath'
#    versionName=106.0.5249.163   codePath=/system/app/webview

# 2) 宿主进程真的把新 .so 映射进去了
adb shell su -c "cat /proc/$(adb shell pidproc)/maps" | grep libwebviewchromium
#    /system/app/webview/lib/arm64/libwebviewchromium.so   ← 路径必须是这个

# 3) 内核特性实测（最硬的证据）
```

第 3 步用**纯 CSS 特性页**，因为很多简易 HTML 查看器不执行 JS：

```
:has(> span)   aspect-ratio   conic-gradient()   flex gap
```

这四项在 Chromium 88+ 才齐（`:has()` 实际是 105），Chrome 45 一个都不支持。
本项目实测页面见 `scripts/webview/css_probe.html`，四项全部生效 → 内核确为 106。

> 这只证明**64 位渲染路径**可用。它同时打死了所有 32 位应用，见下一节。

> **别用魅族自带「浏览器」验证。** 它自带 Chromium 45 引擎（UA 也是硬编码的），
> **不走系统 WebView provider**——`/proc/<pid>/maps` 里根本没有 `libwebviewchromium`。
> 要享受新内核，请用走系统 WebView 的应用（HTML 查看器、各类第三方浏览器、App 内嵌页）。

## ⚠ 实机副作用：106 让 32 位应用必崩（故停用，改装 98）

【实机 2026-11-01 20:30】装上 106 之后，凡是走系统 WebView provider 的应用：

| 现象 | 进程 | ABI | 信号 |
| --- | --- | --- | --- |
| 图库打不开（100% 复现） | `com.meizu.media.gallery` 5.4.1 | `arm` | `SIGBUS / BUS_ADRALN` |
| 全局搜索打不开 | `com.meizu.net.search` | `arm` | `SIGBUS / BUS_ADRALN` |
| 搜索网页时闪退 | `mark.via` | `arm64` | `SIGTRAP / TRAP_BRKPT` |

`/data/system/dropbox` 里 30 条 tombstone 聚合：22 条图库 + 1 条搜索（`arm`，
**每一条的 12 层栈完全相同**）+ 6 条 Via（`arm64`）+ 1 条无关。

**先排除打包错误**：设备上两份 `.so` 与 `webview.apk` 内
`lib/armeabi-v7a/`、`lib/arm64-v8a/` 条目逐字节一致
（md5 `949cf71862d3b1c03f4422f263e83d69` / `5a4858012aaf6583becfc7665e743922`），
APK 与 `.so` 也确实来自同一次构建。不是解压损坏，也不是 ABI 放错目录。

把 tombstone 的 pc 偏移直接反汇编到 Bromite 的 arm `.so` 上（偏移即文件内地址）：

```
0x0023f6d68   ldr   r0, [r0, #4]        ; 取内部表指针
0x0023f6d6a   add.w r0, r0, r1, lsl #2
0x0023f6d6e   ldm   r0!, {r1}           ; ← 故障点，r0 = 0xdb29ab0f
```

`r0` 每次不同但都是奇数（`0xdada7b0f` / `0xdaed5b0f` / `0xdb29ab0f`），
`ldm` 要求 4 字节对齐 → 对齐异常。**指针本身是垃圾值**，而 `#11` 是 JNI 入口
`Java_J_N_M1Y_1XVCN+108`，说明挂在 WebView 初始化的调用链里，还没到渲染。

Via 的两处 pc（`0x6ba1460`、`0x4d426ec`）反汇编出来都是 `brk #0`，
即 Chromium 的 `IMMEDIATE_CRASH()` / `__builtin_trap()` —— **库自己 CHECK 失败主动中止**。

> 【实机】停用模块后：图库正常打开（相册/缩略图可见），Via 冷启动并渲染主页，
> `logcat` 零 `Fatal signal`，dropbox 零新增 tombstone。因果闭合。

结论：**「能装上、64 位页面能渲染」≠「能在这台 Android 6 上跑」**。
arm32 完全不可用，arm64 会在特定路径主动 trap。想留在 106 就得接受图库/搜索打不开；
两者不可兼得，除非换一个不触发上述分支的内核构建（未验证）。

## 版本二分：98 是本机上限【实机 2026-11-01 21:34】

一轮 = 下载 → 改包名/重签 → 部署 → 重启 → 判据，脚本
[`scripts/webview/bromite_round.sh`](../scripts/webview/bromite_round.sh)：

```bash
bash scripts/webview/bromite_round.sh 98.0.4758.116
```

四道门，每一道都对应一次「差点被当成成功」的失败：

| 门 | 判据 | 为什么必须有 |
| --- | --- | --- |
| `deploy` | 设备上每个模块文件 md5 == 暂存副本 | 早期 `su -c "a && b"` 的引号 bug 让全部写操作以 uid 2000 失败，模块没动过却报 PASS |
| `GATEVER` | `dumpsys package com.android.webview` 的 `versionName` == 本轮 tag | provider 不是候选时，后面测的是上一个内核，整轮无意义 → 直接 revert 并判 `INVALID` |
| `GATE32` | 32 位图库起来 + `Fatal signal`=0 + 无新 tombstone/ANR | 就是本次要找的那条断崖 |
| `GATE64` | 64 位 Via 起来并加载页面，无 fatal/新 tombstone | 证明 arm64 侧没退化 |

结果：

| Bromite | provider 确认 | GATE32 | 崩溃点（arm `.so` 内偏移） |
| --- | --- | --- | --- |
| 105.0.5195.147 | ✅ `versionName=105.0.5195.147` | ❌ | pc `023ca78e`，`fault addr 0xda…aa7` |
| 101.0.4951.69 | ✅ | ❌ | pc `022ff156`，`fault addr 0xda…703` |
| 99.0.4844.77 | ✅ | ❌ | pc `022b9a62`，`fault addr 0xda…856` |
| **98.0.4758.116** | ✅ | **✅** | — |
| 90.0.4430.204 | ✅ | ✅ | — |

三个失败版本的 tombstone 全是同一形态：`ABI: 'arm'`、`SIGBUS / BUS_ADRALN`、
12 层栈完全相同、每次只有高位随 ASLR 变化而低 12 位恒定 → 同一个未对齐的表基指针，
不是随机内存损坏。所以 106 那次不是偶发，**断崖就在 98 与 99 之间**。

98 的加严验证（不只是进程存活）：Via 里手输 `https://www.baidu.com/s?wd=localsend`
渲染出真实搜索结果，Chromium 错误页（`net::ERR_NAME_NOT_RESOLVED`）也由新内核绘制；
图库相册/缩略图正常；`dropbox` 的 `anr+tombstone` 计数 45 → 45，`logcat` 零 `Fatal signal`。

> 【实机】`am start -a android.intent.action.VIEW -p mark.via` 会让 Via 的
> `.Trampoline` 自己报 ANR（"no window has focus"），主线程栈停在
> `nativePollOnce`，与内核无关 —— 别把它读成 WebView 卡死；验证渲染要用真实输入流。

### 二分过程中新踩到的三个坑

1. **`adb shell su -c "a && b"`**：`&&` 之后的部分被设备 shell 抢走，以 uid 2000 执行 →
   一律 `adb shell "su -c '…'"`；要连着做多个写操作就 push 一个 `deploy.sh` 再
   `su -c 'sh /data/local/tmp/deploy.sh'`。
2. **AXML 包名要从 manifest 里读**：按字符串池猜包名会抓到
   `com.android.webview.SHOW_DEV_UI` 这类常量，`axml_patch` 变成空操作，APK 仍是
   `org.bromite.webview`，永远当不上 provider。用
   [`manifest_info.py`](../scripts/webview/manifest_info.py) 解析 `<manifest package=…>`。
3. **必须剥掉厂商自带签名**：Bromite 90/87 的包名本来就是 `com.android.webview`，
   不走 `axml_patch` 就没人删 `META-INF/`，产物里会同时留下 `WEBVIEW.SF/RSA`（我们签的）
   和 `BROMITEK.SF/RSA`（厂商的）。Android 6 的 `Signature.fromJar` 只要有一个签名者
   校验不过就抛异常，PMS 直接**不安装这个包**：
   `dumpsys package com.android.webview` 全空、`WebViewFactory` 报
   `NameNotFoundException: com.android.webview`。现在无论要不要改包名都过一遍
   `axml_patch`，并断言 `step1.apk` 里不再含 `.SF/.RSA/.DSA/.EC`。

另外：Android 6 只有 `md5sum`/`sha1sum`，**没有 `sha256sum`**，用它做校验会得到空字符串，
看起来像「内容不一致」。校验一律用 md5。

## 回退

```bash
# 任选其一，都是可逆的
adb shell su -c 'magisk module disable webview106'
adb shell su -c 'touch /data/adb/modules/webview106/disable'   # 实测可用
adb reboot
```

系统原生 `/system/app/webview/webview.apk` 从未被改动，停用即回到出厂内核
（`versionName=Developer Build`、`versionCode=1`，Chromium 44/45）。
重新启用：`rm /data/adb/modules/webview106/disable && reboot`。

> 【实机 2026-11-01 21:34】本机现状：模块**已启用**，内容是二分选出的
> `98.0.4758.116`（`versionCode=1645754520`，`targetSdk=31`）。模块 id 仍叫
> `webview106`，只是历史名字，`module.prop` 里的 `version` 才反映真实内核。
