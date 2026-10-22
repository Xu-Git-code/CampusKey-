#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园网后台静默登录脚本（requests 版，全程不打开浏览器）。

设计目标
--------
1. 纯后台：只用 requests 发 HTTP 请求，不启动浏览器、不模拟点击。
2. 先探测：启动先判断“现在是否已经能上网”，已登录直接退出，避免把在线会话踢掉。
3. 易配置：把 F12 里 “Copy as cURL” 的内容整段粘到 login_request.txt 即可，
   脚本自动解析出 URL / 请求头 / 表单参数。
4. 好排查：失败时给出具体原因（DNS、端口、证书、401、密码错误、Cookie 过期……），
   并把最后一次请求和响应写到 logs/ 目录。

快速使用
--------
    python campus_login.py --dry-run   # 只解析配置，检查抓包内容对不对（不联网）
    python campus_login.py --check     # 只检测当前是否已经联网
    python campus_login.py             # 正式执行：检测 -> 未登录则登录 -> 复检

退出码
------
    0   已在线，或登录成功
    10  配置 / 请求文件有误
    11  网络不可达（没连校园网、门户地址访问不到）
    12  请求发送失败（超时、连接被拒绝、证书错误等）
    13  登录被拒绝或结果失败（密码错误、验证码、参数不对等）
    14  未预期的异常
"""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import logging
import re
import socket
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit

APP_NAME = "校园网自动登录"
APP_VERSION = "1.0.0"

EXIT_OK = 0
EXIT_CONFIG = 10
EXIT_NETWORK = 11
EXIT_REQUEST = 12
EXIT_AUTH = 13
EXIT_UNKNOWN = 14

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_SUCCESS_KEYWORDS = ("success", "成功", "已登录", "认证成功", "已经在线")
DEFAULT_FAILURE_KEYWORDS = (
    "fail", "失败", "错误", "error", "密码不正确", "密码错误",
    "验证码", "欠费", "停机", "用户不存在", "账号不存在",
)
ALREADY_ONLINE_KEYWORDS = ("已在线", "已经在线", "已在线上", "重复登录", "already online")

STATE_TEXT = {
    "online": "已联网",
    "portal": "被门户拦截（未登录）",
    "offline": "未登录",
    "no-network": "网络不可达",
}

log = logging.getLogger("campus")


# ==========================================================================
# 一、内置模板：第一次运行会自动生成 config.ini 和 login_request.txt
# ==========================================================================

DEFAULT_CONFIG = r"""; ================================================================
;  校园网自动登录 - 配置文件
;  改完保存即可。以 ; 开头的内容是注释，不影响运行。
;  第一次使用请先看 README.md 的“3 分钟上手”。
; ================================================================

[general]
; 启动后先等几秒再干活。开机自启建议 20~45，等网卡 / WiFi 就绪。
startup_delay = 0

; 等网络就绪的最长时间（秒）。开机时 WiFi 连得慢也没关系，
; 脚本会一直等到能连上门户，最多等这么久。
network_wait = 180

; 登录请求最多尝试几次。只对“网络错误”重试；
; 密码错误这类明确失败不会重试（防止连续输错把账号锁了）。
retry_times = 2

; 每次重试之间等待多少秒
retry_interval = 8

; 单个 HTTP 请求超时（秒）
http_timeout = 10

; 学校门户是 https 且证书不被信任时，改成 false
verify_ssl = true

; 是否走系统代理。校园网门户一般在局域网，建议 false。
; 如果你开了 Clash / 加速器导致登录失败，也保持 false。
use_system_proxy = false

; 日志级别：DEBUG / INFO / WARNING
log_level = INFO

; 是否把最后一次请求/响应写到 logs 目录，排查问题用
dump = true

[check]
; “是否已经联网”的检测地址：返回 204 表示已经能上网
url = http://connect.rom.miui.com/generate_204

; 判定已联网的状态码，多个用逗号分隔
expect_status = 204

; 返回内容里必须包含的文字（留空表示不检查）
expect_contains =

; 备用检测地址：第一个地址请求失败时再试这个。
; 百度返回 200 且页面里有“百度”二字，就认为通网了。
fallback_url = http://www.baidu.com
fallback_contains = 百度

; 检测超时 / 尝试次数 / 每次间隔（秒）
timeout = 6
attempts = 3
interval = 3

[login]
; 抓包内容放在这个文件里（与 config.ini 同目录）
request_file = login_request.txt

; 账号密码。请求文件里写 {username} / {password} 会被替换成这里的值。
username = 你的学号
password = 你的密码

; 登录前先 GET 一下门户页，拿 Cookie（多数 ePortal / 深澜类门户需要）
warmup = true
; 留空 = 自动使用请求头里的 Referer，或接口地址的站点根路径
warmup_url =

; 是否跟随 302 跳转。一般 false，方便看到服务器真实返回。
allow_redirects = false

; 判断成功 / 失败的关键字（逗号分隔，不区分大小写）。留空则用内置默认值。
success_keyword = success,成功,已登录,认证成功
failure_keyword = fail,失败,错误,error,密码不正确,密码错误,验证码,欠费,停机

; 登录后等几秒再复检网络（有些学校认证生效有延迟）
verify_delay = 2
"""

DEFAULT_REQUEST = r"""# ================================================================
#  登录请求抓包结果 —— 把浏览器 F12 复制的内容粘贴到这个文件
#
#  推荐步骤（详见 README.md 的“抓包教学”）：
#   1. 浏览器按 F12 -> Network(网络) 面板 -> 勾选 Preserve log / Disable cache
#   2. 在校园网登录页输入账号密码，点登录
#   3. 在请求列表里找到那个 POST 请求（名字通常是 login / InterFace.do / ...）
#   4. 右键该请求 -> Copy(复制) -> Copy as cURL (bash)
#   5. 把复制到的一大段内容整段粘贴到本文件末尾
#
#  粘贴后建议做两处替换（以后改密码、IP 变了都不用重新抓包）：
#     密码的值 -> {password}             例：password={password_urlencoded}
#     账号的值 -> {username}             例：userId={username}
#     本机 IP  -> {local_ip}             例：wlan_user_ip={local_ip}
#  可用占位符：
#     {username} {password} {password_urlencoded} {username_urlencoded}
#     {local_ip} {local_mac} {hostname} {timestamp} {timestamp_ms} {date} {time}
#
#  支持三种格式（任选其一）：
#     A. cURL（推荐：Chrome/Edge 用 “Copy as cURL (bash)” 或 “(cmd)”）
#     B. 原始 HTTP 请求（请求行 + 请求头 + 空行 + 请求体）
#     C. 手动格式（*url / *method / 请求头 / [body]）
# ================================================================

