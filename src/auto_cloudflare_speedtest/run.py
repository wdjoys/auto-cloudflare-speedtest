from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = Path("config.toml")


class ExitCode(IntEnum):
    OK = 0
    CONFIG_ERROR = 1
    SPEEDTEST_FAILED = 2
    SUBSCRIPTION_READ_FAILED = 3
    REPLACE_FAILED = 4
    SUBSCRIPTION_UPDATE_FAILED = 5


def _platform_bundle_name(
    system: str | None = None, machine: str | None = None
) -> tuple[str, str]:
    """将 Python 返回的平台名称转换为内置二进制目录命名。"""
    raw_system = (system or platform.system()).strip().lower()
    raw_machine = (machine or platform.machine()).strip().lower()

    system_aliases = {
        "windows": "win",
        "win32": "win",
        "linux": "linux",
        "darwin": "macos",
        "mac": "macos",
        "macos": "macos",
    }
    architecture_aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "i386": "x86",
        "i486": "x86",
        "i586": "x86",
        "i686": "x86",
        "x86": "x86",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7": "armv7",
        "armv7l": "armv7",
    }
    return system_aliases.get(raw_system, raw_system), architecture_aliases.get(
        raw_machine, raw_machine
    )


def _bundled_executable(
    system: str | None = None, machine: str | None = None
) -> Path:
    system_name, architecture = _platform_bundle_name(system, machine)
    filename = "cfst.exe" if system_name == "win" else "cfst"
    return PACKAGE_DIR / "cfst" / f"{system_name}_{architecture}" / filename


def _available_bundles() -> list[str]:
    bundle_root = PACKAGE_DIR / "cfst"
    if not bundle_root.is_dir():
        return []
    return sorted(path.name for path in bundle_root.iterdir() if path.is_dir())


def _default_executable() -> Path:
    bundled = _bundled_executable()
    if bundled.exists():
        return bundled
    installed = shutil.which("cfst") or shutil.which("CloudflareST")
    return Path(installed) if installed else bundled


def _default_ip_file(executable: Path, ipv6: bool = False) -> Path:
    """优先使用所选 CloudflareST 同目录的地址段文件。"""
    filename = "ipv6.txt" if ipv6 else "ip.txt"
    alongside_executable = executable.parent / filename
    if alongside_executable.exists():
        return alongside_executable
    return PACKAGE_DIR / filename


def _ensure_executable_permission(executable: Path) -> bool:
    """在 POSIX 系统中确保内置程序具有当前用户执行权限。"""
    if os.name == "nt" or os.access(executable, os.X_OK):
        return True
    try:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    except OSError as exc:
        print(f"错误: CloudflareST 没有执行权限，且无法自动修复: {exc}")
        print(f"请手动执行: chmod +x {executable}")
        return False
    return os.access(executable, os.X_OK)


