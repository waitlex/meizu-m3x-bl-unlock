# webview106 — Magisk 模块骨架

> **本机现状：已停用**（`/data/adb/modules/webview106/disable`，2026-11-01 20:30）。
> 装上后 64 位页面能渲染 Chromium 106，但**所有 32 位应用一开就崩**
> （图库、全局搜索 `SIGBUS`；Via 搜索 `SIGTRAP`）。原因与回退见
> [`docs/04-webview-106.md`](../../docs/04-webview-106.md) 末尾一节。

本目录**只有 `module.prop`**。APK 与 `.so` 不在仓库里（体积 100 MB+，
且 Bromite 二进制应由你自己下载以保证来源可信），所以这里是一个骨架，
不是可直接安装的 zip。

## 怎么把它变成可用模块

```bash
# 1) 按 docs/04 改包名 + 重签名，得到 signed.apk
# 2) 一条命令铺到设备并重启
bash ../../scripts/webview/build_module.sh signed.apk \
     Bromite-106-arm64.apk Bromite-106-arm.apk
```

## 生效的目录结构（本机 `/data/adb/modules/webview106/` 实况）

```
module.prop
system/app/webview/webview.apk                          98,247,654 B
system/app/webview/lib/arm64/libwebviewchromium.so     131,089,968 B
system/app/webview/lib/arm64/libcrashpad_handler_trampoline.so   5,160 B
system/app/webview/lib/arm/libwebviewchromium.so        69,286,468 B
system/app/webview/lib/arm/libcrashpad_handler_trampoline.so   3,264 B
system/lib64/webview/{同上两个 64 位 .so}
system/lib/webview/{同上两个 32 位 .so}
```

没有 `customize.sh`、没有 `update-binary`、没有 `system.prop`——
Magisk 的 magic mount 会自己 bind-mount `system/` 目录，多余的脚本反而增加出错面。

## 三个必须一致的标识

| 位置 | 值 |
| --- | --- |
| `AndroidManifest.xml` 包名（AXML 字符串池） | `com.android.webview` |
| `resources.arsc` package chunk 包名（+12 起 256B UTF-16LE） | `com.android.webview` |
| 模块内目录名 | `system/app/webview/` |

任意两处不一致，Android 6 的 `WebViewFactory` 就会退回旧内核或干脆崩溃。
详见 [../../docs/04-webview-106.md](../../docs/04-webview-106.md)。

## `module.prop` 里的 `author=local`

发布前把它改成你自己的 GitHub 用户名。`versionCode=1060524` 只要
不小于原厂包即可，别写得更小，否则 `PackageManager` 会拒绝替换。
