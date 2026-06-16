import asyncio
import os
import re
import shutil
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_jmcomic",
    "hect0x7 / adapted-for-astrbot",
    "禁漫天堂(JM)本子下载插件，支持下载为 PDF / ZIP(可加密) 并发回，支持搜索车号",
    "1.0.0",
    "https://github.com/hect0x7/JMComic-Crawler-Python",
)
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 下载根目录：默认放在插件的 data 目录下，避免污染工作目录
        download_dir = (self.config.get("download_dir") or "").strip()
        if not download_dir:
            download_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "data", "jmcomic_download"
            )
        self.download_root = download_dir
        os.makedirs(self.download_root, exist_ok=True)

        # 并发闸门：限制同时进行的下载任务数
        try:
            max_concurrent = int(self.config.get("max_concurrent", 2))
        except (TypeError, ValueError):
            max_concurrent = 2
        self._sema = asyncio.Semaphore(max(1, max_concurrent))

        logger.info("[jmcomic] 插件已加载，下载目录: %s", self.download_root)

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """
        禁漫天堂本子下载。用法：
          jm <车号>            按默认格式下载
          jm pdf <车号>        下载并转 PDF
          jm zip <车号>        下载并打包 ZIP
          jm search <关键词>   搜索本子
          jm rank [日|周|月]   查看排行榜
          jm help              查看帮助
        """
        sub = (arg1 or "").strip().lower()

        if not sub or sub == "help":
            yield event.plain_result(self._help_text())
            return

        if sub in ("rank", "ranking", "排行", "排行榜"):
            async for r in self._do_rank(event, arg2):
                yield r
            return

        if sub == "pdf":
            async for r in self._do_download(event, arg2, fmt="pdf"):
                yield r
            return

        if sub == "zip":
            async for r in self._do_download(event, arg2, fmt="zip"):
                yield r
            return

        if sub == "search":
            # 关键词可能含空格，从原始消息中还原 search 之后的全部内容
            keyword = self._tail_after(event.message_str, "search") or arg2
            async for r in self._do_search(event, keyword):
                yield r
            return

        # 没有子指令关键字 → 把 arg1 当作车号，按默认格式下载
        fmt = (self.config.get("default_format") or "pdf").strip().lower()
        if fmt not in ("pdf", "zip"):
            fmt = "pdf"
        async for r in self._do_download(event, arg1, fmt=fmt):
            yield r

    # ------------------------------------------------------------------ #
    # 下载核心
    # ------------------------------------------------------------------ #
    async def _do_download(self, event: AstrMessageEvent, raw_id: str, fmt: str):
        album_id = self._extract_id(raw_id)
        if not album_id:
            yield event.plain_result("❌ 未识别到有效车号，请输入数字，例如：jm 350234")
            return

        yield event.plain_result(f"🚀 开始下载 JM{album_id}（{fmt.upper()}），请稍候…")

        async with self._sema:
            task_dir = os.path.join(self.download_root, f"task_{album_id}_{int(time.time()*1000)}")
            os.makedirs(task_dir, exist_ok=True)
            try:
                timeout = self._int_cfg("download_timeout", 300)
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._download_blocking, album_id, fmt, task_dir),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                self._safe_rmtree(task_dir)
                yield event.plain_result(f"⏱️ 下载 JM{album_id} 超时，请稍后重试或换用其它车号。")
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("[jmcomic] 下载 JM%s 失败", album_id)
                self._safe_rmtree(task_dir)
                yield event.plain_result(f"❌ 下载 JM{album_id} 失败：{e}")
                return

            out_path, title, extra_msg = result
            if not out_path or not os.path.exists(out_path):
                self._safe_rmtree(task_dir)
                yield event.plain_result(f"❌ 下载 JM{album_id} 完成但未找到产物文件。")
                return

            file_name = os.path.basename(out_path)
            caption = f"✅ JM{album_id} 《{title}》 下载完成"
            if extra_msg:
                caption += f"\n{extra_msg}"

            # 发送文件前先 copy 一份到持久目录，避免框架还没读到就被 finally 删掉
            persist_dir = os.path.join(self.download_root, "pending")
            os.makedirs(persist_dir, exist_ok=True)
            persist_path = os.path.join(persist_dir, file_name)
            try:
                shutil.copy2(out_path, persist_path)
            except Exception as e:  # noqa: BLE001
                logger.warning("[jmcomic] copy 产物失败，改用原路径: %s", e)
                persist_path = out_path

            file_comp = Comp.File(file=persist_path, name=file_name)

            try:
                if self._bool_cfg("send_as_forward", True):
                    # 以「聊天记录（合并转发）」形式发回：标题与文件各为一个节点
                    yield event.chain_result(
                        self._build_forward(event, caption, file_comp)
                    )
                else:
                    yield event.plain_result(caption)
                    yield event.chain_result([file_comp])
            finally:
                # 延迟清理：给框架充分时间读完文件
                self._safe_rmtree(task_dir)
                # 发送后再删 copy（可能框架还在读，所以延迟）
                try:
                    os.remove(persist_path)
                except FileNotFoundError:
                    pass

    def _download_blocking(self, album_id: str, fmt: str, task_dir: str):
        """在线程中执行的同步下载逻辑，返回 (产物路径, 标题, 附加消息)。"""
        import jmcomic
        from jmcomic import JmOption

        option_dict = self._build_option_dict(fmt, task_dir)
        option = JmOption.construct(option_dict)

        album, _dler = jmcomic.download_album(album_id, option)
        title = getattr(album, "title", str(album_id))

        out_path = self._find_output(task_dir, fmt)
        extra_msg = ""
        if fmt == "zip" and self._bool_cfg("zip_encrypt"):
            pwd = (self.config.get("zip_password") or "").strip()
            extra_msg = f"🔐 ZIP 密码：{pwd}" if pwd else "🔐 ZIP 已加密（随机密码见压缩包注释）"
        elif fmt == "pdf" and self._bool_cfg("pdf_encrypt"):
            pwd = (self.config.get("pdf_password") or "").strip()
            if pwd:
                extra_msg = f"🔐 PDF 密码：{pwd}"
        return out_path, title, extra_msg

    def _build_option_dict(self, fmt: str, task_dir: str) -> dict:
        """构造传给 JmOption.construct 的配置字典。"""
        impl = (self.config.get("client_impl") or "api").strip() or "api"
        suffix = (self.config.get("image_suffix") or "").strip()
        proxy = (self.config.get("proxy") or "").strip()

        meta_data = {"impersonate": "chrome", "headers": None, "proxies": None}
        if proxy:
            meta_data["proxies"] = proxy

        # 章节并发数：必须是具体整数。jmcomic 库默认 photo=None，
        # 但 jm_downloader.execute_on_condition 会执行 `count_batch >= count_real`，
        # None 参与比较会在 Python 3.12 抛 TypeError，故这里显式给整数绕开该 bug。
        photo_batch = self._int_cfg("photo_batch", 8)
        option_dict = {
            "dir_rule": {"rule": "Bd_Atitle", "base_dir": task_dir},
            "download": {
                "cache": True,
                "image": {"decode": True, "suffix": suffix or None},
                "threading": {"image": 30, "photo": max(1, photo_batch)},
            },
            "client": {
                "domain": [],
                "postman": {"type": "curl_cffi", "meta_data": meta_data},
                "impl": impl,
                "retry_times": 5,
            },
            "plugins": {
                "valid": "log",
                "after_album": [self._build_export_plugin(fmt, task_dir)],
            },
        }
        return option_dict

    def _build_export_plugin(self, fmt: str, task_dir: str) -> dict:
        """构造 after_album 阶段把图片合并为 PDF/ZIP 的插件配置。"""
        if fmt == "pdf":
            kwargs = {
                "pdf_dir": task_dir,
                "filename_rule": "Atitle",
                "delete_original_file": True,
            }
            if self._bool_cfg("pdf_encrypt"):
                pwd = (self.config.get("pdf_password") or "").strip()
                if pwd:
                    kwargs["encrypt"] = {"password": pwd}
            return {"plugin": "img2pdf", "kwargs": kwargs}

        # zip
        kwargs = {
            "zip_dir": task_dir,
            "filename_rule": "Atitle",
            "level": "album",
            "delete_original_file": True,
        }
        if self._bool_cfg("zip_encrypt"):
            pwd = (self.config.get("zip_password") or "").strip()
            if pwd:
                kwargs["encrypt"] = {"password": pwd}
            else:
                kwargs["encrypt"] = {"type": "random"}
        return {"plugin": "zip", "kwargs": kwargs}

    @staticmethod
    def _find_output(task_dir: str, fmt: str):
        """在任务目录中查找生成的 pdf/zip 产物。"""
        target_ext = ".pdf" if fmt == "pdf" else ".zip"
        candidates = []
        for root, _dirs, files in os.walk(task_dir):
            for f in files:
                if f.lower().endswith(target_ext):
                    full = os.path.join(root, f)
                    candidates.append((os.path.getmtime(full), full))
        if not candidates:
            return None
        # 取最新生成的那个
        candidates.sort(reverse=True)
        return candidates[0][1]

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    async def _do_search(self, event: AstrMessageEvent, keyword: str):
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("❌ 请输入搜索关键词，例如：jm search 无修正")
            return

        yield event.plain_result(f"🔍 正在搜索：{keyword} …")
        try:
            limit = self._int_cfg("search_limit", 10)
            lines = await asyncio.wait_for(
                asyncio.to_thread(self._search_blocking, keyword, limit),
                timeout=60,
            )
        except asyncio.TimeoutError:
            yield event.plain_result("⏱️ 搜索超时，请稍后重试。")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("[jmcomic] 搜索失败")
            yield event.plain_result(f"❌ 搜索失败：{e}")
            return

        if not lines:
            yield event.plain_result(f"😶 没有找到与「{keyword}」相关的本子。")
            return

        header = f"🔍 「{keyword}」搜索结果（前 {len(lines)} 条）：\n"
        body = "\n".join(lines)
        tip = "\n\n💡 发送 jm <车号> 即可下载"
        yield event.plain_result(header + body + tip)

    def _search_blocking(self, keyword: str, limit: int):
        import jmcomic
        from jmcomic import JmOption

        option_dict = self._build_option_dict("pdf", self.download_root)
        # 搜索不需要导出插件
        option_dict["plugins"] = {"valid": "log"}
        option = JmOption.construct(option_dict)

        client = option.new_jm_client()
        page = client.search_site(search_query=keyword, page=1)

        lines = []
        for album_id, title in page.iter_id_title():
            lines.append(f"  JM{album_id}  {title}")
            if len(lines) >= limit:
                break
        return lines

    # ------------------------------------------------------------------ #
    # 排行榜
    # ------------------------------------------------------------------ #
    # 周期别名 -> (调用的客户端方法名, 中文展示名)
    _RANK_PERIODS = {
        "日": ("day_ranking", "日"),
        "day": ("day_ranking", "日"),
        "today": ("day_ranking", "日"),
        "周": ("week_ranking", "周"),
        "week": ("week_ranking", "周"),
        "月": ("month_ranking", "月"),
        "month": ("month_ranking", "月"),
    }

    async def _do_rank(self, event: AstrMessageEvent, period_arg: str):
        period_arg = (period_arg or "").strip().lower()
        method_name, period_cn = self._RANK_PERIODS.get(
            period_arg, ("week_ranking", "周")
        )

        yield event.plain_result(f"📊 正在获取 {period_cn}排行榜 …")
        try:
            limit = self._int_cfg("search_limit", 10)
            lines = await asyncio.wait_for(
                asyncio.to_thread(self._rank_blocking, method_name, limit),
                timeout=60,
            )
        except asyncio.TimeoutError:
            yield event.plain_result("⏱️ 获取排行榜超时，请稍后重试。")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("[jmcomic] 获取排行榜失败")
            yield event.plain_result(f"❌ 获取排行榜失败：{e}")
            return

        if not lines:
            yield event.plain_result(f"😶 暂时拿不到{period_cn}排行榜数据。")
            return

        header = f"📊 禁漫{period_cn}排行榜（前 {len(lines)} 名）：\n"
        body = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
        tip = "\n\n💡 发送 jm <车号> 即可下载"
        yield event.plain_result(header + body + tip)

    def _rank_blocking(self, method_name: str, limit: int):
        import jmcomic  # noqa: F401
        from jmcomic import JmOption

        option_dict = self._build_option_dict("pdf", self.download_root)
        option_dict["plugins"] = {"valid": "log"}
        option = JmOption.construct(option_dict)

        client = option.new_jm_client()
        page = getattr(client, method_name)(page=1)

        lines = []
        for album_id, title in page.iter_id_title():
            lines.append(f"JM{album_id}  {title}")
            if len(lines) >= limit:
                break
        return lines

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _build_forward(self, event: AstrMessageEvent, caption: str, file_comp):
        """把下载结果包装成「聊天记录（合并转发）」节点列表。

        返回的 chain 由若干 Comp.Node 组成，每个 Node 是合并转发里的一条消息。
        发送方信息取机器人自身（uin / name），避免暴露真实用户。
        """
        try:
            uin = int(event.get_self_id())
        except (TypeError, ValueError):
            uin = 0
        nickname = "JMComic"

        return [
            Comp.Node(uin=uin, name=nickname, content=[Comp.Plain(caption)]),
            Comp.Node(uin=uin, name=nickname, content=[file_comp]),
        ]

    @staticmethod
    def _tail_after(message_str: str, marker: str):
        """从完整消息文本中取出 marker 之后的内容（保留空格），用于多词搜索。"""
        if not message_str:
            return ""
        low = message_str.lower()
        idx = low.find(marker.lower())
        if idx == -1:
            return ""
        return message_str[idx + len(marker):].strip()

    @staticmethod
    def _extract_id(text: str):
        """从任意文本中提取车号（连续数字）。"""
        if text is None:
            return None
        m = re.search(r"\d+", str(text))
        return m.group(0) if m else None

    def _bool_cfg(self, key: str, default: bool = False) -> bool:
        v = self.config.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(v)

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_rmtree(path: str):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _help_text() -> str:
        return (
            "📖 JMComic 下载插件指令：\n"
            "  jm <车号>           下载本子（默认格式）\n"
            "  jm pdf <车号>       下载并转为 PDF\n"
            "  jm zip <车号>       下载并打包为 ZIP\n"
            "  jm search <关键词>  搜索本子\n"
            "  jm rank [日|周|月]  查看排行榜（默认周）\n"
            "  jm help             查看本帮助\n"
            "示例：jm 350234 / jm rank 月"
        )

    async def terminate(self):
        """插件卸载/停用时调用，清理下载根目录中的残留任务。"""
        try:
            if os.path.isdir(self.download_root):
                for name in os.listdir(self.download_root):
                    if name.startswith("task_"):
                        self._safe_rmtree(os.path.join(self.download_root, name))
        except Exception:  # noqa: BLE001
            pass
