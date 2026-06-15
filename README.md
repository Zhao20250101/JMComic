# astrbot_plugin_jmcomic

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)（`jmcomic` 库）封装的 [AstrBot](https://astrbot.app) 插件，让机器人可以直接下载禁漫天堂(JM)本子，并以 **PDF** 或 **ZIP（可加密）** 的形式发回聊天。

> ⚠️ 本插件下载 NSFW 内容，请在符合当地法律与平台规则的前提下，于私有/授权环境使用。请勿一次性大量爬取，减轻 JM 服务器压力。

## 功能

- `jm <车号>`：按默认格式（配置项 `default_format`）下载本子并发回
- `jm pdf <车号>`：下载并合并为 PDF 发回
- `jm zip <车号>`：下载并打包为 ZIP 发回（按配置决定是否加密）
- `jm search <关键词>`：站内搜索，返回车号与标题列表
- `jm rank [日|周|月]`：查看排行榜（按观看量，默认周榜）
- `jm help`：查看帮助

车号支持从混合文本中提取数字，例如 `jm 350谁还没看234` 会识别为 `350234`。

## 安装

1. 把整个 `astrbot_plugin_jmcomic` 目录放入 AstrBot 的 `data/plugins/` 下；
   或在 AstrBot WebUI 的「插件管理」中通过仓库地址安装。
2. AstrBot 会自动根据 `requirements.txt` 安装依赖：
   - `jmcomic`（核心下载库）
   - `img2pdf`（PDF 合并）
   - `pikepdf`（PDF 加密，可选）
   - `pyzipper`（ZIP AES 加密，可选）
3. 在 WebUI 插件配置页按需调整配置（见下表），然后重载插件。

## 配置项（`_conf_schema.json`）

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `default_format` | 默认产物格式：`pdf` / `zip` | `pdf` |
| `download_dir` | 下载临时目录，留空用插件 `data/jmcomic_download` | 空 |
| `client_impl` | 客户端实现：`api`(移动端) / `html`(网页端) | `api` |
| `image_suffix` | 图片格式转换：空 / `.jpg` / `.png` / `.webp` | 空 |
| `zip_encrypt` | ZIP 是否加密 | `false` |
| `zip_password` | ZIP 密码，留空则随机生成并在消息中告知 | 空 |
| `pdf_encrypt` | PDF 是否加密 | `false` |
| `pdf_password` | PDF 密码 | 空 |
| `proxy` | 网络代理，如 `http://127.0.0.1:7890` | 空 |
| `search_limit` | 搜索返回条数上限 | `10` |
| `download_timeout` | 单次下载超时（秒） | `300` |
| `max_concurrent` | 同时处理的下载任务数上限 | `2` |

## 使用示例

```text
/jm 350234              # 默认格式下载
/jm pdf 350234          # 下载为 PDF
/jm zip 350234          # 下载为 ZIP（按配置加密）
/jm search 无修正        # 搜索
/jm rank 月              # 月排行榜（日/周/月，默认周）
/jm help                # 帮助
```

（指令前缀 `/` 取决于你的 AstrBot 配置，可能是 `/`、`!` 或无前缀。）

## 实现说明

- 每个下载任务在独立临时目录中进行，下载完成后通过 `jmcomic` 内置的
  `img2pdf` / `zip` 插件（挂载在 `after_album` 钩子）生成产物，发送后立即清理整个任务目录，不留缓存。
- `jmcomic.download_album` 是同步阻塞调用，已用 `asyncio.to_thread` 放入线程池执行，
  配合 `asyncio.Semaphore` 限制并发，避免阻塞 AstrBot 事件循环。
- ZIP 加密使用 AES（依赖 `pyzipper`）；PDF 加密依赖 `pikepdf`。

## 致谢

核心下载能力来自 [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)。
