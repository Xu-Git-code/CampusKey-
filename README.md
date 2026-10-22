# 校园网自动登录模板（Python + requests，无浏览器）

抓一次登录请求，改两行配置，就能在开机时后台完成校园网网页认证。
已在线会自动跳过，不会把会话顶掉；全程只用 `requests` 发请求，不启动浏览器、不模拟点击。

> 这是一个**模板**，不是下载即用的成品。
> 每所学校的认证接口、参数都不一样，需要你按 [抓包教学](抓包教学.md) 自己抓一次登录请求。

## 适用条件

- Windows 10 / 11（脚本本身跨平台，开机自启部分只写了 Windows）
- 校园网是**网页认证**：打开浏览器会跳登录页，输入学号密码后才能上网
- 能按 F12 抓到登录请求（POST 表单或 GET 参数都行）
- Python 3.8+

**不适用**：客户端认证（锐捷、哆点等私有协议）、需要验证码、密码由 JS 加密且无法复用密文。

## 特性

- 纯后台：`requests` 直接发请求，不打开浏览器
- 先检测后登录：已经在线就退出，避免重复登录把会话顶掉
- 支持三种请求格式：cURL / 原始 HTTP / 手动格式
- 占位符：`{username}`、`{password_urlencoded}`、`{local_ip}` 等，改密码不用重新抓包
- 失败给具体原因：DNS、端口、证书、401、密码错误、验证码……
- 日志自动给密码打码
- 可打包 exe，可设置 Windows 开机自启

## 3 步上手

### 1. 装环境

```bash
python -m pip install requests
```

没装 Python 的，看 [小白看这个就够了](小白看这个就够了.md)。

### 2. 抓包

```bash
copy login_request.example.txt login_request.txt
```

F12 → Network → 点登录 → 找到登录请求 → 右键 `Copy as cURL (bash)` → 粘贴进 `login_request.txt`。
详细步骤见 [抓包教学](抓包教学.md)。

### 3. 配置并运行

```bash
copy config.example.ini config.ini
```

用记事本打开 `config.ini`，把 `username` / `password` 改成自己的，然后：

```bash
python campus_login.py --dry-run
python campus_login.py
```

`--dry-run` 只解析配置并打印将要发送的请求，不联网；确认无误后再正式运行。

## 工作原理

```
启动
 ├─ 读 config.ini、解析 login_request.txt（cURL / 原始 HTTP / 手动格式）
 ├─ 等 startup_delay 秒
 ├─ 等门户可达（最多 network_wait 秒，WiFi 慢就继续等）
 ├─ 检测是否已联网：GET 百度，要求 200 且页面含“百度”
 │    ├─ 已在线 ────────────────────────────► 退出（0）
 │    └─ 未登录
 ├─ 替换 {username} / {password} / {local_ip}
 ├─ GET 门户页预热（拿 Cookie）
 ├─ 发送登录请求
 ├─ 解析返回内容（JSON / JSONP / 关键字）
 ├─ 再检测一次网络
 │    ├─ 通了 ──────────────────────────────► 成功（0）
 │    ├─ 明确失败 ──────────────────────────► 退出（13）
 │    └─ 无法判断 / 网络错误 ── 重试 ───────► 退出（12 / 13）
 └─ 全程写 logs/campus_login.log
```

几个关键设计：

- **先检测、后登录**：已在线不重复登录。
- **用真实网页内容判断**：不用 `generate_204`，避免被校园网关白名单骗到。
- **占位符在发送前才替换**：所以 `{local_ip}` 能取到网络就绪后的正确 IP。
- **只对网络错误重试**：密码错误不重试，防止锁账号。
- **成功与否以“实际能不能上网”为准**，不只看服务器返回的文字。

## 项目结构

| 文件 | 作用 |
|---|---|
| `campus_login.py` | 主程序 |
| `config.example.ini` | 配置模板（复制为 `config.ini` 后填写） |
| `login_request.example.txt` | 登录请求模板（复制为 `login_request.txt` 后粘贴抓包内容） |
| `抓包教学.md` | F12 抓包详细教程 |
| `小白看这个就够了.md` | 零基础从装 Python 到开机自启 |
| `run.bat` / `check.bat` / `查看日志.bat` | 双击运行 / 自检 / 看日志（Windows） |
| `安装开机自启.ps1` | 注册 / 卸载 Windows 计划任务 |
| `build_exe.bat` | 打包成 exe（可选） |
| `logs/` | 运行日志（自动生成，已被 git 忽略） |

## 配置说明

大部分配置在 `config.example.ini` 里有中文注释，重点：

| 配置项 | 说明 |
|---|---|
| `username` / `password` | 你的校园网账号密码（必填） |
| `startup_delay` | 启动后等几秒再动作 |
| `network_wait` | 开机 WiFi 慢时，最多等门户可达多少秒 |
| `check.url` | 判断“已联网”的地址，默认百度，要求页面含“百度” |
| `verify_ssl` | 门户是 https 且证书不被信任时改成 `false` |
| `use_system_proxy` | 建议 `false`，避免代理干扰 |

## 开机自启（Windows）

```powershell
powershell -ExecutionPolicy Bypass -File .\安装开机自启.ps1
```

默认是当前用户登录后 30 秒运行一次。也可以直接双击 `run.bat` 手动运行。
卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\安装开机自启.ps1 -Uninstall
```

> 前提：电脑开机后能自动连上校园 WiFi（或插着网线）。脚本只负责网页认证，不会帮你连 WiFi。

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `config.ini 里的 username 还没填` | 没改模板 | 填账号密码 |
| `login_request.txt 里还没有可解析的内容` | 还是模板 | 按抓包教学粘贴 cURL |
| `HTTP 401 / 403` | 请求头 / Cookie 不全 | 重新抓包，保留 `Cookie`、`Referer`、`User-Agent` |
| `密码错误` / `认证失败` | 账号密码错 / 需要验证码 | 先手动登录确认 |
| 提示成功但网页打不开 | 检测地址被限制 | 换 `check.url` |
| 开机没登录 | WiFi 没自动连 / 网络慢 | 设置 WiFi 自动连接；调大 `network_wait` |

更多见 [小白看这个就够了](小白看这个就够了.md) 的“常见报错”。

## 免责声明

- 本项目仅供**自己的设备**自动完成校园网网页认证，请遵守学校的网络使用规定。
- 不提供绕过认证、共享账号、破解客户端等能力。
- 使用产生的任何后果（账号异常、网络中断等）由使用者自行承担。
- 仓库中不包含任何学校、账号、密码信息，所有配置需使用者自行填写。
