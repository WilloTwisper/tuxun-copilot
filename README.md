# Tuxun Copilot

Tuxun Copilot 是一个面向 [tuxun.fun](https://tuxun.fun) 的非官方命令行地理分析助手。输入游戏 ID 或完整链接后，程序会读取当前轮次的街景信息，下载四个方向的缩略图，并调用 Google Gemini 输出结构化的位置推测和地图链接。

本项目仅供学习、研究和个人技术实验使用。它不是 tuxun.fun 或 Google 的官方产品，也不隶属于这些服务。请遵守相关平台规则、服务条款和当地法律；在竞技或排名场景中使用自动辅助可能破坏公平性。

## 功能

- 自动识别裸游戏 ID，以及 `/solo/`、`/party/`、`/challenge/` 等完整链接
- 兼容单人、组队和每日挑战等不同游戏数据接口
- 监控同一局的新轮次，每个新 `panoId` 仅分析一次
- 并行下载前、右、后、左四个方向的 Google 街景缩略图
- 根据游戏模式选择快速或高精度分析提示词
- 通过 JSON Schema 输出国家、地区、城市、经纬度、置信度和判断依据
- 自动生成可点击的 Google Maps 查询链接
- Gemini API Key 和模型限流时自动轮换备用组合
- 可选接入 Clash/mihomo 控制 API，在真实网络失败时测速、切换和暂时拉黑异常节点

## 要求

- Python 3.10 或更高版本
- 可调用 Gemini API 的 Google AI Studio API Key
- 已登录 tuxun.fun 的 Cookie
- 能访问 Google API 和街景服务的网络环境

## 安装

```bash
git clone https://github.com/WilloTwisper/tuxun-copilot.git
cd tuxun-copilot
python -m venv .venv
```

激活虚拟环境：

```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

安装依赖：

```bash
python -m pip install -r requirements.txt
```

运行离线测试：

```bash
python -m unittest discover -s tests -v
```

## 配置

复制配置模板：

```bash
# Windows PowerShell
Copy-Item .env.example .env

# macOS / Linux
cp .env.example .env
```

编辑本地 `.env`。不要提交、分享或截图传播该文件。

```dotenv
API_KEY="your_google_ai_api_key"
TUXUN_COOKIE="fun_ticket=your_ticket; SESSION=your_session; _t_cfg=your_config_id"

# 可选
API_KEY2="your_second_google_ai_api_key"
GEMINI_MODEL="gemini-3.6-flash"
GEMINI_MODEL_FALLBACKS="gemini-3.7-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-pro-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
PROXY="http://127.0.0.1:7897"
CLASH_CONTROLLER="http://127.0.0.1:9097"
CLASH_SECRET="your_clash_controller_secret"
```

### 环境变量

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `API_KEY` | 是 | Google Gemini 主 API Key |
| `TUXUN_COOKIE` | 是 | tuxun.fun 登录 Cookie；推荐同时包含 `fun_ticket`、`SESSION` 和 `_t_cfg` |
| `API_KEY2` | 否 | 主 Key 限流或被拒时尝试的备用 Key |
| `GEMINI_MODEL` | 否 | 主模型，默认 `gemini-3.6-flash` |
| `GEMINI_MODEL_FALLBACKS` | 否 | 逗号分隔的备用模型列表；可用模型会随 Google 调整 |
| `PROXY` | 否 | Google API 和街景请求使用的 HTTP/Clash 混合端口 |
| `CLASH_CONTROLLER` | 否 | Clash/mihomo External Controller 地址；配置后启用自动节点切换 |
| `CLASH_SECRET` | 否 | External Controller 密钥 |

### 获取 tuxun.fun Cookie

1. 在浏览器中登录 [tuxun.fun](https://tuxun.fun)。
2. 打开开发者工具，进入 Application/应用 -> Cookies -> `https://tuxun.fun`。
3. 复制 `fun_ticket`、`SESSION` 和 `_t_cfg` 的值。
4. 按上面的 Cookie 字符串格式写入本地 `.env`。

Cookie 等同于登录凭证。泄露后应立即退出登录或刷新会话。更多安全说明见 [SECURITY.md](SECURITY.md)。

## 使用

```bash
python -X utf8 -u main.py
```

启动后程序会执行街景下载和 Gemini 链路预检。随后输入裸 ID 或完整游戏链接，例如：

```text
Ge9g36p53Vf2yAL9
https://tuxun.fun/solo/Ge9g36p53Vf2yAL9
https://tuxun.fun/challenge/00000000-0000-0000-0000-000000000000
```

程序进入监控模式后会轮询游戏状态，发现新轮次即分析；游戏结束后返回输入界面。监控过程中按 `Ctrl+C` 返回输入界面，在外层再次按 `Ctrl+C` 退出。

## 工作原理

1. 调用 tuxun.fun 接口读取游戏状态和当前轮次 `panoId`。
2. 为同一 `panoId` 生成四个方向的街景缩略图 URL，并行下载图片。
3. 将图片和分析提示词发送给当前可用的 Gemini 模型。
4. 按 Schema 解析结果，输出位置、置信度、依据和地图链接。
5. 网络异常时按需切换 Clash 节点；Key 或模型限流时轮换备用组合。

代理路由刻意拆分：Google 请求可走 `PROXY`，tuxun.fun API 始终直连。不要把两条链路强制合并，否则可能增加图寻接口延迟或导致 Google 请求不可达。

## 项目结构

```text
.
|-- main.py               # CLI、轮次监控、Gemini 调用、提示词和结果输出
|-- config.py             # 环境变量配置模型
|-- tuxun_client.py       # tuxun.fun 认证 API 客户端
|-- streetview.py         # 独立的街景 URL 和下载客户端
|-- models.py             # 游戏状态和轮次领域对象
|-- gemini_router.py      # Gemini 模型/Key 故障转移
|-- clash_controller.py   # 可选 Clash/mihomo 节点探测与自动切换
|-- requirements.txt      # Python 依赖
|-- tests/                # 不需要联网的核心逻辑测试
|-- .env.example          # 无敏感信息的配置模板
|-- SECURITY.md           # 凭证与漏洞报告说明
|-- LICENSE               # 当前维护者原创增量的许可范围
`-- NOTICE                # 基座来源、版权与许可边界
```

## 独立化与归属

本项目早期产品方向参考了 [haczmrh/tuxun-helper](https://github.com/haczmrh/tuxun-helper)。当前发布版本按独立实现重新组织了认证 API、街景下载、领域模型、Gemini 路由、Clash 控制和 CLI 生命周期，并配套新的离线测试。

当前发布历史不包含旧基座提交；基座项目仅作为产品方向的历史参考，不作为当前代码的授权来源。当前代码按 [MIT License](LICENSE) 发布，参考说明见 [NOTICE](NOTICE)。

## 安全与隐私

- `.env` 已被 Git 忽略；提交前仍应运行 `git status` 和敏感信息扫描。
- 不要在 issue、日志、截图或终端录屏中暴露 Key、Cookie、UID 或 Clash Secret。
- 如果凭证曾进入 Git 历史，仅删除当前文件不够；必须撤销凭证，并根据公开状态决定是否重写历史。
- 本仓库不会提供共享 Key、Cookie 或代理节点。

## 许可证

当前发布版本按 [MIT License](LICENSE) 提供。第三方服务、接口、商标和数据分别受其权利人的条款约束。
