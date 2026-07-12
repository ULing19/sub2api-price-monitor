# Sub2API 中转站比价软件

这是一个面向 Sub2API 类中转站的桌面比价工具。你可以把多个中转站保存到软件里，使用内置 WebView 完成登录，然后自动拉取各站点的套餐价格、站内分组和倍率，集中按 `OpenAI`、`Anthropic`、`Gemini`、`Grok` 四类模型横向比较。

它适合用来回答一个很实际的问题：同一个模型分类下，当前哪个中转站、哪个分组的倍率最低。

## 主要能力

- 多站点保存：每个中转站可单独保存站点地址、API 路径、备注和检查间隔。
- WebView 登录：在软件内打开目标站点，手动完成登录或安全验证，后续复用本机 WebView 会话。
- 安全保存密码：按站点完整 URL 分开保存到当前 Windows 用户的凭据管理器，重新授权时自动填充。
- 自动比价：登录成功后自动抓取价格；每次打开软件也会自动检查已保存站点。
- 当前快照：每次更新都会重建对应站点的当前价格，只展示本次结果，不把历史行混入列表。
- 启动不卡顿：启动自动检查会进入后台任务，最多同时抓取 2 个站点，控制台仍可筛选、登录和查看结果。
- 关闭即退出：关闭主窗口会取消正在进行的检查、销毁所有辅助 WebView，并清理只属于本软件配置目录的 WebView2 残留进程。
- 防损坏写入：站点配置和最新价格使用原子写入，异常退出时不会留下半个 JSON 文件。
- 本地诊断日志：窗口版 EXE 的启动、退出和严重错误会写入轮转日志，便于排查且不会进入发行包。
- 重新授权检测：自动更新遇到登录态过期、401/403 或授权失效时，逐个打开对应站点的 WebView 重新登录。
- 失败原因诊断：区分登录失效、Cloudflare 验证、请求超时、HTTP 错误、接口响应不兼容和无价格数据。
- 分组排序：`全部` 页默认按价格升序，四个模型分页按倍率升序。
- 条件筛选：支持搜索、站点、类型、最高倍率和“只看有价格/倍率”。
- 隐私隔离：发行版不包含你的保存站点、登录状态、价格历史或 WebView 缓存。

## 快速使用