# ------- 示例 A：cURL（下面是注释，粘贴你的内容后这些可以留着）-------
# curl 'http://10.10.10.10/eportal/InterFace.do?method=login' \
#   -H 'Accept: */*' \
#   -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
#   -H 'Origin: http://10.10.10.10' \
#   -H 'Referer: http://10.10.10.10/eportal/index.jsp' \
#   -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0' \
#   --data-raw 'userId={username}&password={password_urlencoded}&service=&queryString=&operatorPwd=&operatorUserId=&validcode=&passwordEncrypt=false'

# ------- 示例 B：原始 HTTP 请求 -------
# POST /eportal/InterFace.do?method=login HTTP/1.1
# Host: 10.10.10.10
# Content-Type: application/x-www-form-urlencoded
# Referer: http://10.10.10.10/eportal/index.jsp
#
# userId={username}&password={password_urlencoded}&service=

# ------- 示例 C：手动格式 -------
# *method = POST
# *url = http://10.10.10.10/eportal/InterFace.do?method=login
# Content-Type: application/x-www-form-urlencoded
# Referer: http://10.10.10.10/eportal/index.jsp
# [body]
# userId={username}&password={password_urlencoded}&service=
"""


class ConfigError(Exception):
    """配置 / 请求文件有问题，需要人工处理。"""


def read_text_file(path: Path) -> str:
    """读文本文件，优先 UTF-8（含 BOM），失败再按 GBK 读（兼容老版记事本）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"读取文件失败：{path}\n{exc}")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConfigError(
        f"文件编码无法识别（既不是 UTF-8 也不是 GBK）：{path}\n"
        "请用记事本“另存为”并选择 UTF-8 编码。"
    )


# ==========================================================================
# 二、路径、日志、控制台提示
# ==========================================================================

def app_dir() -> Path:
    """程序所在目录（打包成 exe 后是 exe 所在目录）。配置文件、日志都放这里。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class _ConsoleFormatter(logging.Formatter):
    TAGS = {
        "DEBUG": "调试",
        "INFO": "信息",
        "WARNING": "警告",
        "ERROR": "错误",
        "CRITICAL": "严重",
    }

    def format(self, record: logging.LogRecord) -> str:
        tag = self.TAGS.get(record.levelname, record.levelname)
        msg = super().format(record)
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"[{tag}] {msg}"


def setup_logging(level_name: str, base: Path, debug: bool = False) -> None:
    level = logging.DEBUG if debug else getattr(logging, str(level_name).upper(), logging.INFO)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    # 控制台：打包成无窗口 exe 时 sys.stdout 是 None，此时只写文件日志
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(errors="replace")  # 不改变编码，只避免个别字符报错
        except Exception:
            pass
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(_ConsoleFormatter("%(message)s"))
        console.setLevel(level)
        log.addHandler(console)

    try:
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "campus_login.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        file_handler.setLevel(logging.DEBUG)
        log.addHandler(file_handler)
    except OSError as exc:  # 日志目录不可写时不影响主流程
        log.warning("日志文件创建失败（%s），本次只输出到控制台。", exc)


def notify_user(message: str) -> None:
    """无控制台（--noconsole 打包）时，用弹窗告诉用户配置有问题。正常运行不会弹。"""
    if sys.stdout is not None:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    except Exception:
        pass


# ==========================================================================
# 三、配置
# ==========================================================================

@dataclass
class Settings:
    config_path: Path
    base_dir: Path
    # [general]
    startup_delay: float = 0.0
    network_wait: float = 180.0
    retry_times: int = 2
    retry_interval: float = 8.0
    http_timeout: float = 10.0
    verify_ssl: bool = True
    use_system_proxy: bool = False
    log_level: str = "INFO"
    dump: bool = True
    # [check]
    check_url: str = ""
    check_status: tuple = (204,)
    check_contains: str = ""
    check_fallback: str = ""
    check_fallback_contains: str = ""
    check_timeout: float = 6.0
    check_attempts: int = 3
    check_interval: float = 3.0
    # [login]
    request_file: Path | None = None
    username: str = ""
    password: str = ""
    warmup: bool = True
    warmup_url: str = ""
    allow_redirects: bool = False
    success_keywords: tuple = DEFAULT_SUCCESS_KEYWORDS
    failure_keywords: tuple = DEFAULT_FAILURE_KEYWORDS
    verify_delay: float = 2.0


def _opt(cp: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    if cp.has_section(section) and cp.has_option(section, key):
        return cp.get(section, key).strip()
    return default


def _opt_int(cp, section, key, default):
    raw = _opt(cp, section, key, "")
    if raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        raise ConfigError(f"[{section}] {key} 必须是整数，当前是：{raw}")


def _opt_float(cp, section, key, default):
    raw = _opt(cp, section, key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"[{section}] {key} 必须是数字，当前是：{raw}")


def _opt_bool(cp, section, key, default):
    raw = _opt(cp, section, key, "")
    if raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on", "是", "开")


def _keywords(raw: str, default: tuple) -> tuple:
    items = tuple(x.strip() for x in re.split(r"[,，;；\s]+", raw) if x.strip())
    return items or default


def load_settings(path: Path) -> Settings:
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")

    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(read_text_file(path), source=str(path))
    except configparser.Error as exc:
        raise ConfigError(f"配置文件格式错误（{path}）：{exc}")

    if not cp.has_section("login"):
        raise ConfigError("config.ini 里缺少 [login] 段，可能文件被改坏了，建议删掉后重新运行生成模板。")

    st = Settings(config_path=path, base_dir=path.parent)

    st.startup_delay = max(0.0, _opt_float(cp, "general", "startup_delay", 0.0))
    st.network_wait = max(0.0, _opt_float(cp, "general", "network_wait", 180.0))
    st.retry_times = max(1, _opt_int(cp, "general", "retry_times", 2))
    st.retry_interval = max(0.0, _opt_float(cp, "general", "retry_interval", 8.0))
    st.http_timeout = max(1.0, _opt_float(cp, "general", "http_timeout", 10.0))
    st.verify_ssl = _opt_bool(cp, "general", "verify_ssl", True)
    st.use_system_proxy = _opt_bool(cp, "general", "use_system_proxy", False)
    st.log_level = _opt(cp, "general", "log_level", "INFO").upper()
    st.dump = _opt_bool(cp, "general", "dump", True)

    st.check_url = _opt(cp, "check", "url", "http://connect.rom.miui.com/generate_204")
    raw_status = _opt(cp, "check", "expect_status", "204")
    try:
        st.check_status = tuple(int(x) for x in re.split(r"[,，\s]+", raw_status) if x)
    except ValueError:
        raise ConfigError(f"[check] expect_status 必须是状态码，例如 204 或 200,204，当前是：{raw_status}")
    st.check_contains = _opt(cp, "check", "expect_contains", "")
    st.check_fallback = _opt(cp, "check", "fallback_url", "")
    st.check_fallback_contains = _opt(cp, "check", "fallback_contains", "")
    st.check_timeout = max(1.0, _opt_float(cp, "check", "timeout", 6.0))
    st.check_attempts = max(1, _opt_int(cp, "check", "attempts", 3))
    st.check_interval = max(0.0, _opt_float(cp, "check", "interval", 3.0))

    st.username = _opt(cp, "login", "username", "")
    st.password = _opt(cp, "login", "password", "")
    st.warmup = _opt_bool(cp, "login", "warmup", True)
    st.warmup_url = _opt(cp, "login", "warmup_url", "")
    st.allow_redirects = _opt_bool(cp, "login", "allow_redirects", False)
    st.success_keywords = _keywords(_opt(cp, "login", "success_keyword", ""), DEFAULT_SUCCESS_KEYWORDS)
    st.failure_keywords = _keywords(_opt(cp, "login", "failure_keyword", ""), DEFAULT_FAILURE_KEYWORDS)
    st.verify_delay = max(0.0, _opt_float(cp, "login", "verify_delay", 2.0))

    raw_file = _opt(cp, "login", "request_file", "login_request.txt") or "login_request.txt"
    req_path = Path(raw_file).expanduser()
    if not req_path.is_absolute():
        req_path = path.parent / req_path
    st.request_file = req_path

    # 账号密码必填检查
    if not st.username or st.username in ("你的学号", "学号", "username"):
        raise ConfigError(
            "config.ini 里的 username 还没填。\n"
            "请把 [login] 下的 username 改成你的学号/账号。"
        )
    if not st.password or st.password in ("你的密码", "密码", "password"):
        raise ConfigError(
            "config.ini 里的 password 还没填。\n"
            "请把 [login] 下的 password 改成你的密码。"
        )
    return st


# ==========================================================================
# 四、解析抓包得到的登录请求
# ==========================================================================

@dataclass
class HttpRequest:
    method: str = "POST"
    url: str = ""
    headers: dict = field(default_factory=dict)
    body: str = ""
    warnings: list = field(default_factory=list)


CURL_VALUE_OPTS = {
    "-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "-b", "--cookie", "-X", "--request", "-A", "--user-agent", "-e", "--referer",
    "--url", "-u", "--user", "-x", "--proxy", "--connect-timeout", "-m", "--max-time",
    "-F", "--form", "--form-string", "-o", "--output", "--cacert", "--cert", "--key",
    "-r", "--range", "--retry", "--retry-delay", "--interface", "--resolve",
    "-T", "--upload-file", "--limit-rate", "-w", "--write-out", "-K", "--config",
}
CURL_FLAG_OPTS = {
    "--compressed", "-k", "--insecure", "-L", "--location", "-s", "--silent",
    "-S", "--show-error", "-v", "--verbose", "-i", "--include", "-g", "--globoff",
    "-N", "--no-buffer", "--http1.1", "--http2", "--http2-prior-knowledge",
    "-G", "--get", "-f", "--fail", "-I", "--head", "-4", "--ipv4", "-6", "--ipv6",
    "--no-keepalive", "-J", "--remote-header-name", "-O", "--remote-name",
}
COMMENT_RE = re.compile(r"^\s*(#|//)")


def _norm_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_comment_lines(text: str) -> str:
    kept = []
    for line in _norm_newlines(text).split("\n"):
        if COMMENT_RE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _join_curl_continuations(text: str) -> str:
    """把 cmd 的 ^、bash 的 \\、PowerShell 的 ` 续行拼成一行。"""
    parts, buf = [], ""
    for line in _norm_newlines(text).split("\n"):
        stripped = line.rstrip()
        if not buf and not stripped.strip():
            continue
        if stripped.endswith(("\\", "^", "`")):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        parts.append(buf)
        buf = ""
    if buf.strip():
        parts.append(buf)
    return "\n".join(parts)


