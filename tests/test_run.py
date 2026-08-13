import base64
import csv
import io
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from auto_cloudflare_speedtest.run import (
    ExitCode,
    TeeTextIO,
    _bundled_executable,
    _build_parser,
    _cleanup_expired_logs,
    _cleanup_expired_backups,
    _default_ip_file,
    _platform_bundle_name,
    derive_download_url,
    get_download_content,
    get_sub_content,
    extract_ips_from_csv,
    load_config,
    main,
    missing_download_hosts,
    replace_server_ips,
    replace_server_ips_with_details,
    replace_server_ips_with_count,
    run_pipeline,
    update_subscription,
)


class FakeHttpResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = io.BytesIO(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


class RequestRetryTests(unittest.TestCase):
    def test_derives_sub_store_download_url(self) -> None:
        self.assertEqual(
            derive_download_url(
                "https://example.com/key/api/sub/%E8%87%AA%E5%BB%BA?x=1"
            ),
            "https://example.com/key/download/%E8%87%AA%E5%BB%BA?x=1",
        )
        self.assertIsNone(derive_download_url("https://example.com/custom/api"))

    def test_download_verification_bypasses_cache(self) -> None:
        response = FakeHttpResponse(b"server: 8.8.8.8")
        with patch(
            "auto_cloudflare_speedtest.run.urllib.request.urlopen",
            return_value=response,
        ) as urlopen:
            result = get_download_content(
                "https://example.com/download/test?target=Clash", retries=0
            )

        self.assertEqual(result, "server: 8.8.8.8")
        request = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query["noCache"], ["true"])
        self.assertIn("_auto_cfst_verify", query)
        self.assertTrue(request.headers["User-agent"].startswith("Clash/"))

    def test_finds_hosts_in_plain_and_base64_downloads(self) -> None:
        hosts = ["1.1.1.1", "8.8.8.8"]
        self.assertEqual(missing_download_hosts("server: 1.1.1.1", hosts), ["8.8.8.8"])
        encoded = base64.b64encode(b"server: 1.1.1.1\nserver: 8.8.8.8").decode()
        self.assertEqual(missing_download_hosts(encoded, hosts), [])

    def test_update_requires_response_content_to_match(self) -> None:
        response = FakeHttpResponse(b'{"data":{"content":"new"}}')
        with patch(
            "auto_cloudflare_speedtest.run.urllib.request.urlopen",
            return_value=response,
        ):
            result = update_subscription(
                {"content": "new"}, "https://example.com", retries=0
            )

        self.assertEqual(result, {"data": {"content": "new"}})

    def test_update_rejects_mismatched_response_content(self) -> None:
        response = FakeHttpResponse(b'{"data":{"content":"old"}}')
        with patch(
            "auto_cloudflare_speedtest.run.urllib.request.urlopen",
            return_value=response,
        ):
            result = update_subscription(
                {"content": "new"}, "https://example.com", retries=0
            )

        self.assertIsNone(result)

    def test_retries_connection_reset_then_succeeds(self) -> None:
        response = FakeHttpResponse(b'{"data":{"content":"ok"}}')
        error = urllib.error.URLError(ConnectionResetError(104, "reset"))
        with (
            patch(
                "auto_cloudflare_speedtest.run.urllib.request.urlopen",
                side_effect=[error, response],
            ) as urlopen,
            patch("auto_cloudflare_speedtest.run.time.sleep") as sleep,
        ):
            result = get_sub_content(
                "https://example.com", retries=1, retry_delay=2, timeout=10
            )

        self.assertEqual(result, {"data": {"content": "ok"}})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_does_not_retry_non_transient_http_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.com", 401, "Unauthorized", {}, None
        )
        with (
            patch(
                "auto_cloudflare_speedtest.run.urllib.request.urlopen",
                side_effect=error,
            ) as urlopen,
            patch("auto_cloudflare_speedtest.run.time.sleep") as sleep,
        ):
            result = get_sub_content("https://example.com", retries=5, retry_delay=2)

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


