# Sub2API 中转站比价软件打包说明

这个目录用于把 Sub2API 中转站比价软件打包成可分发版本。默认产物是单文件 `.exe`。

## 隐私边界

发行包只包含程序本体和抓取脚本，不包含本机运行数据。以下内容会被排除：

- `output/`
- `price-sites.json`
- `price-latest.json`
- `price-latest.csv`
- `price-history/`
- `price-webview-profile/`
- `chrome-profiles/`
- `edge-profiles/`
- `price-login-profiles/`

打包后的程序运行时会把每个用户自己的数据保存到：

```text
%LOCALAPPDATA%\Sub2APIPriceMonitor
```

因此你本机保存过的站点、登录状态和历史价格不会进入发行版；其他人打开后会从空配置开始使用。

## 构建

在仓库根目录运行：

```powershell
.\tools\packaging\build_price_app.ps1 -Version 0.1.10
```

构建脚本会校验 `APP_VERSION` 与传入版本一致，并把产品名称和版本号写入 Windows EXE 文件属性。版本不一致时会直接停止构建，避免误发旧二进制。

默认生成单文件 exe：

```text
dist/price-webview-app/Sub2APIPriceMonitor-<版本>.exe
```

产物目录：

```text
dist/price-webview-app
```

## 分发建议

只分发 `Sub2APIPriceMonitor-<版本>.exe`。这个文件可直接运行，不需要安装。
