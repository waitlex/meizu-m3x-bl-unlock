# 04 · WebView 升级：把 Bromite 106 伪装成 `com.android.webview`

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

> **别用魅族自带「浏览器」验证。** 它自带 Chromium 45 引擎（UA 也是硬编码的），
> **不走系统 WebView provider**——`/proc/<pid>/maps` 里根本没有 `libwebviewchromium`。
> 要享受新内核，请用走系统 WebView 的应用（HTML 查看器、各类第三方浏览器、App 内嵌页）。

## 回退

```bash
adb shell su -c 'magisk module disable webview106' && adb reboot
```

系统原生 `/system/app/webview/webview.apk` 从未被改动。