def _split_curl_args(text: str) -> list:
    """按 shell 规则切分参数，支持单引号、双引号和反斜杠转义。"""
    args, cur, quote_char, i = [], "", None, 0
    while i < len(text):
        ch = text[i]
        if quote_char:
            if ch == quote_char:
                quote_char = None
            elif ch == "\\" and quote_char == '"' and i + 1 < len(text):
                nxt = text[i + 1]
                if nxt in ('"', "\\", "$", "`"):
                    cur += nxt
                    i += 1
                else:
                    cur += ch
            else:
                cur += ch
        else:
            if ch in ("'", '"'):
                quote_char = ch
            elif ch.isspace():
                if cur:
                    args.append(cur)
                    cur = ""
            elif ch == "\\" and i + 1 < len(text):
                cur += text[i + 1]
                i += 1
            else:
                cur += ch
        i += 1
    if cur:
        args.append(cur)
    return args


def _split_opt(token: str):
    """把 --data-raw=xxx 拆成 (--data-raw, xxx)。"""
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value
    return token, None


def _parse_header_line(line: str):
    if ":" not in line:
        raise ConfigError(f"请求头格式不对（缺少冒号）：{line}")
    name, value = line.split(":", 1)
    return name.strip(), value.strip()


def _parse_curl(text: str) -> HttpRequest:
    text = _join_curl_continuations(text)
    text = text.replace("$'", "'")  # bash 的 $'...' 写法
    tokens = _split_curl_args(text)
    if tokens and tokens[0].lower() in ("curl", "curl.exe"):
        tokens = tokens[1:]

    req = HttpRequest(method="", url="")
    body_parts, explicit_method, get_mode = [], "", False
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        name, inline = _split_opt(token)
        low = name.lower()

        value = inline
        if name in CURL_VALUE_OPTS:
            if value is None:
                if i + 1 < n:
                    value = tokens[i + 1]
                    i += 1
                else:
                    value = ""
        elif low in CURL_VALUE_OPTS and name.startswith("--"):
            if value is None:
                if i + 1 < n:
                    value = tokens[i + 1]
                    i += 1
                else:
                    value = ""

        if name in ("-H", "--header"):
            key, val = _parse_header_line(value)
            req.headers[key] = val
        elif name in ("-X", "--request"):
            explicit_method = value.upper()
        elif name in ("-d", "--data", "--data-raw", "--data-binary"):
            if value.startswith("@"):
                raise ConfigError(
                    "cURL 的 -d @文件 写法不受支持，请用 --data-raw 把请求体直接写出来。"
                )
            body_parts.append(value)
        elif name == "--data-urlencode":
            if value.startswith("@"):
                raise ConfigError("cURL 的 --data-urlencode @文件 写法不受支持，请直接用 --data-raw。")
            if "=" in value:
                key, val = value.split("=", 1)
                body_parts.append(f"{quote(key, safe='')}={quote(val, safe='')}")
            else:
                body_parts.append(quote(value, safe=""))
        elif name in ("-b", "--cookie"):
            if "=" in value:
                old = req.headers.get("Cookie", "")
                req.headers["Cookie"] = f"{old}; {value}".strip("; ") if old else value
            else:
                req.warnings.append(f"忽略 -b 的文件写法：{value}")
        elif name in ("-A", "--user-agent"):
            req.headers["User-Agent"] = value
        elif name in ("-e", "--referer"):
            req.headers["Referer"] = value
        elif name in ("-u", "--user"):
            token_b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
            req.headers["Authorization"] = "Basic " + token_b64
        elif name in ("-F", "--form", "--form-string"):
            raise ConfigError(
                "检测到 multipart 表单（-F / --form），本脚本不支持这种上传表单。\n"
                "校园网登录一般是 application/x-www-form-urlencoded，"
                "请改用“手动格式”把请求体原样写进 login_request.txt。"
            )
        elif name == "--url":
            req.url = value
        elif name in CURL_FLAG_OPTS:
            if name in ("-G", "--get"):
                get_mode = True
        elif name.startswith("-"):
            req.warnings.append(f"忽略不认识的 cURL 参数：{name}")
        else:
            if not req.url and ("://" in token or token.startswith("/")):
                req.url = token
            else:
                req.warnings.append(f"忽略多余参数：{token}")
        i += 1

    req.body = "&".join(body_parts)
    if get_mode and req.body:
        sep = "&" if "?" in req.url else "?"
        req.url = f"{req.url}{sep}{req.body}"
        req.body = ""
        req.method = explicit_method or "GET"
    else:
        req.method = explicit_method or ("POST" if req.body else "GET")
    if not req.url:
        raise ConfigError("没有从 cURL 内容里找到请求地址（URL）。")
    return req