class ExecutionLogTests(unittest.TestCase):
    def assert_timestamped_lines(self, content: str, expected_lines: list[str]) -> None:
        lines = content.splitlines()
        self.assertEqual(len(lines), len(expected_lines))
        for line, expected in zip(lines, expected_lines):
            self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
            self.assertTrue(line.endswith(expected), line)

    def test_cleanup_removes_only_expired_generated_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            expired = log_dir / "auto-cfst-old.log"
            recent = log_dir / "auto-cfst-recent.log"
            unrelated = log_dir / "application.log"
            for path in (expired, recent, unrelated):
                path.write_text("log", encoding="utf-8")
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(expired, (old_time, old_time))

            deleted, errors = _cleanup_expired_logs(log_dir, 30)

            self.assertEqual(deleted, 1)
            self.assertEqual(errors, [])
            self.assertFalse(expired.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    def test_zero_retention_disables_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            expired = log_dir / "auto-cfst-old.log"
            expired.write_text("log", encoding="utf-8")
            old_time = time.time() - 365 * 24 * 60 * 60
            os.utime(expired, (old_time, old_time))

            deleted, errors = _cleanup_expired_logs(log_dir, 0)

            self.assertEqual((deleted, errors), (0, []))
            self.assertTrue(expired.exists())

    def test_backup_cleanup_uses_same_retention_and_safe_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory)
            expired = backup_dir / "subscription-backup-20260101-000000.yaml"
            recent = backup_dir / "subscription-backup-20260813-000000.yaml"
            unrelated = backup_dir / "manual-backup.yaml"
            for path in (expired, recent, unrelated):
                path.write_text("content", encoding="utf-8")
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(expired, (old_time, old_time))

            deleted, errors = _cleanup_expired_backups(backup_dir, 30)

            self.assertEqual((deleted, errors), (1, []))
            self.assertFalse(expired.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    def test_carriage_return_progress_keeps_only_last_frame_in_log(self) -> None:
        terminal = io.StringIO()
        log = io.StringIO()
        output = TeeTextIO(terminal, log)

        output.write("开始测速\n")
        output.write("0 / 100 [___] 可用: 0\r")
        output.write("50 / 100 [-__] 可用: 45\r")
        output.write("100 / 100 [---] 可用: 90\n")
        output.finalize()

        self.assertEqual(terminal.getvalue(), "开始测速\n100 / 100 [---] 可用: 90\n")
        self.assert_timestamped_lines(
            log.getvalue(), ["开始测速", "100 / 100 [---] 可用: 90"]
        )

    def test_newline_progress_is_also_collapsed(self) -> None:
        terminal = io.StringIO()
        log = io.StringIO()
        output = TeeTextIO(terminal, log)

        output.write("0 / 100 [___] 可用: 0\n")
        output.write("50 / 100 [-__] 可用: 45\n")
        output.write("100 / 100 [---] 可用: 90\n")
        output.write("开始下载测速\n")
        output.finalize()

        self.assert_timestamped_lines(
            log.getvalue(), ["100 / 100 [---] 可用: 90", "开始下载测速"]
        )

    def test_concatenated_progress_keeps_last_frame(self) -> None:
        terminal = io.StringIO()
        log = io.StringIO()
        output = TeeTextIO(terminal, log)

        output.write("0 / 100 [___] 可用: 0  50 / 100 [-__] 可用: 45\n")
        output.finalize()

        self.assert_timestamped_lines(log.getvalue(), ["50 / 100 [-__] 可用: 45"])

    def test_main_writes_metadata_and_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            log_path = root / "execution.log"
            config_path.write_text("", encoding="utf-8")

            def fake_execute(_args) -> int:
                print("测试命令输出")
                return 0

            with patch(
                "auto_cloudflare_speedtest.run._execute_command",
                side_effect=fake_execute,
            ):
                exit_code = main(
                    [
                        "check",
                        "--config",
                        str(config_path),
                        "--log-file",
                        str(log_path),
                    ]
                )

            content = log_path.read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertIn("测试命令输出", content)
            self.assertIn("开始时间:", content)
            self.assertIn("运行平台:", content)
            self.assertIn("退出码: 0", content)


class PlatformSelectionTests(unittest.TestCase):
    def test_normalizes_common_system_and_architecture_names(self) -> None:
        cases = [
            (("Windows", "AMD64"), ("win", "x86_64")),
            (("Linux", "x86_64"), ("linux", "x86_64")),
            (("Darwin", "arm64"), ("macos", "arm64")),
            (("Linux", "aarch64"), ("linux", "arm64")),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_platform_bundle_name(*raw), expected)

    def test_builds_windows_and_linux_bundle_paths(self) -> None:
        self.assertEqual(
            _bundled_executable("Windows", "AMD64").parts[-3:],
            ("cfst", "win_x86_64", "cfst.exe"),
        )
        self.assertEqual(
            _bundled_executable("Linux", "x86_64").parts[-3:],
            ("cfst", "linux_x86_64", "cfst"),
        )

    def test_ip_file_follows_selected_executable(self) -> None:
        windows_executable = _bundled_executable("Windows", "AMD64")
        self.assertEqual(
            _default_ip_file(windows_executable), windows_executable.parent / "ip.txt"
        )
        self.assertEqual(
            _default_ip_file(windows_executable, ipv6=True),
            windows_executable.parent / "ipv6.txt",
        )


class ReplaceServerIpsTests(unittest.TestCase):
    def test_details_distinguish_matches_from_real_changes(self) -> None:
        content = (
            'server: "1.1.1.1" # cloudflare cdn ip 1\n'
            'server: "2.2.2.2" # cloudflare cdn ip 2\n'
        )
        updated, matched, changes = replace_server_ips_with_details(
            content, ["1.1.1.1", "8.8.8.8"]
        )

        self.assertEqual(matched, 2)
        self.assertEqual(changes, [(2, "2.2.2.2", "8.8.8.8")])
        self.assertIn("8.8.8.8", updated)

    def test_replaces_ipv4_ipv6_and_unquoted_hosts(self) -> None:
        content = """proxies:
  - server: "old.example.com" # cloudflare cdn ip 1
  - server: '1.1.1.1' # cloudflare cdn ip 2
  - server: old.example.net # cloudflare cdn ip 3
"""
        updated, count = replace_server_ips_with_count(
            content, ["8.8.8.8", "2606:4700::1111", "9.9.9.9"]
        )

        self.assertEqual(count, 3)
        self.assertIn('server: "8.8.8.8" # cloudflare cdn ip 1', updated)
        self.assertIn("server: '2606:4700::1111' # cloudflare cdn ip 2", updated)
        self.assertIn("server: 9.9.9.9 # cloudflare cdn ip 3", updated)

    def test_does_not_replace_unmarked_servers(self) -> None:
        content = '- server: "1.1.1.1"\n'
        updated, count = replace_server_ips_with_count(content, ["8.8.8.8"])
        self.assertEqual(count, 0)
        self.assertEqual(updated, content)

    def test_legacy_wrapper_returns_string(self) -> None:
        content = 'server: "1.1.1.1" # cloudflare cdn ip 1'
        self.assertIn("8.8.8.8", replace_server_ips(content, ["8.8.8.8"]))


class CsvTests(unittest.TestCase):
    def test_extracts_valid_ips_and_skips_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.csv"
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(["IP 地址", "延迟"])
                writer.writerow(["1.1.1.1", "10"])
                writer.writerow(["2606:4700::1111", "20"])
                writer.writerow(["not-an-ip", "30"])

            self.assertEqual(
                extract_ips_from_csv(path), ["1.1.1.1", "2606:4700::1111"]
            )


class ConfigTests(unittest.TestCase):
    def test_loads_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[subscription]\nurl = "https://example.com"\n', encoding="utf-8")
            self.assertEqual(
                load_config(path)["subscription"]["url"], "https://example.com"
            )


class PipelineSafetyTests(unittest.TestCase):
    def test_apply_verifies_management_api_and_download_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(
                '[subscription]\nurl = "https://example.com/key/api/sub/test"\n'
                '[output]\nupdated_yaml = "preview.yaml"\nresult_csv = "result.csv"\n',
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                ["run", "--config", str(config_path), "--apply"]
            )
            original = 'server: "1.1.1.1" # cloudflare cdn ip 1\n'
            updated = 'server: "8.8.8.8" # cloudflare cdn ip 1\n'

            with (
                patch(
                    "auto_cloudflare_speedtest.run.get_sub_content",
                    side_effect=[
                        {"data": {"name": "test", "content": original}},
                        {"data": {"name": "test", "content": updated}},
                    ],
                ),
                patch(
                    "auto_cloudflare_speedtest.run.run_cfst_speedtest",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                patch(
                    "auto_cloudflare_speedtest.run.extract_ips_from_csv",
                    return_value=["8.8.8.8"],
                ),
                patch(
                    "auto_cloudflare_speedtest.run.update_subscription",
                    return_value={"data": {"name": "test", "content": updated}},
                ),
                patch(
                    "auto_cloudflare_speedtest.run.get_download_content",
                    return_value="proxies:\n  - server: 8.8.8.8\n",
                ) as download,
            ):
                result = run_pipeline(args)

            self.assertEqual(result, ExitCode.OK)
            self.assertEqual(
                download.call_args.args[0],
                "https://example.com/key/download/test",
            )

    def test_unchanged_content_writes_current_preview_without_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            preview_path = root / "preview.yaml"
            config_path.write_text(
                '[subscription]\nurl = "https://example.com/api"\n'
                '[output]\nupdated_yaml = "preview.yaml"\nresult_csv = "result.csv"\n',
                encoding="utf-8",
            )
            preview_path.write_text("stale", encoding="utf-8")
            args = _build_parser().parse_args(
                ["run", "--config", str(config_path), "--apply"]
            )
            content = 'server: "1.1.1.1" # cloudflare cdn ip 1\n'
            subscription = {"data": {"content": content}}

            with (
                patch(
                    "auto_cloudflare_speedtest.run.get_sub_content",
                    return_value=subscription,
                ),
                patch(
                    "auto_cloudflare_speedtest.run.run_cfst_speedtest",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                patch(
                    "auto_cloudflare_speedtest.run.extract_ips_from_csv",
                    return_value=["1.1.1.1"],
                ),
                patch("auto_cloudflare_speedtest.run.update_subscription") as update,
            ):
                result = run_pipeline(args)

            self.assertEqual(result, ExitCode.OK)
            self.assertEqual(preview_path.read_text(encoding="utf-8"), content)
            update.assert_not_called()

    def test_default_mode_writes_preview_without_remote_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(
                '[subscription]\nurl = "https://example.com/api"\n'
                '[output]\nupdated_yaml = "preview.yaml"\nresult_csv = "result.csv"\n',
                encoding="utf-8",
            )
            args = _build_parser().parse_args(["run", "--config", str(config_path)])
            subscription = {
                "data": {
                    "content": 'server: "1.1.1.1" # cloudflare cdn ip 1\n'
                }
            }

            with (
                patch(
                    "auto_cloudflare_speedtest.run.get_sub_content",
                    return_value=subscription,
                ),
                patch(
                    "auto_cloudflare_speedtest.run.run_cfst_speedtest",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                patch(
                    "auto_cloudflare_speedtest.run.extract_ips_from_csv",
                    return_value=["8.8.8.8"],
                ),
                patch("auto_cloudflare_speedtest.run.update_subscription") as update,
            ):
                result = run_pipeline(args)

            self.assertEqual(result, ExitCode.OK)
            self.assertIn("8.8.8.8", (root / "preview.yaml").read_text(encoding="utf-8"))
            update.assert_not_called()

    def test_debug_mode_cannot_update_remote_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[subscription]\nurl = "https://example.com/api"\n'
                '[speedtest]\ndebug = true\n',
                encoding="utf-8",
            )
            args = _build_parser().parse_args(
                ["run", "--config", str(config_path), "--apply"]
            )
            with patch("auto_cloudflare_speedtest.run.get_sub_content") as fetch:
                result = run_pipeline(args)

            self.assertEqual(result, ExitCode.CONFIG_ERROR)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