def _resolve_path(value: str | Path, base_dir: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法读取配置文件 {path}: {exc}") from exc


def run_cfst_speedtest(
    n: int = 1000,
    tl: int = 210,
    dn: int = 10,
    sl: float = 3,
    p: int = 0,
    executable_path: str | Path | None = None,
    ip_file: str | Path | None = None,
    result_file: str | Path = "result.csv",
    debug: bool = False,
) -> Optional[subprocess.CompletedProcess]:
    """运行 CloudflareST，并将过程输出实时显示在终端。"""
    executable = _resolve_path(executable_path or _default_executable())
    addresses = _resolve_path(ip_file or _default_ip_file(executable))
    result_path = _resolve_path(result_file)

    if not executable.is_file():
        print(f"错误: 未找到 CloudflareST 可执行文件: {executable}")
        system_name, architecture = _platform_bundle_name()
        print(f"当前平台识别为: {system_name}_{architecture}")
        available = _available_bundles()
        if available:
            print(f"项目内已有平台: {', '.join(available)}")
        print("请通过 --executable 或 config.toml 中的 executable 指定正确路径。")
        return None
    if not _ensure_executable_permission(executable):
        return None
    if not addresses.is_file():
        print(f"错误: 未找到 IP 段文件: {addresses}")
        print("请通过 --ip-file 或 config.toml 中的 ip_file 指定正确路径。")
        return None

    result_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        "-f",
        str(addresses),
        "-o",
        str(result_path),
        "-n",
        str(n),
        "-tl",
        str(tl),
        "-dn",
        str(dn),
        "-sl",
        str(sl),
        "-p",
        str(p),
    ]
    if debug:
        command.append("-debug")

    print(f"正在运行 CloudflareST，结果将保存到: {result_path}")
    try:
        previous_result = None
        if result_path.exists():
            stat = result_path.stat()
            previous_result = (stat.st_mtime_ns, stat.st_size)

        completed = subprocess.run(command, check=True, cwd=result_path.parent)
        if not result_path.is_file():
            print("错误: CloudflareST 没有生成结果文件。")
            print(
                f"当前筛选条件为：延迟不高于 {tl} ms、下载速度不低于 {sl} MB/s；"
                "通常是没有 IP 同时满足条件。"
            )
            print("可先降低 --speed-limit，或使用 --debug 查看实际测速结果。")
            return None

        stat = result_path.stat()
        current_result = (stat.st_mtime_ns, stat.st_size)
        if previous_result is not None and current_result == previous_result:
            print(f"错误: CloudflareST 没有更新结果文件，拒绝使用上次残留数据: {result_path}")
            print("可先降低 --speed-limit，或使用 --debug 查看实际测速结果。")
            return None
        return completed
    except FileNotFoundError:
        print(f"错误: 无法执行文件: {executable}")
    except subprocess.CalledProcessError as exc:
        print(f"错误: CloudflareST 执行失败，退出码: {exc.returncode}")
    except OSError as exc:
        print(f"错误: 启动 CloudflareST 失败: {exc}")
    return None


def extract_ips_from_csv(file_path: str | Path = "result.csv") -> list[str]:
    """从 CloudflareST CSV 结果的第一列读取并校验 IP 地址。"""
    path = _resolve_path(file_path)
    ips: list[str] = []
    try:
        with path.open(mode="r", encoding="utf-8-sig", newline="") as csvfile:
            csv_reader = csv.reader(csvfile)
            header = next(csv_reader, None)
            if not header:
                print(f"错误: 结果文件为空: {path}")
                return []
            for line_number, row in enumerate(csv_reader, start=2):
                if not row or not row[0].strip():
                    continue
                candidate = row[0].strip()
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    print(f"警告: 跳过第 {line_number} 行的无效 IP: {candidate}")
                    continue
                ips.append(candidate)
    except OSError as exc:
        print(f"错误: 无法读取结果文件 {path}: {exc}")
        return []

    print(f"从测速结果中读取到 {len(ips)} 个有效 IP。")
    return ips


def _request_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "auto-cloudflare-speedtest/0.2",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_sub_content(url: str, token: str | None = None) -> Optional[dict[str, Any]]:
    """读取远程订阅 JSON。"""
    print(f"正在获取订阅内容: {url}")
    try:
        request = urllib.request.Request(url, headers=_request_headers(token))
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            print("错误: 订阅接口没有返回 JSON 对象。")
            return None
        response_data = data.get("data")
        content = response_data.get("content") if isinstance(response_data, dict) else None
        if not isinstance(content, str):
            print("错误: 响应中缺少字符串字段 data.content。")
            return None
        return data
    except urllib.error.HTTPError as exc:
        print(f"错误: 订阅读取失败，HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        print(f"错误: 无法连接订阅接口: {exc.reason}")
    except json.JSONDecodeError as exc:
        print(f"错误: 订阅接口返回的不是有效 JSON: {exc}")
    except (TypeError, ValueError) as exc:
        print(f"错误: 订阅 URL 或响应格式无效: {exc}")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"错误: 读取订阅内容失败: {exc}")
    return None