def _parse_raw_http(text: str) -> HttpRequest:
    lines = _norm_newlines(text).split("\n")
    first = lines[0].strip()
    match = re.match(
        r"^(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s+(\S+)\s+HTTP/\d(?:\.\d)?$",
        first,
        re.I,
    )
    if not match:
        raise ConfigError("原始 HTTP 格式不对，第一行应类似：POST /path HTTP/1.1")
    method, target = match.group(1).upper(), match.group(2)
    headers, i = {}, 1
    while i < len(lines) and lines[i].strip():
        if ":" in lines[i]:
            key, value = _parse_header_line(lines[i])
            headers[key] = value
        i += 1
    body = "\n".join(lines[i + 1:]).strip()

    host = headers.pop("Host", "") or headers.pop("host", "")
    scheme = "http"
    for probe in ("Origin", "Referer"):
        value = headers.get(probe, "")
        if value.lower().startswith("https://"):
            scheme = "https"
            break
    if target.lower().startswith(("http://", "https://")):
        url = target
    elif host:
        url = f"{scheme}://{host}{target}"
    else:
        raise ConfigError("原始 HTTP 请求里没有 Host 头，无法拼出完整地址。")
    return HttpRequest(method=method, url=url, headers=headers, body=body)


def _parse_manual(text: str) -> HttpRequest:
    lines = _norm_newlines(text).split("\n")
    body_index = None
    for index, line in enumerate(lines):
        if line.strip().lower() in ("[body]", "body:"):
            body_index = index
            break
    head_lines = lines[:body_index] if body_index is not None else lines

    req = HttpRequest(method="POST", url="")
    for line in head_lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^\*?\s*(method|url)\s*[:=]\s*(.+)$", stripped, re.I)
        if match:
            key, value = match.group(1).lower(), match.group(2).strip().strip("'\"")
            if key == "method":
                req.method = value.upper()
            else:
                req.url = value
            continue
        if ":" in stripped:
            key, value = _parse_header_line(stripped)
            req.headers[key] = value
            continue
        raise ConfigError(f"手动格式里这一行无法识别：{stripped}")
    if not req.url:
        raise ConfigError("手动格式里缺少 *url = ...")

    if body_index is not None:
        body_lines = [ln.strip() for ln in lines[body_index + 1:] if ln.strip()]
        if len(body_lines) == 1:
            req.body = body_lines[0]
        elif body_lines:
            joined = "\n".join(body_lines)
            req.body = joined if joined.startswith(("{", "[")) else "&".join(body_lines)
    return req


def parse_request_text(text: str) -> HttpRequest:
    clean = _strip_comment_lines(text)
    if not clean.strip():
        raise ConfigError(
            "login_request.txt 里还没有可解析的内容。\n"
            "请按 README.md 的“抓包教学”，把 F12 里 Copy as cURL 的内容粘贴进去"
            "（文件里以 # 开头的示例可以留着，不影响）。"
        )

    if re.match(r"^\s*curl(\.exe)?\s", clean, re.I):
        req = _parse_curl(clean)
    elif re.match(r"^[A-Z]+\s+\S+\s+HTTP/\d", clean.strip(), re.I):
        req = _parse_raw_http(clean.strip())
    elif re.search(r"^\s*\*?\s*(url|method)\s*[:=]", clean, re.I | re.M):
        req = _parse_manual(clean)
    else:
        raise ConfigError(
            "看不懂 login_request.txt 的格式。支持三种：\n"
            "  A) 以 curl 开头的一整段（推荐，F12 右键 Copy as cURL）\n"
            "  B) POST /path HTTP/1.1 开头的原始请求\n"
            "  C) *url = http://... 开头的手动格式\n"
            "请对照 README.md 的“抓包教学”重新复制。"
        )

    if not req.url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"登录接口地址不是 http/https 开头：{req.url}")
    return req


def finalize_headers(req: HttpRequest) -> None:
    cleaned = {}
    for key, value in req.headers.items():
        name = key.strip()
        if not name or name.startswith(":"):
            continue
        if name.lower() in ("content-length", "host"):
            continue
        if name.lower() == "accept-encoding":
            value = "gzip, deflate"  # 去掉 br / zstd，避免 requests 解不开
        cleaned[name] = value
    req.headers = cleaned
    req.headers.setdefault("User-Agent", DEFAULT_UA)
    req.headers.setdefault("Accept", "*/*")
    if req.body and not any(k.lower() == "content-type" for k in req.headers):
        req.headers["Content-Type"] = "application/x-www-form-urlencoded"


# ==========================================================================
# 五、占位符替换
# ==========================================================================

