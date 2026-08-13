from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import contextlib
import csv
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional, TextIO


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = Path("config.toml")


class ExitCode(IntEnum):
    OK = 0
    CONFIG_ERROR = 1
    SPEEDTEST_FAILED = 2
    SUBSCRIPTION_READ_FAILED = 3
    REPLACE_FAILED = 4
    SUBSCRIPTION_UPDATE_FAILED = 5


class TeeTextIO:
    """将普通文本双写，并将 CloudflareST 动态进度压缩成最终状态。"""

    ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    PROGRESS_START = re.compile(r"(?<!\d)\d+\s*/\s*\d+\s*\[")

    def __init__(self, terminal: TextIO, log_file: TextIO) -> None:
        self.terminal = terminal
        self.log_file = log_file
        self._pending_line = ""
        self._latest_progress: str | None = None
        self._progress_visible = False

    def write(self, text: str) -> int:
        for character in text:
            if character == "\r":
                self._consume_line(is_carriage_return=True)
            elif character == "\n":
                self._consume_line(is_carriage_return=False)
            elif character == "\b":
                self._pending_line = self._pending_line[:-1]
            else:
                self._pending_line += character
        return len(text)

    def _clean_line(self, line: str) -> str:
        return self.ANSI_ESCAPE.sub("", line).rstrip()

    def _write_log_line(self, line: str) -> None:
        """日志中的非空行增加精确到秒的本地时间，空行原样保留。"""
        if line:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log_file.write(f"[{timestamp}] {line}\n")
        else:
            self.log_file.write("\n")

    def _extract_progress(self, line: str) -> str | None:
        """提取最后一个进度帧，兼容多个帧被拼在同一行的情况。"""
        matches = list(self.PROGRESS_START.finditer(line))
        if not matches:
            return None
        return line[matches[-1].start() :].strip()

    def _render_progress(self, progress: str) -> None:
        if not self.terminal.isatty():
            return
        # \x1b[K 清除旧进度行较长时残留在右侧的字符。
        self.terminal.write(f"\r{progress}\x1b[K")
        self.terminal.flush()
        self._progress_visible = True

    def _commit_progress(self) -> None:
        if self._latest_progress is None:
            return
        if self.terminal.isatty():
            self.terminal.write(f"\r{self._latest_progress}\x1b[K\n")
        else:
            self.terminal.write(f"{self._latest_progress}\n")
        self._write_log_line(self._latest_progress)
        self._latest_progress = None
        self._progress_visible = False

    def _consume_line(self, *, is_carriage_return: bool) -> None:
        clean_line = self._clean_line(self._pending_line)
        self._pending_line = ""

        progress = self._extract_progress(clean_line)
        if progress is not None:
            self._latest_progress = progress
            self._render_progress(progress)
            # CRLF 或测速程序在最终帧后输出换行时，落盘最终进度。
            if not is_carriage_return and clean_line == "":
                self._commit_progress()
            return

        committed_progress = self._latest_progress is not None
        if committed_progress:
            self._commit_progress()

        # 空行只负责结束动态进度时，不额外制造一行。
        if not clean_line and (is_carriage_return or committed_progress):
            return
        self.terminal.write(f"{clean_line}\n")
        self._write_log_line(clean_line)

    def finalize(self) -> None:
        """将最后一行写入日志，避免无换行输出丢失。"""
        if self._pending_line:
            self._consume_line(is_carriage_return=False)
        self._commit_progress()
        self.terminal.flush()
        self.log_file.flush()

    def flush(self) -> None:
        self.terminal.flush()
        # 不提交半行或动态进度；阶段结束或 finalize 时才写入日志。
        self.log_file.flush()

    def isatty(self) -> bool:
        return self.terminal.isatty()

    @property
    def encoding(self) -> str | None:
        return self.terminal.encoding


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

        process = subprocess.Popen(
            command,
            cwd=result_path.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        if process.stdout is not None:
            # 二进制读取避免 TextIOWrapper 将 \r 自动转换成 \n；增量解码避免中文被截断。
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := process.stdout.read(1024):
                decoded = decoder.decode(chunk)
                if decoded:
                    print(decoded, end="", flush=True)
            remaining = decoder.decode(b"", final=True)
            if remaining:
                print(remaining, end="", flush=True)
        return_code = process.wait()
        completed = subprocess.CompletedProcess(command, return_code)
        if return_code != 0:
            print(f"错误: CloudflareST 执行失败，退出码: {return_code}")
            return None
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
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _is_retryable_request_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS
    if isinstance(exc, urllib.error.URLError):
        # 包括 DNS 临时失败、连接重置、拒绝连接和超时。
        return True
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, http.client.HTTPException),
    )