def replace_server_ips_with_count(content: str, ips: list[str]) -> tuple[str, int]:
    """按编号标记替换 server，兼容 IPv4、IPv6、域名及单双引号。"""
    replaced = 0
    for index, ip in enumerate(ips, start=1):
        pattern = re.compile(
            rf"(?P<prefix>\bserver\s*:\s*)(?P<quote>[\"']?)(?P<host>[^\s#\"']+)"
            rf"(?P=quote)(?P<suffix>\s*#\s*cloudflare\s+cdn\s+ip\s+{index}\b)",
            re.IGNORECASE,
        )

        def replacer(match: re.Match[str]) -> str:
            return (
                f"{match.group('prefix')}{match.group('quote')}{ip}"
                f"{match.group('quote')}{match.group('suffix')}"
            )

        content, count = pattern.subn(replacer, content, count=1)
        replaced += count
    return content, replaced


def replace_server_ips(content: str, ips: list[str]) -> str:
    """兼容旧调用方式；需要替换数量时使用 replace_server_ips_with_count。"""
    return replace_server_ips_with_count(content, ips)[0]


def update_subscription(
    content: dict[str, Any], url: str, token: str | None = None
) -> bool:
    """通过 PATCH 更新远程订阅。"""
    headers = _request_headers(token)
    headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url=url,
        data=json.dumps(content, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if 200 <= response.status < 300:
                return True
            print(f"错误: 更新订阅失败，HTTP 状态码: {response.status}")
    except urllib.error.HTTPError as exc:
        print(f"错误: 更新订阅失败，HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        print(f"错误: 无法连接订阅接口: {exc.reason}")
    except (TypeError, ValueError) as exc:
        print(f"错误: 订阅 URL 或请求内容无效: {exc}")
    except OSError as exc:
        print(f"错误: 发送更新请求失败: {exc}")
    return False


def _nested(config: dict[str, Any], section: str, key: str, default: Any) -> Any:
    values = config.get(section, {})
    return values.get(key, default) if isinstance(values, dict) else default


def _value(cli_value: Any, config: dict[str, Any], section: str, key: str, default: Any) -> Any:
    return cli_value if cli_value is not None else _nested(config, section, key, default)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-cfst", description="测速并替换订阅中的 Cloudflare CDN IP"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行测速并生成或提交订阅更新")
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser.add_argument("--url", help="订阅 API 地址（也可使用 CFST_SUBSCRIPTION_URL）")
    run_parser.add_argument("--token", help="认证令牌（建议使用 CFST_SUBSCRIPTION_TOKEN）")
    update_mode = run_parser.add_mutually_exclusive_group()
    update_mode.add_argument("--apply", action="store_true", help="将修改提交到远程")
    update_mode.add_argument(
        "--dry-run", action="store_true", help="仅生成本地预览（默认行为）"
    )
    run_parser.add_argument("--executable", help="CloudflareST 可执行文件路径")
    run_parser.add_argument("--ip-file", help="IP 段文件路径")
    run_parser.add_argument("--result", help="测速 CSV 输出路径")
    run_parser.add_argument("--output", help="更新后 YAML 输出路径")
    run_parser.add_argument("--threads", type=int, help="延迟测速线程数")
    run_parser.add_argument("--latency", type=int, help="平均延迟上限（毫秒）")
    run_parser.add_argument("--download-count", type=int, help="下载测速数量")
    run_parser.add_argument("--speed-limit", type=float, help="下载速度下限（MB/s）")
    run_parser.add_argument("--display-count", type=int, help="终端显示的结果数量")
    run_parser.add_argument(
        "--debug",
        action="store_true",
        default=None,
        help="开启 CloudflareST 调试输出；安全起见不能与 --apply 同用",
    )

    check_parser = subparsers.add_parser("check", help="检查配置与本地运行环境")
    check_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    check_parser.add_argument("--executable", help="CloudflareST 可执行文件路径")
    check_parser.add_argument("--ip-file", help="IP 段文件路径")
    return parser


def _validated_positive(name: str, value: int | float, allow_zero: bool = False) -> bool:
    valid = value >= 0 if allow_zero else value > 0
    if not valid:
        print(f"错误: {name} 必须为{'非负数' if allow_zero else '正数'}。")
    return valid


def check_environment(args: argparse.Namespace) -> ExitCode:
    try:
        config_path = _resolve_path(args.config)
        config = load_config(config_path)
    except ValueError as exc:
        print(f"错误: {exc}")
        return ExitCode.CONFIG_ERROR

    base_dir = config_path.parent
    try:
        executable_value = _value(
            args.executable, config, "speedtest", "executable", _default_executable()
        )
        executable = _resolve_path(executable_value, base_dir)
        ip_file_value = _value(
            args.ip_file, config, "speedtest", "ip_file", _default_ip_file(executable)
        )
        ip_file = _resolve_path(ip_file_value, base_dir)
    except (TypeError, ValueError, OSError) as exc:
        print(f"错误: 路径配置无效: {exc}")
        return ExitCode.CONFIG_ERROR
    url = os.getenv("CFST_SUBSCRIPTION_URL") or _nested(config, "subscription", "url", "")

    system_name, architecture = _platform_bundle_name()
    executable_ready = executable.is_file() and (
        os.name == "nt" or os.access(executable, os.X_OK)
    )
    executable_detail = str(executable)
    if executable.is_file() and not executable_ready:
        executable_detail += "（缺少执行权限，运行时将尝试自动修复）"

    checks = [
        ("Python 3.11+", sys.version_info >= (3, 11), sys.version.split()[0]),
        ("运行平台", True, f"{system_name}_{architecture}"),
        ("CloudflareST", executable.is_file(), executable_detail),
        ("IP 段文件", ip_file.is_file(), str(ip_file)),
        ("订阅 URL", bool(url), url or "未配置（运行时可传 --url）"),
    ]
    for label, success, detail in checks:
        print(f"[{'OK' if success else '--'}] {label}: {detail}")
    required_ok = checks[0][1] and checks[2][1] and checks[3][1]
    return ExitCode.OK if required_ok else ExitCode.CONFIG_ERROR


def run_pipeline(args: argparse.Namespace) -> ExitCode:
    try:
        config_path = _resolve_path(args.config)
        config = load_config(config_path)
    except ValueError as exc:
        print(f"错误: {exc}")
        return ExitCode.CONFIG_ERROR

    base_dir = config_path.parent
    url = args.url or os.getenv("CFST_SUBSCRIPTION_URL") or _nested(config, "subscription", "url", "")
    token = args.token or os.getenv("CFST_SUBSCRIPTION_TOKEN") or _nested(config, "subscription", "token", "")
    if not isinstance(url, str) or not url:
        print("错误: 未配置订阅 URL。请复制 config.example.toml 为 config.toml 并填写 url，")
        print("或使用 --url / CFST_SUBSCRIPTION_URL。")
        return ExitCode.CONFIG_ERROR
    if not url.lower().startswith(("http://", "https://")):
        print("错误: 订阅 URL 必须以 http:// 或 https:// 开头。")
        return ExitCode.CONFIG_ERROR
    if token and not isinstance(token, str):
        print("错误: subscription.token 必须是字符串。")
        return ExitCode.CONFIG_ERROR

    try:
        executable = _resolve_path(
            _value(args.executable, config, "speedtest", "executable", _default_executable()),
            base_dir,
        )
        ip_file = _resolve_path(
            _value(
                args.ip_file,
                config,
                "speedtest",
                "ip_file",
                _default_ip_file(executable),
            ),
            base_dir,
        )
        result_file = _resolve_path(
            _value(args.result, config, "output", "result_csv", "result.csv"), base_dir
        )
        output_file = _resolve_path(
            _value(args.output, config, "output", "updated_yaml", "updated_sub.yaml"),
            base_dir,
        )
        threads = int(_value(args.threads, config, "speedtest", "threads", 1000))
        latency = int(_value(args.latency, config, "speedtest", "latency", 210))
        download_count = int(
            _value(args.download_count, config, "speedtest", "download_count", 10)
        )
        speed_limit = float(
            _value(args.speed_limit, config, "speedtest", "speed_limit", 3)
        )
        display_count = int(
            _value(args.display_count, config, "speedtest", "display_count", 0)
        )
        debug_value = _value(args.debug, config, "speedtest", "debug", False)
        if not isinstance(debug_value, bool):
            raise ValueError("speedtest.debug 必须是 true 或 false")
        debug = debug_value
    except (TypeError, ValueError, OSError) as exc:
        print(f"错误: 配置项类型或路径无效: {exc}")
        return ExitCode.CONFIG_ERROR

    validations = [
        _validated_positive("threads", threads),
        _validated_positive("latency", latency),
        _validated_positive("download_count", download_count),
        _validated_positive("speed_limit", speed_limit, allow_zero=True),
        _validated_positive("display_count", display_count, allow_zero=True),
    ]
    if not all(validations):
        return ExitCode.CONFIG_ERROR
    if debug and args.apply:
        print("错误: --debug 可能输出未达到速度门槛的 IP，不能与 --apply 同时使用。")
        print("请先以预览模式诊断，再调整 speed_limit 后正常运行。")
        return ExitCode.CONFIG_ERROR

    subscription = get_sub_content(url, token or None)
    if subscription is None:
        return ExitCode.SUBSCRIPTION_READ_FAILED
    original_content = subscription["data"]["content"]

    speedtest = run_cfst_speedtest(
        n=threads,
        tl=latency,
        dn=download_count,
        sl=speed_limit,
        p=display_count,
        executable_path=executable,
        ip_file=ip_file,
        result_file=result_file,
        debug=debug,
    )
    if speedtest is None or speedtest.returncode != 0:
        return ExitCode.SPEEDTEST_FAILED

    ips = extract_ips_from_csv(result_file)
    if not ips:
        print("错误: 测速没有产生有效 IP，已停止更新。")
        return ExitCode.SPEEDTEST_FAILED

    updated_content, replaced = replace_server_ips_with_count(original_content, ips)
    if replaced == 0:
        print("错误: 未找到 '# cloudflare cdn ip N' 标记，已停止更新。")
        return ExitCode.REPLACE_FAILED
    print(f"已替换 {replaced} 个 server 地址（测速结果共 {len(ips)} 个）。")
    if replaced < len(ips):
        print(f"提示: 有 {len(ips) - replaced} 个 IP 没有对应的编号标记，已忽略。")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_file.write_text(updated_content, encoding="utf-8")
    except OSError as exc:
        print(f"错误: 无法保存预览文件 {output_file}: {exc}")
        return ExitCode.REPLACE_FAILED
    print(f"更新后的完整内容已保存到: {output_file}")

    if not args.apply:
        print("预览模式：没有修改远程订阅。确认文件后使用 --apply 提交。")
        return ExitCode.OK

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = output_file.with_name(f"subscription-backup-{timestamp}.yaml")
    try:
        backup_file.write_text(original_content, encoding="utf-8")
    except OSError as exc:
        print(f"错误: 无法创建备份 {backup_file}: {exc}")
        return ExitCode.REPLACE_FAILED
    print(f"远程更新前的原内容已备份到: {backup_file}")

    subscription["data"]["content"] = updated_content
    if not update_subscription(subscription["data"], url, token or None):
        return ExitCode.SUBSCRIPTION_UPDATE_FAILED
    print("订阅更新成功。")
    return ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check":
        return int(check_environment(args))
    return int(run_pipeline(args))


if __name__ == "__main__":
    raise SystemExit(main())