def _local_ip_for(host: str, port: int = 80) -> str:
    targets = []
    if host:
        targets.append((host, port))
    targets.append(("223.5.5.5", 53))
    for target_host, target_port in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target_host, target_port))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            sock.close()
    # 兜底：从主机名解析本机局域网 IP
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return ""


def _mac_address() -> str:
    node = uuid.getnode()
    raw = f"{node:012x}".upper()
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def build_placeholders(settings: Settings, req: HttpRequest) -> dict:
    parts = urlsplit(req.url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    now = datetime.now()
    return {
        "username": settings.username,
        "password": settings.password,
        "password_urlencoded": quote(settings.password, safe=""),
        "username_urlencoded": quote(settings.username, safe=""),
        "local_ip": _local_ip_for(host, port),
        "local_mac": _mac_address(),
        "hostname": socket.gethostname(),
        "timestamp": str(int(now.timestamp())),
        "timestamp_ms": str(int(now.timestamp() * 1000)),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
    }


def apply_placeholders(req: HttpRequest, values: dict) -> None:
    def render(text: str) -> str:
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        return text

    req.url = render(req.url)
    req.headers = {key: render(value) for key, value in req.headers.items()}
    req.body = render(req.body)


def describe_missing_placeholders(req: HttpRequest) -> list:
    known = ("username", "password", "password_urlencoded", "username_urlencoded",
             "local_ip", "local_mac", "hostname", "timestamp", "timestamp_ms", "date", "time")
    found = []
    for key in known:
        if "{" + key + "}" in req.url or "{" + key + "}" in req.body:
            found.append(key)
    return found


# ==========================================================================
# 六、联网检测
# ==========================================================================

@dataclass
class NetState:
    state: str
    detail: str
    portal: str = ""


def build_session(settings: Settings):
    import requests

    if not settings.verify_ssl:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    session = requests.Session()
    session.trust_env = settings.use_system_proxy
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Connection": "keep-alive",
    })
    return session


def response_text(resp) -> str:
    encoding = getattr(resp, "encoding", None)
    if not encoding or encoding.lower() in ("iso-8859-1", "ascii"):
        try:
            encoding = resp.apparent_encoding or "utf-8"
        except Exception:
            encoding = "utf-8"
    try:
        return resp.content.decode(encoding, "replace")
    except (LookupError, TypeError):
        return resp.content.decode("utf-8", "replace")


def _judge_probe(resp, statuses: tuple, contains: str) -> tuple:
    status = resp.status_code
    location = resp.headers.get("Location", "")
    final_url = getattr(resp, "url", "")
    if status in statuses:
        if contains:
            body = response_text(resp)[:200_000]
            if contains not in body:
                where = f"（最终地址 {final_url}）" if final_url and final_url != location else ""
                return False, f"HTTP {status}，但内容里没有“{contains}”{where}，可能被门户劫持", ""
        return True, f"HTTP {status}，检测通过", ""
    if 300 <= status < 400:
        return False, f"HTTP {status} 重定向到 {location or '(空)'}，说明还没登录", location
    if status in (401, 403):
        return False, f"HTTP {status}，被网关拦截，说明还没登录", ""
    return False, f"HTTP {status}，不符合预期状态码 {statuses}", ""


def _probe(session, settings: Settings, url: str):
    import requests

    try:
        # 跟随跳转：门户未登录时最终会落到登录页（内容对不上），
        # 百度这类站点自己会 302 到 https，跟随之后才能看到真正内容。
        resp = session.get(
            url,
            timeout=settings.check_timeout,
            allow_redirects=True,
            verify=settings.verify_ssl,
        )
        return resp, None
    except requests.RequestException as exc:
        return None, exc


def _check_once(session, settings: Settings) -> NetState:
    primary_detail = "未配置检测地址"
    primary_got_response = False
    if settings.check_url:
        resp, err = _probe(session, settings, settings.check_url)
        if resp is not None:
            primary_got_response = True
            ok, detail, portal = _judge_probe(resp, settings.check_status, settings.check_contains)
            if ok:
                return NetState("online", detail)
            primary_detail = detail
            if portal:
                return NetState("portal", detail, portal)
        else:
            primary_detail = _short_error(err)

    if settings.check_fallback:
        resp, err = _probe(session, settings, settings.check_fallback)
        if resp is not None:
            ok, detail, portal = _judge_probe(resp, (200,), settings.check_fallback_contains)
            if ok:
                return NetState("online", detail)
            return NetState(
                "portal" if portal else "offline",
                f"{detail}；主检测：{primary_detail}",
                portal,
            )
        return NetState("no-network", f"主检测：{primary_detail}；备用检测：{_short_error(err)}")

    if primary_got_response:
        return NetState("offline", primary_detail)
    return NetState("no-network", primary_detail)


def check_online(session, settings: Settings, attempts: int | None = None,
                 interval: float | None = None) -> NetState:
    total = settings.check_attempts if attempts is None else max(1, attempts)
    wait = settings.check_interval if interval is None else interval
    last = NetState("no-network", "未检测")
    for index in range(total):
        last = _check_once(session, settings)
        if last.state == "online":
            return last
        if last.state in ("portal", "offline"):
            return last
        if index + 1 < total:
            log.info("联网检测第 %d/%d 次未通过（%s），%.0f 秒后重试 ...",
                     index + 1, total, last.detail, wait)
            time.sleep(wait)
    return last


def portal_reachable(settings: Settings, req: HttpRequest) -> bool:
    parts = urlsplit(req.url)
    host = parts.hostname
    if not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=settings.check_timeout):
            return True
    except OSError:
        return False


def wait_for_portal(settings: Settings, req: HttpRequest) -> bool:
    """开机时 WiFi 可能起得很慢，先等门户地址可达，最多 network_wait 秒。"""
    if settings.network_wait <= 0:
        return portal_reachable(settings, req)
    deadline = time.time() + settings.network_wait
    last_note = 0.0
    while True:
        if portal_reachable(settings, req):
            return True
        now = time.time()
        if now >= deadline:
            return False
        if now - last_note >= 15:
            log.info("等待网络就绪（WiFi / 网线），最多再等 %d 秒 ...", int(deadline - now))
            last_note = now
        time.sleep(5)


# ==========================================================================
# 七、判断接口返回
# ==========================================================================

@dataclass
class Verdict:
    kind: str          # success / failure / unknown
    reason: str
    snippet: str
    hint: str = ""


def _maybe_json(text: str):
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    match = re.match(r"^[\w$.]+\s*\((.*)\)\s*;?\s*$", stripped, re.S)
    if match:
        candidates.append(match.group(1))
    for item in candidates:
        if item[:1] in ("{", "["):
            try:
                return json.loads(item)
            except (ValueError, TypeError):
                continue
    return None