def _request_with_retry(
    request_factory: Any,
    *,
    timeout: float,
    retries: int,
    retry_delay: float,
    operation: str,
) -> tuple[int, bytes]:
    """执行 HTTP 请求，仅针对可能恢复的网络错误进行指数退避重试。"""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request_factory(), timeout=timeout) as response:
                return response.status, response.read()
        except Exception as exc:
            if attempt >= retries or not _is_retryable_request_error(exc):
                raise
            wait_seconds = min(retry_delay * (2**attempt), 15.0)
            print(
                f"警告: {operation}失败: {exc}；{wait_seconds:g} 秒后重试 "
                f"({attempt + 1}/{retries})..."
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
    raise RuntimeError("请求重试流程异常结束")


def get_sub_content(
    url: str,
    token: str | None = None,
    retries: int = 5,
    retry_delay: float = 2,
    timeout: float = 30,
    cache_bust: bool = False,
) -> Optional[dict[str, Any]]:
    """读取远程订阅 JSON。"""
    print(f"正在获取订阅内容: {url}")
    try:
        request_url = url
        if cache_bust:
            separator = "&" if urllib.parse.urlsplit(url).query else "?"
            request_url = f"{url}{separator}_auto_cfst_verify={time.time_ns()}"
        _, response_body = _request_with_retry(
            lambda: urllib.request.Request(
                request_url, headers=_request_headers(token)
            ),
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            operation="获取订阅",
        )
        data = json.loads(response_body.decode("utf-8"))
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


def derive_download_url(api_url: str) -> str | None:
    """从 Sub-Store 的管理 API 地址推导真实订阅下载地址。"""
    parsed = urllib.parse.urlsplit(api_url)
    marker = "/api/sub/"
    if marker not in parsed.path:
        return None
    path = parsed.path.replace(marker, "/download/", 1)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def get_download_content(
    url: str,
    token: str | None = None,
    retries: int = 5,
    retry_delay: float = 2,
    timeout: float = 30,
) -> str | None:
    """绕过 Sub-Store 缓存读取客户端实际使用的订阅内容。"""
    print(f"正在验证订阅下载链接: {url}")
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "_auto_cfst_verify"]
        if not any(key == "noCache" for key, _ in query):
            query.append(("noCache", "true"))
        query.append(("_auto_cfst_verify", str(time.time_ns())))
        request_url = urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(query),
                parsed.fragment,
            )
        )
        headers = _request_headers(token)
        headers["User-Agent"] = "Clash/auto-cloudflare-speedtest"
        headers["Accept"] = "text/plain, application/yaml, application/json"
        _, response_body = _request_with_retry(
            lambda: urllib.request.Request(request_url, headers=headers),
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            operation="验证订阅下载链接",
        )
        return response_body.decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        print(f"错误: 订阅下载链接读取失败，HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        print(f"错误: 无法连接订阅下载链接: {exc.reason}")
    except (TypeError, ValueError) as exc:
        print(f"错误: 订阅下载 URL 无效: {exc}")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"错误: 读取订阅下载内容失败: {exc}")
    return None


def missing_download_hosts(content: str, hosts: list[str]) -> list[str]:
    """返回下载内容中缺少的新地址，并兼容整体 Base64 编码的订阅。"""
    candidates = [content]
    compact = "".join(content.split())
    if compact:
        try:
            padding = "=" * (-len(compact) % 4)
            decoded = base64.b64decode(compact + padding, validate=True).decode("utf-8")
            candidates.append(decoded)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    return [host for host in hosts if not any(host in item for item in candidates)]


def replace_server_ips_with_details(
    content: str, ips: list[str]
) -> tuple[str, int, list[tuple[int, str, str]]]:
    """按编号替换 server，并区分匹配数与真实变化数。"""
    matched = 0
    changes: list[tuple[int, str, str]] = []
    for index, ip in enumerate(ips, start=1):
        pattern = re.compile(
            rf"(?P<prefix>\bserver\s*:\s*)(?P<quote>[\"']?)(?P<host>[^\s#\"']+)"
            rf"(?P=quote)(?P<suffix>\s*#\s*cloudflare\s+cdn\s+ip\s+{index}\b)",
            re.IGNORECASE,
        )

        def replacer(match: re.Match[str]) -> str:
            old_host = match.group("host")
            if old_host != ip:
                changes.append((index, old_host, ip))
            return (
                f"{match.group('prefix')}{match.group('quote')}{ip}"
                f"{match.group('quote')}{match.group('suffix')}"
            )

        content, count = pattern.subn(replacer, content, count=1)
        matched += count
    return content, matched, changes


