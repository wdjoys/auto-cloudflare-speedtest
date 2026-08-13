# auto-cloudflare-speedtest

运行 CloudflareST，从测速结果中选择 IP，并替换订阅内容里带编号标记的
Cloudflare CDN 节点。工具默认只生成本地预览，只有明确使用 `--apply` 才会更新远程订阅。

## 环境要求

- Windows x86_64 或 Linux x86_64；程序会自动选择项目内对应的 CloudflareST
- Python 3.11 或更高版本
- 推荐使用 [uv](https://docs.astral.sh/uv/)

## 快速开始

```powershell
uv sync
Copy-Item config.example.toml config.toml
```

编辑 `config.toml`，填写订阅 API 的 `url`，然后检查环境：

```powershell
uv run auto-cfst check
```

第一次运行请保持预览模式：

```powershell
uv run auto-cfst run
```

也可以显式写成 `uv run auto-cfst run --dry-run`，效果相同。

运行成功后，检查生成的 `updated_sub.yaml`。确认内容正确再提交：

```powershell
uv run auto-cfst run --apply
```

使用 `--apply` 时，程序会先在输出文件旁生成带时间戳的原订阅备份。
提交前会列出每个实际变化的 `旧 server -> 新 server` 并显示内容哈希；PATCH 后既校验
接口响应，又重新 GET 远程订阅逐字确认，最后绕过缓存读取客户端实际使用的下载链接，
检查每个新 IP 是否出现。三步全部通过才会报告更新成功。如果测速 IP 与原 server 全部
相同，则明确提示无需更新。

不安装命令入口也可以这样运行：

```powershell
uv run python -m auto_cloudflare_speedtest run
```

## 订阅内容标记

只会替换带有以下编号注释的 `server`。编号必须从 1 开始，并与测速结果顺序对应：

```yaml
- server: "1.1.1.1" # cloudflare cdn ip 1
- server: "2606:4700::1111" # cloudflare cdn ip 2
```

IPv4、IPv6、域名、单双引号和无引号写法均受支持。如果一个标记都没有匹配到，
程序会停止，不会发起远程更新。

## 配置方式

完整配置见 `config.example.toml`。命令行参数优先级高于配置文件：

```powershell
uv run auto-cfst run `
  --url "https://example.com/api/subscription" `
  --download-url "https://example.com/download/subscription" `
  --threads 500 `
  --latency 180 `
  --download-count 5 `
  --speed-limit 5
```

敏感信息推荐放在环境变量中：

```powershell
$env:CFST_SUBSCRIPTION_URL = "https://example.com/api/subscription"
$env:CFST_SUBSCRIPTION_DOWNLOAD_URL = "https://example.com/download/subscription"
$env:CFST_SUBSCRIPTION_TOKEN = "your-token"
uv run auto-cfst run
```

查看全部参数：

```powershell
uv run auto-cfst run --help
```

常用参数：

- `--config`：配置文件路径，默认 `config.toml`
- `--download-url`：客户端实际使用的订阅链接，用于提交后的最终验证
- `--executable`：CloudflareST 可执行文件路径
- `--ip-file`：IPv4 或 IPv6 地址段文件路径
- `--result`：测速 CSV 路径
- `--output`：更新后 YAML 路径
- `--retries`：连接重置、超时、429 和 5xx 等临时错误的重试次数
- `--retry-delay`：首次重试等待秒数，后续按指数退避
- `--request-timeout`：单次订阅读取或更新请求的超时时间
- `--log-file`：将本次执行日志写入指定文件
- `--no-log`：本次不写执行日志
- `--debug`：显示 CloudflareST 下载失败原因，仅限预览模式
- `--apply`：提交远程更新；未指定时只生成本地预览

## 常见问题

**提示找不到 CloudflareST**

运行 `auto-cfst check` 查看识别到的平台和程序路径。内置二进制采用
`cfst/<平台>_<架构>/cfst[.exe]` 目录格式，例如 `win_x86_64`、`linux_x86_64`。
默认 IP 段文件也从该平台目录读取。如果当前平台尚未内置，可通过 `--executable`
和 `--ip-file` 或配置文件指定外部文件。

**想测试 IPv6**

使用 `--ip-file` 指定当前平台目录下的 `ipv6.txt`，例如 Windows x86_64：
`--ip-file src/auto_cloudflare_speedtest/cfst/win_x86_64/ipv6.txt`，也可以写入配置文件。

**生成了 CSV，但没有更新**

检查订阅内容是否包含 `# cloudflare cdn ip 1` 这类连续编号标记。程序默认不会更新远程；
确认预览后还需显式添加 `--apply`。

**下载测速显示 `0 / 10`，并且没有生成 CSV**

说明没有 IP 达到当前的下载速度下限。先使用配置中的 `0.01 MB/s` 再试；如仍无结果，
运行 `uv run auto-cfst run --debug` 查看 CloudflareST 给出的下载失败原因。调试模式只用于
诊断，不能与 `--apply` 一起使用。

**出现 `Connection reset by peer`**

这是订阅服务器或中间网络临时断开连接。程序默认会自动重试 5 次，等待时间依次为
2、4、8、15、15 秒；HTTP 4xx 配置或权限错误不会盲目重试。可在配置文件中调整
`retries`、`retry_delay` 和 `request_timeout`。

**管理接口显示成功，但客户端订阅看起来没有变化**

对于 Sub-Store，程序会把 `/api/sub/订阅名` 自动转换成 `/download/订阅名`，追加
`noCache=true` 后检查本次产生的全部新 IP。若客户端使用分享链接、组合订阅或带
`target` 参数的链接，请在 `subscription.download_url` 中填写客户端的完整地址。

## 执行日志

日志默认开启，每次运行会在 `logs/` 中生成独立的带时间戳文件，同时保留终端实时输出。
日志包括网络重试、测速进度、替换结果、退出码和耗时，不会写入订阅正文或认证令牌。
日志文件中的每条非空记录都带有 `[YYYY-MM-DD HH:MM:SS]` 本地时间前缀，终端输出
保持简洁，不额外添加时间。
CloudflareST 使用回车覆盖同一行刷新进度；日志会压缩这些动态帧，只保留每个阶段换行前的
最终进度，避免生成一整行重复进度条。

默认保留最近 30 天日志。每次启动时只会删除 `log_dir` 中修改时间超过 30 天且名称匹配
`auto-cfst-*.log` 的文件，不会清理其他文件。可通过 `log_retention_days` 修改保留天数，
设为 `0` 可关闭自动清理。

远程更新前的原订阅统一保存在 `backups/`，文件名为
`subscription-backup-YYYYMMDD-HHMMSS.yaml`。备份与日志共用 `log_retention_days` 保留期；
清理仅匹配 `subscription-backup-*.yaml`，不会删除备份目录中的其他文件。即使使用
`--no-log` 关闭本次日志，备份清理仍会执行。

```bash
# 指定日志文件
python3 src/auto_cloudflare_speedtest/run.py run --log-file /var/log/auto-cfst.log

# 本次关闭日志
python3 src/auto_cloudflare_speedtest/run.py run --no-log
```

## 开发与测试

```powershell
uv run python -m unittest discover -v
```

CloudflareST 是独立的第三方程序；分发或升级内置二进制文件时，请同时核对其来源、版本和许可证。
