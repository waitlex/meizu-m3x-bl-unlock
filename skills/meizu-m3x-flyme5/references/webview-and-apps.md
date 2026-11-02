# WebView、状态栏与 API 23 应用适配

## 一、WebView provider 替换

Android 6 的加载链：`WebViewFactory.loadInstalledWebViewPackages()` 读 provider APK 的
`resources.arsc` 取 assigned package identifiers，与 manifest 包名三方比对，再用
`getPackageInfo().nativeLibraryDirectories` 定 `.so` 查找路径。**三处包名必须一致**，否则抛
`RuntimeException: Package not found: com.android.webview`。

| 位置 | 原值 | 改成 | 工具 |
| --- | --- | --- | --- |
| `AndroidManifest.xml`（AXML）package | `org.bromite.webview` | `com.android.webview` | `scripts/webview/axml_patch.py` |
| `resources.arsc` package chunk 名 | `org.bromite.webview` | `com.android.webview` | `scripts/webview/patch_arsc.py` |
| 签名 | Bromite key | 自签 v1（SHA-256） | `scripts/webview/sign_apk.sh` |

部署走 Magisk 模块挂到 `/system/app/webview/`（systemless，可一键回退），不要直接改 `/system`。

一轮完整流程（下载 → 改包名 → 重签 → 部署 → 重启 → 判据）：

```bash
bash scripts/webview/bromite_round.sh 98.0.4758.116
```

手工装配已有签名包：`bash scripts/webview/build_module.sh signed.apk bromite_arm64.apk bromite_arm.apk`。

### 判据（四道门，缺一不可）

1. `dumpsys package com.android.webview` 的 `versionName` 是目标版本。
2. `dumpsys webviewupdate` 选中该 provider。
3. **32 位进程真的映射到 32 位引擎**：`maps` 里出现 `webview/lib/arm/libwebviewchromium.so`（只看进程存活不算）。
4. 冒烟：图库缩略图能开、Via 搜索页真实渲染；`tombstone` 无新增。

### 版本上限【实机 2026-11-01】

| 版本 | arm64 页面 | 32 位应用 | 备注 |
| --- | --- | --- | --- |
| 106 | ✅ | ❌ | arm `SIGBUS/BUS_ADRALN`；arm64 `SIGTRAP/TRAP_BRKPT`（Chromium `IMMEDIATE_CRASH`） |
| 105 / 101 / 99 | ✅ | ❌ | 同一条 arm32 `SIGBUS`，不是 106 独有 |
| **98.0.4758.116** | ✅ | **✅** | **本机上限**，现用 |
| 90 | ✅ | ✅ | 可用，功能更旧 |

断崖在 **98/99 之间**：106 的故障点是 `ldm r0!,{r1}` 读到垃圾奇数指针（每次崩溃地址都变），
arm64 两处 pc 都是 `brk #0`。这是构建本身在 API 23 / 3.18 内核上跑不通，不是配置错。

回退：`touch /data/adb/modules/webview98/disable && reboot`（把模块目录名换成实际名字）。

## 二、Flyme 状态栏深色图标（meizuFlags）

**根因**：Flyme 5 的状态栏图标颜色由 `WindowManager.LayoutParams.meizuFlags` 决定，
`MEIZU_FLAG_DARK_STATUS_BAR_ICON = 0x200`，框架自动映射成 `mSystemUiVisibility |= 0x2000`。
Dart/Jetpack 只设 `statusBarColor: Colors.transparent`，从不设图标亮度 → 浅色页面白图标不可见。

**判据**：`dumpsys window` 里目标窗口出现 `meizuFlags=0x200` 且 `vsysui=0x2600`（或 `0x2700`）。

**做法**：在 Activity 的 `onCreate` 调 super 之后注入反射（见 `scripts/patch_statusbar.py`）：

```smali
invoke-static {p0}, L<your/pkg>/<Activity>;->sbarFix(Landroid/app/Activity;)V
```

helper 独立成一个方法，内部 try/catch `Throwable`，取 `meizuFlags` 字段 OR 上
`MEIZU_FLAG_DARK_STATUS_BAR_ICON` 后 `setAttributes`。

两个坑：

- **寄存器上限**：`invoke-*` 只能编码 v0–v15。把 2 参数方法的 `.registers 10` 提到 18 会让 `p1` 变成 v17，
  汇编直接报 `Invalid register: v17`。所以必须用独立 helper，不要在原方法里堆代码。
- **不要用 `setStatusBarColor` 兜底**：Via 上一注入该调用就静默卡死（进程活着、主线程 idle、
  窗口不创建、无 FATAL）。原始 APK、原 dex 重打包、无操作 smali 往返都正常，只有这一个调用致命。

Magisk 模块改不了其他应用窗口的 `LayoutParams`；Flyme 全局「沉浸式状态栏」开关对第三方 App 无效；
Xposed 是唯一模块路线，但本机有开机循环风险。

## 三、Via 浏览器专项

- Via 7.3.4 用 androidx `EdgeToEdge`，在 API 23 上设 `decorView.setSystemUiVisibility(0x700)` +
  `setStatusBarColor(0)`；manifest 无 light-status-bar 标志。
- Via 用第三方证书（无私钥），**只能卸载重装**；`pm uninstall -k` 会留陈旧记录导致
  `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，必须完整 `pm uninstall mark.via`。
- Flyme 下 `adb install` 报 `INSTALL_FAILED_USER_RESTRICTED`，改用
  `adb push` + `su -c 'pm install -r /data/local/tmp/x.apk'`。
- 重装后 uid 会变（本机 10087→10093→10098），恢复数据后要 `chown -R <uid>` + `restorecon -R`。
- 数据备份：先 force-stop 再 root `cp -a` 整个 data 目录拉回；对运行中的 App 用 `tar` 会得到截断包。
- 每次升级都要重打补丁（签名不同，必然卸载重装）。

## 四、API 23 应用重编（Kazumi / PiliPlus 类 Flutter 工程）

- 非 Treble、`ro.treble.enabled` 为空、无 `vendor` / `system_b` → 不能刷 GSI。
- 重编要把工程回移到 API 23 能跑的依赖组合（compileSdk/targetSdk、构建插件、依赖库三层一起降），
  再补 smali locale 补丁；只改 `minSdk` 会装得上但运行崩。
- 产物与补丁结果见仓库外的本地目录（本机为 `~/Downloads/m3x-api23/`），仓库不放二进制。
- 已重编的两款：Kazumi 2.4.2、PiliPlus 2.2.0（arm64-v8a + API 23）。

## 五、VoLTE 与解码（结论）

- VoLTE：`scripts/volte/enable_volte.sh` 打开 IMS 相关开关；判据是 `volte_imcb/stack/ua` running、
  IMS PDN `ccmni4` 为 `CONNECTED + Capabilities: IMS&VALIDATED`、状态栏 `4G+HD`，最后必须**真实通话 + 短信**验证。
- 解码：唯一硬解失败项是 10-bit HEVC，改用 VLC 软解可 30 fps 实时；其余按 `scripts/video/bench_decode.sh` 的矩阵。
- 升级：官方最高 Flyme 7.8.7.24 beta 底层仍是 Android 6.0.1；没有 M3X 的 LineageOS/GSI 路径，
  不要为「升 Android 15」投入编译时间。