def _json_verdict(obj) -> tuple:
    if not isinstance(obj, dict):
        return None, ""
    flat = {str(k).lower(): v for k, v in obj.items()}
    message = ""
    for key in ("message", "msg", "msga", "error_msg", "errmsg", "info", "error"):
        value = flat.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break

    for key in ("result", "res", "success", "status", "code", "errno", "ret"):
        if key not in flat:
            continue
        value = flat[key]
        if isinstance(value, bool):
            return ("success" if value else "failure"), message
        if isinstance(value, (int, float)):
            # Dr.COM 的 result：1=成功，0=失败/已在线；code/errno/ret：0=成功
            if key == "result":
                return ("success" if value == 1 else "failure"), message
            if key in ("code", "errno", "ret"):
                return ("success" if value == 0 else "failure"), message
        if isinstance(value, str):
            token = value.strip().lower()
            if key in ("code", "errno", "ret") and token.isdigit():
                return ("success" if int(token) == 0 else "failure"), message
            if token in ("success", "ok", "true", "1", "200"):
                return "success", message
            if token in ("fail", "failed", "false", "error", "0"):
                return "failure", message
    return None, message


def _find_keyword(text: str, keywords: tuple) -> str:
    lowered = text.lower()
    for keyword in keywords:
        if keyword and keyword.lower() in lowered:
            return keyword
    return ""


def _snippet(text: str, limit: int = 300) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def analyze_response(resp, settings: Settings) -> Verdict:
    text = response_text(resp)
    snippet = _snippet(text)
    status = resp.status_code

    online_kw = _find_keyword(text, ALREADY_ONLINE_KEYWORDS)
    if online_kw:
        return Verdict("success", f"服务器提示“{online_kw}”，说明这个账号已经在线", snippet)

    data = _maybe_json(text)
    if data is not None:
        kind, message = _json_verdict(data)
        if kind == "success":
            return Verdict("success", message or "接口返回成功", snippet)
        if kind == "failure":
            return Verdict("failure", message or "接口返回失败", snippet,
                           "账号/密码是否正确？是否需要验证码或二次认证？")

    success_kw = _find_keyword(text, settings.success_keywords)
    failure_kw = _find_keyword(text, settings.failure_keywords)
    if success_kw:
        return Verdict("success", f"返回内容包含“{success_kw}”", snippet)
    if failure_kw:
        return Verdict("failure", f"返回内容包含“{failure_kw}”", snippet,
                       "看接口返回的 message 字段；如果是密码错误，先手动登录一次确认账号状态。")

    if status in (401, 403):
        return Verdict("failure", f"HTTP {status}", snippet,
                       "请求头 / Cookie 不完整，或 Referer、User-Agent 校验失败。"
                       "建议重新抓一次包，确保请求头和抓包时完全一致。")
    if status == 404:
        return Verdict("failure", "HTTP 404，接口地址不存在", snippet,
                       "登录接口地址复制错了（可能漏了路径或 queryString）。重新 Copy as cURL。")
    if status >= 500:
        return Verdict("failure", f"HTTP {status}，服务器异常", snippet,
                       "参数格式可能不对；用 --dry-run 对照 F12 的表单参数逐项检查。")
    if 300 <= status < 400:
        location = resp.headers.get("Location", "")
        return Verdict("unknown", f"HTTP {status} 跳转到 {location or '(空)'}", snippet,
                       "有些学校登录成功会 302。把 allow_redirects 改成 true 再试，"
                       "或直接看联网复检结果。")
    return Verdict("unknown", f"HTTP {status}，返回内容无法识别", snippet,
                   "用 --debug 看 logs/last_response.txt；对照浏览器里同样的请求返回什么。")


# ==========================================================================
# 八、脱敏、日志落盘
# ==========================================================================

def mask_secrets(text: str, settings: Settings) -> str:
    if not text:
        return text
    password = settings.password or ""
    if password:
        variants = {
            password,
            quote(password, safe=""),
            quote_plus(password, safe=""),
            json.dumps(password, ensure_ascii=False)[1:-1],
        }
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                text = text.replace(variant, "***")

    def mask_pair(match):
        name = match.group(1)
        if "encrypt" in name.lower():
            return match.group(0)
        return f"{name}{match.group(2)}***"

    text = re.sub(
        r"(?i)([A-Za-z0-9_\-]*(?:password|passwd|pwd|upass|token|secret)[A-Za-z0-9_\-]*)"
        r"(\s*[=:]\s*)([^&\s\"'<>]+)",
        mask_pair,
        text,
    )

    def mask_json(match):
        if "encrypt" in match.group(2).lower():
            return match.group(0)
        return f"{match.group(1)}***{match.group(4)}"

    text = re.sub(
        r"(?i)(\"([A-Za-z0-9_\-]*(?:password|passwd|pwd|upass|token|secret)[A-Za-z0-9_\-]*)\""
        r"\s*:\s*\")([^\"]*)(\")",
        mask_json,
        text,
    )
    return text


def write_dumps(settings: Settings, req: HttpRequest, resp=None,
                exc_text: str = "", attempt: int = 1) -> None:
    if not settings.dump:
        return
    try:
        dump_dir = settings.base_dir / "logs"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        req_lines = [
            f"# 第 {attempt} 次尝试  {stamp}",
            f"{req.method} {req.url}",
            "",
            "[请求头]",
        ]
        req_lines += [f"{key}: {value}" for key, value in req.headers.items()]
        if req.body:
            req_lines += ["", "[请求体]", req.body]
        (dump_dir / "last_request.txt").write_text(
            mask_secrets("\n".join(req_lines), settings), encoding="utf-8"
        )

        if resp is not None:
            resp_lines = [
                f"# 第 {attempt} 次尝试  {stamp}",
                f"HTTP {resp.status_code} {getattr(resp, 'reason', '')}",
                "",
                "[响应头]",
            ]
            resp_lines += [f"{key}: {value}" for key, value in resp.headers.items()]
            body = response_text(resp)
            if len(body) > 20000:
                body = body[:20000] + f"\n...(已截断，原始长度 {len(body)} 字符)"
            resp_lines += ["", "[响应体]", body]
            (dump_dir / "last_response.txt").write_text(
                mask_secrets("\n".join(resp_lines), settings), encoding="utf-8"
            )
        elif exc_text:
            (dump_dir / "last_response.txt").write_text(
                mask_secrets(f"# {stamp}\n{exc_text}", settings), encoding="utf-8"
            )
    except OSError as exc:
        log.debug("写调试文件失败：%s", exc)


# ==========================================================================
# 九、请求发送与登录流程
# ==========================================================================