def replace_server_ips_with_count(content: str, ips: list[str]) -> tuple[str, int]:
    """兼容旧调用方式，返回匹配的标记数量。"""
    updated, matched, _ = replace_server_ips_with_details(content, ips)
    return updated, matched


def replace_server_ips(content: str, ips: list[str]) -> str:
    """兼容旧调用方式；需要替换数量时使用 replace_server_ips_with_count。"""
    return replace_server_ips_with_count(content, ips)[0]


def update_subscription(
    content: dict[str, Any],
    url: str,
    token: str | None = None,
    retries: int = 5,
    retry_delay: float = 2,
    timeout: float = 30,
) -> Optional[dict[str, Any]]:
    """通过 PATCH 更新远程订阅。"""
    headers = _request_headers(token)
    headers["Content-Type"] = "application/json; charset=utf-8"
    data_bytes = json.dumps(content, ensure_ascii=False).encode("utf-8")
    try:
        status, response_body = _request_with_retry(
            lambda: urllib.request.Request(
                url=url,
                data=data_bytes,
                headers=headers,
                method="PATCH",
            ),
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            operation="更新订阅",
        )
        if 200 <= status < 300:
            try:
                response_data = json.loads(response_body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(f"错误: 更新接口返回的不是有效 JSON: {exc}")
                return None
            if not isinstance(response_data, dict):
                print("错误: 更新接口没有返回 JSON 对象。")
                return None
            returned_data = response_data.get("data")
            returned_content = (
                returned_data.get("content")
                if isinstance(returned_data, dict)
                else None
            )
            if returned_content != content.get("content"):
                print("错误: PATCH 响应中的 data.content 与提交内容不一致。")
                return None
            return response_data
        print(f"错误: 更新订阅失败，HTTP 状态码: {status}")
    except urllib.error.HTTPError as exc:
        print(f"错误: 更新订阅失败，HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        print(f"错误: 无法连接订阅接口: {exc.reason}")
    except (TypeError, ValueError) as exc:
        print(f"错误: 订阅 URL 或请求内容无效: {exc}")
    except OSError as exc:
        print(f"错误: 发送更新请求失败: {exc}")
    return None


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


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
    run_parser.add_argument(
        "--download-url",
        help="客户端订阅下载地址；Sub-Store 地址默认可从管理 API 自动推导",
    )
    run_parser.add_argument("--token", help="认证令牌（建议使用 CFST_SUBSCRIPTION_TOKEN）")
    run_parser.add_argument("--retries", type=int, help="临时网络错误的重试次数")
    run_parser.add_argument("--retry-delay", type=float, help="首次重试等待秒数，后续指数增加")
    run_parser.add_argument("--request-timeout", type=float, help="单次 HTTP 请求超时秒数")
    run_parser.add_argument("--log-file", type=Path, help="执行日志文件路径")
    run_parser.add_argument("--no-log", action="store_true", help="本次运行不写日志")
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
    check_parser.add_argument("--log-file", type=Path, help="执行日志文件路径")
    check_parser.add_argument("--no-log", action="store_true", help="本次检查不写日志")
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
    configured_download_url = (
        args.download_url
        or os.getenv("CFST_SUBSCRIPTION_DOWNLOAD_URL")
        or _nested(config, "subscription", "download_url", "")
    )
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
    if configured_download_url and not isinstance(configured_download_url, str):
        print("错误: subscription.download_url 必须是字符串。")
        return ExitCode.CONFIG_ERROR
    if configured_download_url and not configured_download_url.lower().startswith(
        ("http://", "https://")
    ):
        print("错误: 订阅下载 URL 必须以 http:// 或 https:// 开头。")
        return ExitCode.CONFIG_ERROR
    download_url = configured_download_url or derive_download_url(url)

    try:
        retries = int(_value(args.retries, config, "subscription", "retries", 5))
        retry_delay = float(
            _value(args.retry_delay, config, "subscription", "retry_delay", 2)
        )
        request_timeout = float(
            _value(args.request_timeout, config, "subscription", "request_timeout", 30)
        )
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
        backup_dir = _resolve_path(
            _nested(config, "output", "backup_dir", "backups"), base_dir
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
        _validated_positive("retries", retries, allow_zero=True),
        _validated_positive("retry_delay", retry_delay, allow_zero=True),
        _validated_positive("request_timeout", request_timeout),
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

    subscription = get_sub_content(
        url,
        token or None,
        retries=retries,
        retry_delay=retry_delay,
        timeout=request_timeout,
    )
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

    updated_content, matched, changes = replace_server_ips_with_details(
        original_content, ips
    )
    if matched == 0:
        print("错误: 未找到 '# cloudflare cdn ip N' 标记，已停止更新。")
        return ExitCode.REPLACE_FAILED
    print(
        f"匹配 {matched} 个 server 标记，实际变化 {len(changes)} 个"
        f"（测速结果共 {len(ips)} 个）。"
    )
    for index, old_host, new_host in changes:
        print(f"  #{index}: {old_host} -> {new_host}")
    if matched < len(ips):
        print(f"提示: 有 {len(ips) - matched} 个 IP 没有对应的编号标记，已忽略。")
    print(
        f"内容校验: 更新前 sha256={_content_digest(original_content)}，"
        f"更新后 sha256={_content_digest(updated_content)}"
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_file.write_text(updated_content, encoding="utf-8")
    except OSError as exc:
        print(f"错误: 无法保存预览文件 {output_file}: {exc}")
        return ExitCode.REPLACE_FAILED
    print(f"更新后的完整内容已保存到: {output_file}")

    if not changes or updated_content == original_content:
        print("测速 IP 与远程订阅中的 server 地址一致，无需更新；未发送 PATCH。")
        return ExitCode.OK

    if not args.apply:
        print("预览模式：没有修改远程订阅。确认文件后使用 --apply 提交。")
        return ExitCode.OK

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"subscription-backup-{timestamp}.yaml"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file.write_text(original_content, encoding="utf-8")
    except OSError as exc:
        print(f"错误: 无法创建备份 {backup_file}: {exc}")
        return ExitCode.REPLACE_FAILED
    print(f"远程更新前的原内容已备份到: {backup_file}")

    subscription["data"]["content"] = updated_content
    patch_response = update_subscription(
        subscription["data"],
        url,
        token or None,
        retries=retries,
        retry_delay=retry_delay,
        timeout=request_timeout,
    )
    if patch_response is None:
        return ExitCode.SUBSCRIPTION_UPDATE_FAILED

    print("PATCH 响应校验通过，正在重新读取远程订阅进行最终验证...")
    verified_subscription = get_sub_content(
        url,
        token or None,
        retries=retries,
        retry_delay=retry_delay,
        timeout=request_timeout,
        cache_bust=True,
    )
    if verified_subscription is None:
        print("错误: PATCH 后无法重新读取订阅，不能确认远程更新是否生效。")
        return ExitCode.SUBSCRIPTION_UPDATE_FAILED
    remote_content = verified_subscription["data"]["content"]
    if remote_content != updated_content:
        print("错误: PATCH 返回成功，但重新 GET 后远程 data.content 与提交内容不一致。")
        print(
            f"提交 sha256={_content_digest(updated_content)}，"
            f"远程 sha256={_content_digest(remote_content)}"
        )
        return ExitCode.SUBSCRIPTION_UPDATE_FAILED
    print(f"管理接口更新成功并已验证，远程 sha256={_content_digest(remote_content)}。")
    if download_url:
        downloaded_content = get_download_content(
            download_url,
            token or None,
            retries=retries,
            retry_delay=retry_delay,
            timeout=request_timeout,
        )
        if downloaded_content is None:
            print("错误: 管理接口已更新，但无法验证客户端订阅下载链接。")
            return ExitCode.SUBSCRIPTION_UPDATE_FAILED
        expected_hosts = [new_host for _, _, new_host in changes]
        missing_hosts = missing_download_hosts(downloaded_content, expected_hosts)
        if missing_hosts:
            print("错误: 管理接口已更新，但订阅下载内容缺少以下新地址：")
            for host in missing_hosts:
                print(f"  - {host}")
            return ExitCode.SUBSCRIPTION_UPDATE_FAILED
        print(
            f"订阅下载链接验证通过，{len(expected_hosts)} 个新地址均已生效，"
            f"下载内容 sha256={_content_digest(downloaded_content)}。"
        )
    else:
        print("提示: 无法从管理 API 推导下载链接，已跳过客户端订阅内容校验。")
        print("可配置 subscription.download_url 或使用 --download-url 启用校验。")
    return ExitCode.OK


def _execute_command(args: argparse.Namespace) -> int:
    if args.command == "check":
        return int(check_environment(args))
    return int(run_pipeline(args))


def _storage_settings(args: argparse.Namespace) -> tuple[bool, Path, Path, Path, int]:
    """读取日志、备份目录和共同保留期。"""
    config_path = _resolve_path(args.config)
    try:
        config = load_config(config_path)
    except ValueError:
        config = {}
    enabled_value = _nested(config, "output", "log_enabled", True)
    enabled = enabled_value if isinstance(enabled_value, bool) else True
    if args.no_log:
        enabled = False

    log_dir_value = _nested(config, "output", "log_dir", "logs")
    try:
        log_dir = _resolve_path(log_dir_value, config_path.parent)
    except (TypeError, ValueError, OSError):
        log_dir = _resolve_path("logs", config_path.parent)

    backup_dir_value = _nested(config, "output", "backup_dir", "backups")
    try:
        backup_dir = _resolve_path(backup_dir_value, config_path.parent)
    except (TypeError, ValueError, OSError):
        backup_dir = _resolve_path("backups", config_path.parent)

    retention_value = _nested(config, "output", "log_retention_days", 30)
    try:
        retention_days = max(0, int(retention_value))
    except (TypeError, ValueError):
        retention_days = 30

    if args.log_file is not None:
        log_path = _resolve_path(args.log_file, config_path.parent)
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log_path = log_dir / f"auto-cfst-{timestamp}.log"
    return enabled, log_path, log_dir, backup_dir, retention_days


def _cleanup_expired_files(
    directory: Path, pattern: str, retention_days: int
) -> tuple[int, list[str]]:
    """按修改时间清理指定目录和文件名模式，不递归、不触碰其他文件。"""
    if retention_days <= 0 or not directory.is_dir():
        return 0, []

    cutoff = time.time() - retention_days * 24 * 60 * 60
    deleted = 0
    errors: list[str] = []
    try:
        candidates = list(directory.glob(pattern))
    except OSError as exc:
        return 0, [f"无法扫描目录 {directory}: {exc}"]

    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError as exc:
            errors.append(f"无法删除过期日志 {path}: {exc}")
    return deleted, errors


def _cleanup_expired_logs(log_dir: Path, retention_days: int) -> tuple[int, list[str]]:
    """兼容旧调用：清理本程序生成的日志。"""
    return _cleanup_expired_files(log_dir, "auto-cfst-*.log", retention_days)


def _cleanup_expired_backups(
    backup_dir: Path, retention_days: int
) -> tuple[int, list[str]]:
    """清理本程序生成的订阅备份。"""
    return _cleanup_expired_files(
        backup_dir, "subscription-backup-*.yaml", retention_days
    )


def _print_cleanup_result(
    retention_days: int,
    deleted_logs: int,
    deleted_backups: int,
    errors: list[str],
) -> None:
    if retention_days > 0:
        print(
            f"文件保留期: {retention_days} 天，本次清理 {deleted_logs} 个过期日志、"
            f"{deleted_backups} 个过期订阅备份。"
        )
    else:
        print("日志和订阅备份自动清理: 已关闭。")
    for error in errors:
        print(f"警告: {error}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    enabled, log_path, log_dir, backup_dir, retention_days = _storage_settings(args)
    deleted_logs, log_cleanup_errors = _cleanup_expired_logs(
        log_dir, retention_days
    )
    deleted_backups, backup_cleanup_errors = _cleanup_expired_backups(
        backup_dir, retention_days
    )
    cleanup_errors = log_cleanup_errors + backup_cleanup_errors
    if not enabled:
        _print_cleanup_result(
            retention_days, deleted_logs, deleted_backups, cleanup_errors
        )
        return _execute_command(args)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"警告: 无法创建日志文件 {log_path}: {exc}")
        return _execute_command(args)

    started_at = datetime.now()
    with log_file:
        stdout = TeeTextIO(sys.stdout, log_file)
        stderr = TeeTextIO(sys.stderr, log_file)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            print(f"日志文件: {log_path}")
            print(f"开始时间: {started_at.isoformat(timespec='seconds')}")
            print(f"运行平台: {platform.system()} {platform.machine()}")
            _print_cleanup_result(
                retention_days, deleted_logs, deleted_backups, cleanup_errors
            )
            try:
                exit_code = _execute_command(args)
            except KeyboardInterrupt:
                print("\n执行被用户中断。")
                exit_code = 130
            # 避免子进程最后一行无换行时与结束元数据粘连。
            stdout.finalize()
            stderr.finalize()
            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()
            print(f"结束时间: {finished_at.isoformat(timespec='seconds')}")
            print(f"退出码: {exit_code}，耗时: {elapsed:.1f} 秒")
            stdout.finalize()
            stderr.finalize()
            return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