从 [Releases](https://github.com/ULing19/sub2api-price-monitor/releases) 下载 `Sub2APIPriceMonitor-*.exe` 后直接运行，不需要安装。

也可以在源码目录启动：

```powershell
.\tools\run_price_login_app.ps1
```

启动时带一个站点：

```powershell
.\tools\run_price_login_app.ps1 https://sub.example.com
```

## 图文讲解

### 1. 多站点总览

<img src="docs/images/sub2api-overview.png" alt="Sub2API 中转站比价总览" width="920">

主界面采用左侧工作区导航，价格、站点授权和运行日志各自独立。顶部可直接切换 `OpenAI`、`Anthropic`、`Gemini`、`Grok`，统计区展示当前快照中的套餐、分组和模型分类数量。`全部` 页按价格升序展示，模型分类页按倍率升序展示。

### 2. 站点配置、WebView 登录和自动抓取

<img src="docs/images/sub2api-webview-login.png" alt="WebView 登录后自动抓取价格" width="920">

在 `站点与授权` 页面保存网址、备注、API 路径和更新间隔，再点击 `打开 WebView 登录`。你可以在站点窗口里正常登录、完成安全验证或进入控制台；软件不绕过验证，只复用已经登录成功的 WebView Cookie/session。登录态可用后，软件会自动调用 Sub2API 接口抓取当前价格，并把 WebView 窗口收起。

登录页只有一个明确的当前密码框时，软件会自动保存你填写的用户名和密码。再次打开同一站点登录页时会自动填充；站点开启 `自动登录`、提交按钮唯一且页面没有验证码、OTP 或未勾选的必选条款时，软件会自动提交。修改密码、注册确认等多个密码框页面不会自动填充，避免凭据混淆。

### 3. 模型分页按倍率找低价分组

<img src="docs/images/sub2api-openai-ranking.png" alt="OpenAI 分页按倍率升序比价" width="920">

进入 `OpenAI`、`Anthropic`、`Gemini` 或 `Grok` 分页后，软件会把不同中转站的站内分组摊平成候选项，并按倍率数值升序排列。表格里同时展示站点、模型分类、站内分组、平台、套餐说明、价格和倍率，用来快速定位当前最便宜的可用分组。

### 4. 条件筛选

<img src="docs/images/sub2api-filtering.png" alt="按搜索、站点、类型和最高倍率筛选" width="920">

当保存站点较多时，可以用筛选栏缩小范围。例如进入 `Anthropic` 页，搜索 `Claude`，类型选择 `分组`，并限制最高倍率不超过 `0.20`。筛选结果仍然保持倍率升序，适合快速比较 Claude、Gemini、Grok 等分类下的低倍率候选。

## 推荐流程

1. 输入中转站地址，例如 `https://sub.example.com`。
2. 根据目标站点调整 API 路径，默认是 `/api/v1`。
3. 点击 `打开 WebView 登录`，在弹出的站点窗口中完成登录。
4. 登录后等待自动抓取，或手动点击 `抓取当前价格`。
5. 填写或修改备注，点击 `保存站点`。
6. 对其他中转站重复以上步骤，然后在模型分页里比较倍率。

## 重新授权

自动更新全部站点或后台检查时，如果软件检测到某个站点返回 401/403、登录态过期、token/session 失效，或页面已经跳回登录界面，会把该站点标记为“需重新授权”。软件随后会逐个打开对应站点的 WebView，等待你重新登录；授权恢复并成功抓取价格后，窗口会自动收起，再继续处理下一个失效站点。

登录失效、Cloudflare 验证、未发现价格、HTTP 错误或接口响应不兼容会进入重新授权流程，便于人工打开 WebView 检查登录态、验证页或站点控制台。普通网络超时、断网或登录窗口被手动关闭只会记录为抓取失败，不会反复弹出登录窗口。

如果目标站点停在 Cloudflare 安全验证页，软件会标记为 `Cloudflare 验证未完成` 并暂停自动抓取，保留当前 WebView 等待人工验证。请在 WebView 中手动完成验证，再点击 `WebView抓取` 继续；验证无法通过时，需要检查本机网络、防火墙或站点对 WebView 的兼容性。

## 登录密码

每个站点都可以分别开启 `保存密码` 和 `自动登录`。用户名和密码按完整站点 URL 独立保存到 Windows 凭据管理器，站点记录中只显示“已保存密码”，不会写入 `price-sites.json`、价格历史、WebView 缓存或发行版。跨域登录跳转不会自动填充原站点密码。

需要移除某个站点的密码时，先选择该站点，再点击 `清除密码`。取消 `保存密码` 并保存站点，或直接删除站点，也会同步删除该站点凭据。

自动登录失败后会进入冷却时间，不会每隔几秒反复提交旧密码。遇到 Cloudflare、人机验证、验证码、OTP、跨域登录或表单结构不明确时，软件只保留可确认的自动填充，等待你人工完成验证。

## 工作方式

软件会优先请求：

```text
/api/v1/groups/available
```

然后抓取套餐接口：

```text
/api/v1/payment/checkout-info
/api/v1/payment/plans
```

套餐会优先通过 `group_id`、`group_name`、`group_platform`、`platform`、`provider` 等字段匹配站内分组。模型分类会尽量根据分组信息归入 `OpenAI`、`Anthropic`、`Gemini`、`Grok`，匹配不到时才退回到套餐名称、描述或模型列表做弱匹配。

当前价格列表是最新快照，不是历史记录。每次更新会先移除本次刷新站点的旧行，再写入这次实际返回的套餐、分组或错误结果；已经消失的旧套餐和旧分组不会继续残留。后台只刷新到期站点时，未到期站点的当前快照会继续保留。历史快照仍单独写入 `price-history/`，不会重新混入当前列表。

## 本地数据

源码运行时数据保存到：

```text
output/
```

单文件 exe 运行时数据保存到：

```text
%LOCALAPPDATA%/Sub2APIPriceMonitor
```

主要文件包括：

```text
price-sites.json
price-latest.json
price-latest.csv
price-history/
price-webview-profile/
logs/app.log
```

关闭主窗口后，软件会停止自动检查和未完成的抓取任务，然后关闭登录窗口与后台 WebView。它只会清理使用本软件专用 `price-webview-profile` 的 WebView2 进程，不会影响普通 Edge 或其他软件的 WebView。

诊断日志最多保留 4 个约 1 MB 的本地文件，并会隐藏常见 Bearer Token 和 `sk-` 密钥格式。日志、站点配置、Cookie、密码和价格历史都只保存在当前用户电脑上。

## 隐私边界

发行版只包含程序本体和抓取脚本，不会包含本机的 `output/`、保存站点、价格历史或 WebView 登录缓存。其他人运行 exe 后会从空配置开始，自己的站点和登录状态会保存到自己的本机目录。

Windows 凭据管理器中的密码属于当前 Windows 用户，也不会进入 exe 或 GitHub 仓库。

如果目标站点出现人机验证，请在 WebView 窗口中手动完成。这个软件不会绕过验证，也不会替你破解目标站点限制。

## 打包

生成单文件 exe：

```powershell
.\tools\packaging\build_price_app.ps1
```

产物位置：

```text
dist/price-webview-app/Sub2APIPriceMonitor-<版本>.exe
```