def describe_exception(exc: BaseException, url: str = "") -> str:
    import requests

    text = f"{type(exc).__name__}: {str(exc)[:400]}"
    low = text.lower()
    host = ""
    try:
        host = urlsplit(url).netloc
    except Exception:
        pass

    if isinstance(exc, requests.exceptions.SSLError) or "certificate verify failed" in low:
        cause = "HTTPS 证书校验失败，学校门户多半用了自签名证书。"
        fix = "把 config.ini 的 [general] verify_ssl 改成 false；如果地址本是 http，检查是不是写成了 https。"
    elif ("getaddrinfo failed" in low or "name or service not known" in low
          or "nodename nor servname" in low or "11001" in text):
        cause = f"域名解析失败，DNS 查不到 {host or '接口域名'}。"
        fix = "确认地址没写错；确认已连上校园网；能拿到门户 IP 的话直接换成 IP。"
    elif "connection refused" in low or "10061" in text or "积极拒绝" in text:
        cause = "目标端口没有服务在监听（连接被拒绝）。"
        fix = "检查接口地址的端口是否写错；确认复制 cURL 时没有漏掉路径。"
    elif ("timed out" in low or "10060" in text
          or isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout))):
        cause = "请求超时，数据包发出去没有回应。"
        fix = "确认已连校园 WiFi/网线；调大 http_timeout；用浏览器打开接口地址确认能访问。"
    elif ("network is unreachable" in low or "no route to host" in low
          or "10051" in text or "10065" in text or "无法访问网络" in text):
        cause = "本机没有到目标网络的路由，通常是没有连上校园网。"
        fix = "检查 WiFi/网线；开机自启时把 startup_delay 调大到 30~60。"
    elif isinstance(exc, requests.exceptions.ProxyError) or "proxy" in low:
        cause = "系统代理拦截了请求（Clash、加速器、公司代理等）。"
        fix = "关闭系统代理，并保持 config.ini 里 use_system_proxy = false。"
    elif isinstance(exc, requests.exceptions.TooManyRedirects):
        cause = "重定向次数过多，被门户反复跳转。"
        fix = "把 allow_redirects 设为 false；直接使用登录接口地址。"
    elif isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema)):
        cause = "接口地址格式不对。"
        fix = "地址必须以 http:// 或 https:// 开头，并包含完整路径。"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        cause = "无法建立连接（网络不通、地址错误或被防火墙拦截）。"
        fix = "先用浏览器打开登录页确认地址；确认已连校园网；检查代理/VPN。"
    else:
        cause = "未归类的请求异常。"
        fix = "用 --debug 再跑一次，并查看 logs 目录里的 last_request.txt / last_response.txt。"
    return f"{text}\n可能原因：{cause}\n处理建议：{fix}"


def _short_error(exc: BaseException | None) -> str:
    if exc is None:
        return "未知错误"
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def do_warmup(session, settings: Settings, req: HttpRequest) -> None:
    if not settings.warmup:
        return
    url = settings.warmup_url or req.headers.get("Referer", "") or ""
    if not url:
        parts = urlsplit(req.url)
        if parts.scheme and parts.netloc:
            url = f"{parts.scheme}://{parts.netloc}/"
    if not url or url == req.url:
        return
    try:
        resp = session.get(
            url,
            timeout=settings.http_timeout,
            verify=settings.verify_ssl,
            allow_redirects=True,
        )
        log.debug("预热请求 GET %s -> HTTP %s", url, resp.status_code)
        cookies = session.cookies.get_dict()
        if cookies:
            log.debug("预热拿到 %d 个 Cookie：%s", len(cookies), ", ".join(cookies))
    except Exception as exc:
        log.warning("预热请求失败（不影响后续登录）：%s", _short_error(exc))


def verify_online(session, settings: Settings) -> tuple:
    if settings.verify_delay > 0:
        time.sleep(settings.verify_delay)
    state = check_online(session, settings, attempts=2, interval=2.0)
    return state.state == "online", state.detail


def perform_login(session, settings: Settings, req: HttpRequest) -> int:
    import requests

    attempts = max(1, settings.retry_times)
    started = time.time()
    for attempt in range(1, attempts + 1):
        log.info("发送登录请求（第 %d/%d 次）：%s %s",
                 attempt, attempts, req.method, mask_secrets(req.url, settings))
        try:
            resp = session.request(
                req.method,
                req.url,
                headers=req.headers,
                data=req.body.encode("utf-8") if req.body else None,
                timeout=settings.http_timeout,
                allow_redirects=settings.allow_redirects,
                verify=settings.verify_ssl,
            )
        except requests.RequestException as exc:
            reason = mask_secrets(describe_exception(exc, req.url), settings)
            log.error("请求发送失败：%s", reason)
            write_dumps(settings, req, exc_text=reason, attempt=attempt)
            if attempt < attempts:
                log.info("等待 %.0f 秒后重试 ...", settings.retry_interval)
                time.sleep(settings.retry_interval)
                continue
            log.error("已尝试 %d 次仍失败，退出。", attempts)
            return EXIT_REQUEST

        write_dumps(settings, req, resp=resp, attempt=attempt)
        verdict = analyze_response(resp, settings)
        log.debug("接口返回 HTTP %s，判断：%s（%s）", resp.status_code, verdict.kind, verdict.reason)

        online, detail = verify_online(session, settings)
        elapsed = time.time() - started
        if online:
            log.info("登录成功，用时 %.1f 秒。", elapsed)
            log.info("联网复检：%s", detail)
            return EXIT_OK

        if verdict.kind == "success":
            log.warning(
                "接口返回成功，但联网复检没通过（%s）。可能是认证生效有延迟，或检测地址被学校限制。",
                detail,
            )
            log.warning("按登录成功处理。若实际打不开网页，请把 config.ini 里的 check.url 换成校内能访问的地址。")
            return EXIT_OK

        if verdict.kind == "failure":
            log.error("登录失败：%s", mask_secrets(verdict.reason, settings))
            log.error("接口返回（截断）：%s", mask_secrets(verdict.snippet, settings))
            if verdict.hint:
                log.error("排查建议：%s", verdict.hint)
            log.error("完整请求/响应已写入：%s", settings.base_dir / "logs")
            return EXIT_AUTH

        log.error("无法判断登录结果：HTTP %s，联网复检仍未通过（%s）。", resp.status_code, detail)
        log.error("接口返回（截断）：%s", mask_secrets(verdict.snippet, settings))
        if verdict.hint:
            log.error("排查建议：%s", verdict.hint)
        log.error("完整请求/响应已写入：%s", settings.base_dir / "logs")
        if attempt < attempts:
            log.info("等待 %.0f 秒后重试 ...", settings.retry_interval)
            time.sleep(settings.retry_interval)
            continue
        return EXIT_AUTH

    return EXIT_AUTH


# ==========================================================================
# 十、启动流程
# ==========================================================================

def ensure_files(config_path: Path) -> bool:
    """配置文件不存在时生成模板。返回 True 表示这是第一次运行。"""
    first_run = False
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        first_run = True
        request_path = config_path.parent / "login_request.txt"
        if not request_path.exists():
            request_path.write_text(DEFAULT_REQUEST, encoding="utf-8")
    return first_run


def parse_request_file(settings: Settings) -> HttpRequest:
    path = settings.request_file
    if path is None:
        raise ConfigError("没有配置 request_file")
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_REQUEST, encoding="utf-8")
        except OSError:
            pass
        raise ConfigError(
            f"找不到请求文件：{path}\n"
            "已生成模板。请按 README.md 的“抓包教学”把登录请求粘贴进去。"
        )
    try:
        text = read_text_file(path)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"读取请求文件失败：{path}\n{exc}")
    request = parse_request_text(text)
    for warning in request.warnings:
        log.warning("解析提示：%s", warning)
    return request


def print_dry_run(req: HttpRequest, settings: Settings) -> None:
    log.info("================ 解析结果（--dry-run，不会真的发送）================")
    log.info("请求方法：%s", req.method)
    log.info("请求地址：%s", mask_secrets(req.url, settings))
    log.info("请求头（共 %d 个）：", len(req.headers))
    for key, value in req.headers.items():
        log.info("    %s: %s", key, mask_secrets(value, settings))
    if req.body:
        log.info("请求体：")
        log.info("    %s", mask_secrets(req.body, settings))
    else:
        log.info("请求体：（空）")
    log.info("提示：把上面的内容与 F12 里的 Request Headers / Form Data 对照，")
    log.info("      一致就说明解析正确，可以正式运行 python campus_login.py 了。")


def prepare_request(settings: Settings, request: HttpRequest) -> None:
    """填充占位符、补齐请求头。必须等网络就绪后调用，才能拿到正确的本机 IP。"""
    apply_placeholders(request, build_placeholders(settings, request))
    finalize_headers(request)
    missing = describe_missing_placeholders(request)
    if missing:
        raise ConfigError(
            "请求里还有没替换的占位符：" + ", ".join("{" + m + "}" for m in missing)
            + "\n请检查拼写，或把该值改成固定内容。"
        )


def run(args) -> int:
    base = app_dir()
    config_path = Path(args.config).expanduser().resolve() if args.config else base / "config.ini"

    if ensure_files(config_path):
        setup_logging("INFO", config_path.parent, args.debug)
        message = (
            "首次运行，已生成配置模板：\n"
            f"  {config_path}\n"
            f"  {config_path.parent / 'login_request.txt'}\n\n"
            "请先完成两步：\n"
            "  1) 按 README.md 抓包，把登录请求粘贴到 login_request.txt\n"
            "  2) 在 config.ini 里填好 username / password\n"
            "然后重新运行本程序。"
        )
        log.warning(message)
        notify_user(message)
        return EXIT_CONFIG

    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        setup_logging("INFO", config_path.parent, args.debug)
        log.error("%s", exc)
        notify_user(str(exc))
        return EXIT_CONFIG

    setup_logging(settings.log_level, settings.base_dir, args.debug)
    # --dry-run / --check 是人工排查用的，强制显示 INFO 及以上，避免被 log_level 过滤掉
    if args.debug or args.dry_run or args.check:
        for handler in log.handlers:
            handler.setLevel(logging.DEBUG if args.debug else logging.INFO)
    log.info("%s v%s", APP_NAME, APP_VERSION)
    log.debug("配置文件：%s", settings.config_path)
    log.debug("请求文件：%s", settings.request_file)

    try:
        import requests  # noqa: F401
    except ImportError:
        log.error("缺少 requests 库。请先执行：python -m pip install requests")
        return EXIT_CONFIG

    session = build_session(settings)

    if args.check:
        state = check_online(session, settings)
        log.info("联网检测：%s（%s）", STATE_TEXT.get(state.state, state.state), state.detail)
        return EXIT_OK if state.state == "online" else EXIT_NETWORK

    try:
        request = parse_request_file(settings)
    except ConfigError as exc:
        log.error("解析登录请求失败：%s", exc)
        notify_user(f"解析登录请求失败：\n{exc}")
        return EXIT_CONFIG

    if args.dry_run:
        try:
            prepare_request(settings, request)
        except ConfigError as exc:
            log.error("解析登录请求失败：%s", exc)
            return EXIT_CONFIG
        print_dry_run(request, settings)
        return EXIT_OK

    if settings.startup_delay > 0:
        log.info("先等待 %.0f 秒，让网络就绪 ...", settings.startup_delay)
        time.sleep(settings.startup_delay)

    if not wait_for_portal(settings, request):
        log.error("等了 %.0f 秒，登录接口仍然连不上（WiFi 可能还没连上）。", settings.network_wait)
        log.error("处理建议：确认校园 WiFi 已设置为“自动连接”；")
        log.error("          或把 config.ini 的 startup_delay / network_wait 调大。")
        return EXIT_NETWORK

    # 等网络就绪之后再取本机 IP、时间戳，避免拿到空 IP
    try:
        prepare_request(settings, request)
    except ConfigError as exc:
        log.error("解析登录请求失败：%s", exc)
        notify_user(f"解析登录请求失败：\n{exc}")
        return EXIT_CONFIG

    state = check_online(session, settings)
    log.info("联网检测：%s（%s）", STATE_TEXT.get(state.state, state.state), state.detail)

    if state.state == "online":
        log.info("当前已经能上网（通常代表已登录），无需操作，直接退出。")
        return EXIT_OK

    if state.state == "no-network":
        if portal_reachable(settings, request):
            log.warning("外网检测不通，但门户地址可以连接，按“未登录”继续尝试登录。")
        else:
            log.error("网络不可达，登录接口也连不上。")
            log.error("处理建议：确认已连接校园 WiFi 或网线；开机自启请把 startup_delay 调到 30~60；")
            log.error("          确认 login_request.txt 里的地址在当前网络能访问。")
            return EXIT_NETWORK

    do_warmup(session, settings, request)
    return perform_login(session, settings, request)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="校园网后台静默登录（requests 版，不打开浏览器）",
    )
    parser.add_argument("-c", "--config", help="配置文件路径，默认是程序目录下的 config.ini")
    parser.add_argument("--check", action="store_true", help="只检测当前是否已经联网")
    parser.add_argument("--dry-run", action="store_true", help="只解析并显示将要发送的请求，不发送")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("用户中断")
        return 130
    except Exception:
        log.exception("未预期的异常")
        return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
