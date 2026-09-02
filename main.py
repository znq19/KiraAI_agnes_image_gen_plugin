"""
Agnes AI 图片生成插件
====================
基于 Agnes AI API (agnes-image-2.5-flash) 的文生图/图生图插件。
生成图片后自动发送到聊天，支持合并转发（QQ）和直接发送。
"""

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional, Dict

import aiohttp

from core.plugin import BasePlugin, logger, on, Priority, register
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat import MessageChain
from core.chat.message_elements import Image, Text
from core.provider import LLMRequest
from core.utils.path_utils import get_data_path


# ─── 风格提示词映射 ─────────────────────────────────────────────

STYLE_PROMPTS: dict[str, str] = {
    "anime": "anime illustration style, cel shading, soft lighting, highly detailed, vibrant colors",
    "realistic": "photorealistic, cinematic lighting, 8k resolution, highly detailed, sharp focus, professional photography",
    "oil_painting": "oil painting style, visible brush strokes, classical art composition, rich texture and depth",
    "watercolor": "watercolor painting, soft edges, flowing colors, artistic, delicate washes, paper texture",
}

# ─── 可用尺寸 ────────────────────────────────────────────────────

# 档位式尺寸（Agnes Image 2.1/2.5 Flash 推荐，配合 ratio 使用）
SIZE_TIERS: list[str] = ["1K", "2K", "3K", "4K"]

SIZE_TIER_LABELS: dict[str, str] = {
    "1K": "1K（约1024px）",
    "2K": "2K（约2048px）",
    "3K": "3K（约3072px）",
    "4K": "4K（约4096px）",
}

# 全部合法尺寸（档位；自定义宽高由插件配置 custom_width/custom_height 提供）
SIZE_OPTIONS: list[str] = SIZE_TIERS

SIZE_LABELS: dict[str, str] = {
    **SIZE_TIER_LABELS,
}

# 宽高比（配合档位式尺寸）
RATIO_OPTIONS: list[str] = [
    "1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9",
]

RATIO_LABELS: dict[str, str] = {
    "1:1": "正方形",
    "3:4": "竖图",
    "4:3": "横图",
    "16:9": "宽屏",
    "9:16": "手机竖屏",
    "2:3": "竖图 2:3",
    "3:2": "横图 3:2",
    "21:9": "超宽屏",
}

STYLE_LABELS: dict[str, str] = {
    "anime": "动漫",
    "realistic": "写实",
    "oil_painting": "油画",
    "watercolor": "水彩",
}


class AgnesImageGenPlugin(BasePlugin):
    """Agnes AI 图片生成插件

    通过 Agnes AI API 生成高质量图片，支持：
    - 文生图：纯文本描述生成图片
    - 图生图：基于参考图片 URL 生成变体
    - 多种风格：动漫 / 写实 / 油画 / 水彩
    - 尺寸档位：1K / 2K / 3K / 4K（配合 8 种宽高比），支持自定义宽高
    - 发送模式：合并转发（QQ）/ 逐张直接发送
    """

    # ── 生命周期 ─────────────────────────────────────────────────

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # API 设置
        api_sec = cfg.get("section_api", {})
        self.api_key: str = api_sec.get("api_key", "")
        self.api_base: str = api_sec.get(
            "api_base", "https://apihub.agnes-ai.com/v1/images/generations"
        )
        self.model: str = api_sec.get("model", "agnes-image-2.5-flash")
        self.timeout: int = max(5, min(360, api_sec.get("timeout", 180)))

        # 生成设置
        gen_sec = cfg.get("section_generation", {})
        self.default_size: str = gen_sec.get("default_size", "2K")
        if self.default_size not in SIZE_OPTIONS:
            self.default_size = "2K"
        self.default_ratio: str = gen_sec.get("default_ratio", "16:9")
        if self.default_ratio not in RATIO_OPTIONS:
            self.default_ratio = "16:9"
        # 自定义宽高（可选）：填了则 LLM 传 custom_size 时使用，宽高分开配置
        self.custom_width: int = max(0, int(gen_sec.get("custom_width", 0) or 0))
        self.custom_height: int = max(0, int(gen_sec.get("custom_height", 0) or 0))
        if self.custom_width and self.custom_height:
            self.custom_size: str = f"{self.custom_width}x{self.custom_height}"
        else:
            self.custom_size: str = ""
        self.default_style: str = gen_sec.get("default_style", "anime")
        if self.default_style not in STYLE_PROMPTS:
            self.default_style = "anime"
        self.max_count: int = max(1, min(10, gen_sec.get("max_count", 4)))

        # 异步生成：工具快速返回，后台生成+发送，完成后 publish_notice 通知 LLM 接话（默认开）
        self.async_generate: bool = gen_sec.get("async_generate", True)

        # 自我形象参考图：优先用插件配置，未配置则自动读取 KiraAI 系统设置
        self.selfie_image_path: str = gen_sec.get("selfie_image_path", "")
        if not self.selfie_image_path:
            try:
                kira_selfie = (
                    self.ctx.config.get("bot_config", {})
                    .get("selfie", {})
                    .get("path", "")
                )
                if kira_selfie and kira_selfie != "None":
                    self.selfie_image_path = str(kira_selfie)
                    logger.info(
                        "[agnes_image_gen] 自动读取 KiraAI 系统设置中的"
                        f" Bot 角色形象参考图: {self.selfie_image_path}"
                    )
            except Exception:
                pass

        # 发送设置
        send_sec = cfg.get("section_sending", {})
        self.send_as_forward: bool = send_sec.get("send_as_forward", True)
        self.image_storage_dir: str = send_sec.get("image_storage_dir", "files/agnes")
        self.save_generated: bool = send_sec.get("save_generated", True)
        self.max_cache_files: int = send_sec.get("max_cache_files", 100)
        self.proxy: str = send_sec.get("proxy", "")

        self._storage_dir: Optional[Path] = None
        # 后台生成任务（按 sid 管理）：异步模式下工具快速返回，生成完成后 publish_notice 通知 LLM
        self._gen_tasks: Dict[str, asyncio.Task] = {}

    async def initialize(self):
        """初始化存储目录，验证配置，清理缓存"""
        data_dir = Path(get_data_path())
        self._storage_dir = data_dir / self.image_storage_dir
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        if not self.api_key:
            logger.warning(
                "[agnes_image_gen] API Key 未配置，请在插件设置中填写 Agnes AI 密钥"
            )
        else:
            selfie_info = (
                f", 角色形象参考图={'已配置' if self.selfie_image_path else '未配置'}"
            )
            logger.info(
                "[agnes_image_gen] 已就绪 "
                f"(模型={self.model}, 默认尺寸={self.default_size}, "
                f"默认风格={self.default_style}, 合并转发={self.send_as_forward}"
                f"{selfie_info})"
            )

        await self._cleanup_cache()

    async def terminate(self):
        """清理资源（无持久连接需关闭）；取消后台生成任务"""
        for t in list(self._gen_tasks.values()):
            if not t.done():
                t.cancel()
        self._gen_tasks.clear()

    # ── 缓存管理 ─────────────────────────────────────────────────

    async def _cleanup_cache(self):
        """清理超出上限的旧缓存文件"""
        if self.max_cache_files <= 0 or not self._storage_dir:
            return
        try:
            files = sorted(
                self._storage_dir.glob("agnes_*.png"),
                key=lambda f: f.stat().st_mtime,
            )
            excess = len(files) - self.max_cache_files
            if excess > 0:
                for f in files[:excess]:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                logger.info(f"[agnes_image_gen] 清理了 {excess} 个过期缓存文件")
        except Exception as e:
            logger.warning(f"[agnes_image_gen] 缓存清理失败: {e}")

    # ── Agnes AI API 调用 ────────────────────────────────────────

    # 重试配置
    _API_MAX_RETRIES: int = 3
    _API_RETRY_DELAY: float = 5.0  # 秒

    async def _call_agnes_api(
        self,
        prompt: str,
        size: str = "1K",
        ratio: str = "1:1",
        n: int = 1,
        reference_image_urls: Optional[List[str]] = None,
    ) -> tuple[Optional[List[str]], str]:
        """调用 Agnes AI 图片生成 API（带自动重试）

        Args:
            prompt: 图片描述提示词（英文）
            size: 图片尺寸（档位 1K/2K/3K/4K 或历史精确尺寸）
            ratio: 宽高比（配合档位式尺寸，如 16:9）
            n: 生成数量
            reference_image_urls: 已解析的参考图片列表（URL 或 data URL），
                由调用方先通过 _resolve_reference_image 逐张处理

        Returns:
            (图片 URL 列表, 错误信息)。成功时错误信息为空字符串；
            失败时列表为 None，错误信息为可展示给用户的详细原因
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "n": n,
        }

        # 档位式尺寸配合 ratio；自定义精确尺寸不传 ratio（服务端自动标准化）
        if size in SIZE_TIERS:
            payload["ratio"] = ratio

        if reference_image_urls:
            payload["extra_body"] = {
                "image": reference_image_urls,
            }

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        kwargs_base = {"headers": headers, "json": payload, "timeout": timeout}
        if self.proxy:
            kwargs_base["proxy"] = self.proxy

        last_error = ""
        for attempt in range(1, self._API_MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_base, **kwargs_base) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            urls = [
                                item["url"]
                                for item in result.get("data", [])
                                if item.get("url")
                            ]
                            if not urls:
                                logger.error(
                                    "[agnes_image_gen] API 返回中没有图片 URL"
                                )
                                return None, "API 返回成功但没有图片 URL（响应中 data 为空）"
                            return urls, ""

                        error_text = await resp.text()
                        logger.warning(
                            f"[agnes_image_gen] API 返回 {resp.status} "
                            f"(第 {attempt}/{self._API_MAX_RETRIES} 次): "
                            f"{error_text[:200]}"
                        )

                        # 4xx 客户端错误不重试（401/403/400 等是参数或认证问题）
                        if 400 <= resp.status < 500 and resp.status != 429:
                            logger.error(
                                f"[agnes_image_gen] 客户端错误 {resp.status}，"
                                f"不重试: {error_text[:200]}"
                            )
                            return None, self._format_api_error(resp.status, error_text)

                        # 429 限流 / 5xx 服务端错误 → 等待后重试
                        if attempt < self._API_MAX_RETRIES:
                            delay = self._API_RETRY_DELAY * attempt
                            logger.info(
                                f"[agnes_image_gen] "
                                f"{'限流' if resp.status == 429 else '服务端异常'}，"
                                f"{delay:.0f} 秒后重试..."
                            )
                            await asyncio.sleep(delay)
                            continue

                        last_error = self._format_api_error(resp.status, error_text)
                        return None, last_error

            except asyncio.TimeoutError:
                logger.warning(
                    f"[agnes_image_gen] 请求超时 "
                    f"(第 {attempt}/{self._API_MAX_RETRIES} 次)"
                )
                if attempt < self._API_MAX_RETRIES:
                    await asyncio.sleep(self._API_RETRY_DELAY)
                    continue
                last_error = f"请求超时（{self.timeout} 秒无响应）"
                return None, last_error

            except aiohttp.ClientError as e:
                logger.warning(
                    f"[agnes_image_gen] 网络异常 (第 {attempt}/{self._API_MAX_RETRIES} 次): {e}"
                )
                if attempt < self._API_MAX_RETRIES:
                    await asyncio.sleep(self._API_RETRY_DELAY)
                    continue
                last_error = f"网络错误: {e}"
                return None, last_error

            except Exception as e:
                logger.error(f"[agnes_image_gen] API 调用异常: {e}")
                return None, f"API 调用异常: {e}"

        logger.error(
            f"[agnes_image_gen] 重试 {self._API_MAX_RETRIES} 次后仍失败: {last_error}"
        )
        return None, last_error or "未知错误"

    @staticmethod
    def _format_api_error(status: int, error_text: str) -> str:
        """把 API 错误响应整理成可读的错误信息（供 LLM 转述给用户）"""
        detail = error_text.strip()[:300]
        # 尝试从 JSON 错误体里提取 message 字段
        try:
            parsed = json.loads(error_text)
            if isinstance(parsed, dict):
                msg = parsed.get("message")
                if not msg:
                    err_obj = parsed.get("error")
                    if isinstance(err_obj, dict):
                        msg = err_obj.get("message")
                if msg:
                    detail = str(msg)[:300]
        except (json.JSONDecodeError, AttributeError):
            pass

        hints = {
            400: "请求参数有误（可能是尺寸/宽高比组合不被支持）",
            401: "API Key 无效或已过期",
            403: "API Key 无权限访问该模型",
            404: "接口地址或模型名称不存在",
            429: "请求过于频繁，已被限流",
        }
        hint = hints.get(status, "")
        return f"HTTP {status} {hint}：{detail}" if hint else f"HTTP {status}：{detail}"

    @staticmethod
    def _resolve_local_reference_path(reference: str) -> Optional[Path]:
        """把参考图路径解析为真实存在的本地文件（多候选）

        KiraAI 生态中 LLM 可能给出几种相对路径写法：
        - ``data/temp/xxx.jpg``  → 相对 KiraAI 根目录（含 data 前缀）
        - ``temp/xxx.jpg``       → 相对 data/ 目录（无 data 前缀）
        - 绝对路径 ``C:\\...\\data\\temp\\xxx.jpg``

        逐个尝试，返回第一个存在的文件；都不存在返回 None。
        """
        if not reference:
            return None

        ref = Path(reference)
        if ref.is_absolute():
            return ref if ref.is_file() else None

        data_dir = Path(get_data_path())
        root_dir = data_dir.parent  # KiraAI 根目录
        rel = reference.replace("\\", "/")

        candidates: List[Path] = []
        # 候选1: 相对 data/ 目录，如 "temp/xxx.jpg"
        candidates.append(data_dir / rel)
        # 候选2: 去掉 data/ 前缀后相对 data/ 目录，如 "data/temp/xxx.jpg" → "temp/xxx.jpg"
        if rel.startswith("data/"):
            candidates.append(data_dir / rel[len("data/"):])
        # 候选3: 相对根目录（含 data 前缀），如 "data/temp/xxx.jpg"
        candidates.append(root_dir / rel)

        for cand in candidates:
            if cand.is_file():
                return cand
        return None

    async def _resolve_reference_image(self, reference: str) -> Optional[str]:
        """解析参考图片来源，支持 URL 和本地文件路径

        本地文件路径会被自动解析为绝对路径并转为 base64 data URL，
        以兼容 Agnes API 的 image 参数。

        Returns:
            可用的 URL 字符串，解析失败返回 None
        """
        if not reference:
            return None

        # 已经是 URL，直接返回
        if reference.startswith(("http://", "https://", "data:")):
            return reference

        # 本地文件路径：多候选解析
        try:
            ref_path = self._resolve_local_reference_path(reference)
            if ref_path is None:
                logger.warning(
                    f"[agnes_image_gen] 参考图文件不存在: {reference}"
                )
                return None

            # 读取并转换为 base64 data URL
            img_data = ref_path.read_bytes()
            suffix = ref_path.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
            }.get(suffix, "image/png")
            b64 = base64.b64encode(img_data).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"
            logger.info(
                f"[agnes_image_gen] 本地参考图已转换: {ref_path} "
                f"({len(img_data)} bytes)"
            )
            return data_url
        except Exception as e:
            logger.error(f"[agnes_image_gen] 参考图解析失败: {e}")
            return None

    # ── 图片下载 ─────────────────────────────────────────────────

    async def _download_image(self, url: str, filename: str) -> tuple[Optional[str], str]:
        """下载单张图片到本地存储目录

        Returns:
            (本地绝对路径, 错误信息)。成功时错误信息为空字符串
        """
        timeout = aiohttp.ClientTimeout(total=60)
        kwargs: dict = {"timeout": timeout}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, **kwargs) as resp:
                    if resp.status != 200:
                        err = f"下载失败 HTTP {resp.status}: {url[:100]}"
                        logger.error(f"[agnes_image_gen] {err}")
                        return None, err

                    data = await resp.read()
                    local_path = self._storage_dir / filename
                    local_path.write_bytes(data)
                    return str(local_path.absolute()), ""
        except asyncio.TimeoutError:
            err = f"下载超时: {url[:100]}"
            logger.error(f"[agnes_image_gen] {err}")
            return None, err
        except Exception as e:
            err = f"下载异常: {e} ({url[:100]})"
            logger.error(f"[agnes_image_gen] {err}")
            return None, err

    # ── Session ID 构造 ──────────────────────────────────────────

    @staticmethod
    def _get_sid(event: KiraMessageBatchEvent) -> str:
        """从批量事件中提取 session_id

        格式: {adapter_name}:{session_type}:{target_id}
        例: qq:gm:123456789 或 qq:dm:987654321
        """
        if event.sid:
            return event.sid

        adapter = event.adapter.name if event.adapter else "qq"

        if event.messages:
            last_msg = event.messages[-1]
            if hasattr(last_msg, "group") and last_msg.group:
                return f"{adapter}:gm:{last_msg.group.group_id}"
            else:
                sender_id = (
                    last_msg.sender.user_id if hasattr(last_msg, "sender") and last_msg.sender else "0"
                )
                return f"{adapter}:dm:{sender_id}"

        return f"{adapter}:dm:0"

    # ── 后台任务通知 ────────────────────────────────────────────

    async def _publish_notice(self, sid: str, text: str) -> None:
        """publish_notice：构造合成事件进主线路，LLM 接话完成回复（消息合并进会话）。

        用于后台生成任务完成后，让 LLM 补一句"图片已生成"并自然合并消息。
        """
        try:
            await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=True)
        except Exception:
            logger.exception("[agnes_image_gen] publish_notice failed")

    # ── 消息发送 ─────────────────────────────────────────────────

    async def _send_image_directly(
        self, event: KiraMessageBatchEvent, local_path: str
    ) -> bool:
        """通过 MessageChain 直接发送单张图片"""
        try:
            sid = self._get_sid(event)
            img = Image(local_path, caption="")
            chain = MessageChain([img])
            result = await self.ctx.message_processor.send_message_chain(sid, chain)
            return result.ok
        except Exception as e:
            logger.error(f"[agnes_image_gen] 直接发送失败: {e}")
            return False

    async def _send_forward_images(
        self, event: KiraMessageBatchEvent, local_paths: List[str]
    ) -> bool:
        """以合并转发形式发送多张图片（仅 QQ 平台）

        参考 pixiv_image_searcher 插件的实现模式。
        非 QQ 平台或不支持时返回 False，调用方应回退到直接发送。
        """
        # 平台筛选：合并转发仅 QQ 支持
        if event.adapter.platform != "QQ":
            logger.debug("[agnes_image_gen] 非 QQ 平台，跳过合并转发")
            return False

        sid = self._get_sid(event)
        try:
            parts = sid.split(":")
            if len(parts) < 3:
                return False
            adapter_name = parts[0]
            session_type = parts[1]
            target_id = parts[2]
        except (ValueError, IndexError):
            return False

        if session_type not in ("gm", "dm"):
            return False

        # 获取 QQ 客户端
        try:
            adapter_inst = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if not adapter_inst:
                logger.error(f"[agnes_image_gen] 未找到适配器: {adapter_name}")
                return False
            client = adapter_inst.get_client()
            if not client:
                logger.error(f"[agnes_image_gen] 适配器 {adapter_name} 无可用客户端")
                return False
        except Exception as e:
            logger.error(f"[agnes_image_gen] 获取适配器失败: {e}")
            return False

        # 获取机器人信息
        adapter_config = adapter_inst.config if hasattr(adapter_inst, "config") else {}
        bot_nick = (
            adapter_config.get("nickname", "")
            or adapter_config.get("bot_name", "")
            or "Kira"
        )

        if event.messages:
            last_msg = event.messages[-1]
            self_id = str(last_msg.self_id) if last_msg.self_id else str(event.self_id)
        else:
            self_id = str(event.self_id) if hasattr(event, "self_id") else ""

        # 构造合并转发节点
        nodes = []
        for path in local_paths:
            abs_path = os.path.abspath(path)
            nodes.append({
                "type": "node",
                "data": {
                    "name": bot_nick,
                    "uin": self_id,
                    "content": [
                        {"type": "image", "data": {"file": abs_path}}
                    ],
                },
            })

        # 发送合并转发消息
        try:
            if session_type == "gm":
                await client.send_action(
                    "send_forward_msg",
                    {"group_id": int(target_id), "messages": nodes},
                )
            else:
                await client.send_action(
                    "send_forward_msg",
                    {"user_id": int(target_id), "messages": nodes},
                )
            logger.info(
                f"[agnes_image_gen] 合并转发成功 ({len(local_paths)} 张) -> {sid}"
            )
            return True
        except Exception as e:
            logger.error(f"[agnes_image_gen] 合并转发失败: {e}")
            return False

    # ── 下载 + 发送（统一入口）──────────────────────────────────

    async def _download_and_send(
        self, event: KiraMessageBatchEvent, image_urls: List[str]
    ) -> tuple[List[str], str]:
        """下载图片并发送到聊天

        流程: 逐张下载 → 按配置选择发送方式 → 返回成功路径列表

        Returns:
            (成功下载的本地路径列表, 错误信息)。全部成功时错误信息为空字符串
        """
        local_paths: List[str] = []
        errors: List[str] = []

        for i, url in enumerate(image_urls):
            timestamp = int(time.time() * 1000)
            filename = f"agnes_{timestamp}_{i}.png"
            local_path, err = await self._download_image(url, filename)
            if local_path:
                local_paths.append(local_path)
            else:
                errors.append(err or f"第 {i + 1} 张图片下载失败")
                logger.warning(f"[agnes_image_gen] 第 {i + 1} 张图片下载失败: {err}")

        if not local_paths:
            return [], "；".join(errors) or "所有图片下载失败"

        # 选择发送方式
        if self.send_as_forward and len(local_paths) > 1:
            ok = await self._send_forward_images(event, local_paths)
            if not ok:
                logger.info("[agnes_image_gen] 合并转发失败，回退到逐张直接发送")
                for path in local_paths:
                    if not await self._send_image_directly(event, path):
                        errors.append(f"直接发送失败: {path}")
        else:
            for path in local_paths:
                if not await self._send_image_directly(event, path):
                    errors.append(f"直接发送失败: {path}")

        # 不保留则删除本地文件
        if not self.save_generated:
            for path in local_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

        return local_paths, "；".join(errors)

    # ── LLM 工具 ─────────────────────────────────────────────────

    @register.tool(
        name="agnes_image_gen",
        description=(
            "通过 Agnes AI 生成高质量图片并自动发送到聊天。"
            "当用户要求生成图片、画图、AI 绘图、文生图、图生图时调用此工具。"
            "支持多种风格（动漫/写实/油画/水彩）和多种尺寸（1K~4K 档位 + 8 种宽高比）。"
            "如果用户提供了参考图片的 URL，使用 reference_image_url 参数进入图生图模式。"
            "如果用户要求看 Bot 角色自身的形象图/自拍（如「你长什么样」「发张自拍」「看看你的样子」），"
            "将 use_selfie 设为 true，工具会自动使用 Bot 角色自身的形象参考图进行图生图。"
            "图片生成和发送全自动完成，你只需告知用户结果即可，不要再用 <file> 标签发图。"
            "生成需要数十秒，工具会快速返回，图片生成完成后会自动发送并通知你，"
            "收到通知后再告知用户图片已生成。"
        ),
        params={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "详细的英文图片描述提示词。"
                        "如果用户用中文描述，务必翻译并扩写成富有细节的英文提示词，"
                        "包括构图、风格、光照、色彩、背景等要素。"
                        "默认风格为动漫插画。"
                        "如果是生成 Bot 角色自身的形象图，用 'the character' 指代角色，"
                        "不要描述外貌特征（参考图已有）。"
                    ),
                },
                "size": {
                    "type": "string",
                    "description": (
                        "图片尺寸档位。可选: 1K(约1024px), 2K(约2048px), "
                        "3K(约3072px), 4K(约4096px)。"
                        "默认 2K。用户要求高清/超清/4K 画质时用 2K 或 4K。"
                        "如需精确尺寸，用 custom_size 参数（宽x高）。"
                    ),
                    "default": "2K",
                },
                "ratio": {
                    "type": "string",
                    "description": (
                        "图片宽高比（配合 size 档位使用）。可选: "
                        "1:1(正方形), 3:4(竖图), 4:3(横图), 16:9(宽屏), "
                        "9:16(手机竖屏), 2:3, 3:2, 21:9(超宽屏)。"
                        "默认 16:9。用户说竖图/横图/宽屏/手机壁纸时选择对应比例。"
                        "使用 custom_size 精确尺寸时此参数无效。"
                    ),
                    "default": "16:9",
                },
                "custom_size": {
                    "type": "string",
                    "description": (
                        "自定义精确尺寸，格式 宽x高（如 1024x768）。"
                        "仅当用户明确要求特定分辨率时使用；"
                        "未配置或格式非法时回退到 size 档位。"
                    ),
                },
                "style": {
                    "type": "string",
                    "description": (
                        "图片风格。可选: anime(动漫), realistic(写实), "
                        "oil_painting(油画), watercolor(水彩)。默认 anime。"
                    ),
                    "default": "anime",
                },
                "count": {
                    "type": "integer",
                    "description": "生成图片数量，1~4。默认 1。",
                    "default": 1,
                },
                "reference_image_url": {
                    "type": "string",
                    "description": (
                        "参考图片，用于图生图模式。支持多张，用英文逗号分隔（最多 4 张）。"
                        "每张支持三种形式："
                        "1) http(s):// 图片 URL；"
                        "2) KiraAI 相对路径（如 data/temp/xxx.jpg，消息上下文中图片的 file_path 就是这种）；"
                        "3) 绝对路径。"
                        "当用户发来图片并说「基于这张图」「图生图」「参考这张图」时，"
                        "把该图片在消息上下文中的 file_path 填到这里。"
                        "若用户想「角色形象 + 指定参考图」一起作为参考，可同时将 use_selfie 设为 true。"
                        "（纯角色形象图/自拍场景用 use_selfie=true 即可，不必填此参数）"
                    ),
                },
                "use_selfie": {
                    "type": "boolean",
                    "description": (
                        "是否使用 Bot 角色自身的形象参考图进行图生图。"
                        "当用户要求看 Bot 角色自身的图片（即「你」的图片），如说"
                        "「你长什么样」「发张自拍」「发张你的照片」「看看你的样子」"
                        "「你的二次元形象」「你换个场景/衣服看看」等时，设为 true。"
                        "可以和其他参考图同时使用（如「用你的形象加上这张图的场景」），"
                        "此时形象参考图会被放在参考图列表首位。"
                    ),
                },
            },
            "required": ["prompt"],
        },
    )
    async def agnes_image_gen(
        self,
        event: KiraMessageBatchEvent,
        prompt: str,
        size: Optional[str] = None,
        ratio: Optional[str] = None,
        custom_size: Optional[str] = None,
        style: Optional[str] = None,
        count: int = 1,
        reference_image_url: str = "",
        use_selfie: bool = False,
    ) -> str:
        """Agnes AI 图片生成工具

        由 LLM 通过 function calling 自动调用。
        生成图片 → 下载 → 发送 → 返回结果提示给 LLM。
        """
        # 配置校验
        if not self.api_key:
            return (
                "错误：Agnes AI API Key 未配置。"
                "请管理员在 KiraAI WebUI 的插件设置中填写 API Key。"
            )

        # 参数校验：未传或非法时回退到插件配置的默认值
        if size is None or size not in SIZE_OPTIONS:
            size = self.default_size
        if ratio is None or ratio not in RATIO_OPTIONS:
            ratio = self.default_ratio
        if style is None or style not in STYLE_PROMPTS:
            style = self.default_style
        count = max(1, min(self.max_count, count))

        # 自定义精确尺寸：LLM 传了合法值优先用；否则用插件配置的宽高；都没有则用档位
        custom_size = (custom_size or "").strip().lower()
        if not re.fullmatch(r"\d{2,5}x\d{2,5}", custom_size):
            custom_size = self.custom_size

        # 收集参考图列表（use_selfie 与 reference_image_url 可叠加）
        raw_refs: List[str] = []
        if use_selfie:
            if not self.selfie_image_path:
                return (
                    "错误：未配置自我形象参考图。"
                    "请在 KiraAI 系统设置或插件设置中配置形象参考图路径后重试。"
                )
            raw_refs.append(self.selfie_image_path)
        if reference_image_url:
            for part in reference_image_url.split(","):
                part = part.strip()
                if part:
                    raw_refs.append(part)

        # 参考图数量上限（Agnes API / litellm 对 image 数组有限制）
        if len(raw_refs) > 4:
            return (
                "错误：参考图最多支持 4 张，当前收到 "
                f"{len(raw_refs)} 张。请减少参考图数量后重试。"
            )

        # 解析每张参考图（URL 直通 / 本地路径转 data URL），失败显式报错
        ref_urls: List[str] = []
        for ref in raw_refs:
            resolved = await self._resolve_reference_image(ref)
            if resolved is None:
                return (
                    "错误：参考图解析失败，找不到文件："
                    f"{ref}\n"
                    "支持的形式：\n"
                    "- http(s):// 图片 URL\n"
                    "- KiraAI 相对路径（如 data/temp/xxx.jpg 或 temp/xxx.jpg）\n"
                    "- 绝对路径（如 C:/.../data/temp/xxx.jpg）\n"
                    "如果用户刚发来图片，请优先使用消息上下文中记录的 file_path。"
                )
            ref_urls.append(resolved)

        # 构建完整提示词
        style_prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS["anime"])
        full_prompt = f"{prompt}, {style_prompt}"

        if use_selfie:
            mode_str = "角色形象图生图"
        elif ref_urls:
            mode_str = "图生图"
        else:
            mode_str = "文生图"
        style_label = STYLE_LABELS.get(style, style)
        ratio_label = RATIO_LABELS.get(ratio, ratio)

        # 实际请求尺寸：自定义精确尺寸优先，否则用档位（档位才带 ratio）
        req_size = custom_size if custom_size else size
        use_ratio = size in SIZE_TIERS and not custom_size
        if custom_size:
            size_desc = f"{custom_size}（自定义）"
        else:
            size_desc = f"{SIZE_LABELS.get(size, size)}（{ratio_label}）"

        logger.info(
            f"[agnes_image_gen] 开始生成: "
            f"模式={mode_str}, 风格={style_label}, 尺寸={size_desc}, "
            f"数量={count}, 参考图={len(ref_urls)}张, prompt={full_prompt[:100]}..."
        )

        # 同步模式（async_generate=false）：保持原行为，工具等生成完再返回
        if not self.async_generate:
            urls, err = await self._call_agnes_api(
                prompt=full_prompt,
                size=req_size,
                ratio=ratio if use_ratio else "1:1",
                n=count,
                reference_image_urls=ref_urls if ref_urls else None,
            )
            if not urls:
                return (
                    "生成失败：Agnes AI API 调用失败（已自动重试 3 次）。\n"
                    f"具体错误：{err}\n"
                    "请把具体错误转述给用户，并建议："
                    "检查 API Key 是否有效、账户余额是否充足，或稍等 10~20 秒后再试。"
                )
            sent_paths, dl_err = await self._download_and_send(event, urls)
            if not sent_paths:
                return (
                    "生成失败：API 返回了图片链接，但所有图片下载或发送均失败。\n"
                    f"具体错误：{dl_err}\n"
                    "请把具体错误转述给用户，并建议检查网络连接是否正常。"
                )
            send_mode = (
                "合并转发"
                if (self.send_as_forward and len(sent_paths) > 1)
                else "直接发送"
            )
            paths_str = "\n".join(f"  - {p}" for p in sent_paths)
            return (
                f"已成功以「{send_mode}」发送 {len(sent_paths)} 张图片到当前聊天。\n"
                f"──────────────────────\n"
                f"模式：{mode_str}  风格：{style_label}  尺寸：{size_desc}\n"
                f"──────────────────────\n"
                f"这些图片已由工具直接发送完毕，你无需再次发送，也禁止使用 <file> 标签。\n"
                f"请用中文简短告知用户图片已生成即可，不要重复描述图片内容。\n"
                f"生成的文件：\n{paths_str}"
            )

        # 异步模式（默认）：工具快速返回，后台生成+下载+发送，完成后 publish_notice 通知 LLM 接话
        sid = self._get_sid(event)
        if sid in self._gen_tasks and not self._gen_tasks[sid].done():
            return "⏳ 该会话已有图片生成任务在进行，完成后会自动发送，请告知用户稍候，不要重复请求。"

        async def _run():
            try:
                urls, err = await self._call_agnes_api(
                    prompt=full_prompt,
                    size=req_size,
                    ratio=ratio if use_ratio else "1:1",
                    n=count,
                    reference_image_urls=ref_urls if ref_urls else None,
                )
                if not urls:
                    await self._publish_notice(
                        sid,
                        "系统通知：图片生成失败（Agnes API 调用失败，已自动重试3次）。\n"
                        f"具体错误：{err}\n"
                        "请把具体错误转述给用户，并建议检查 API Key 是否有效、"
                        "账户余额是否充足，或稍等 10~20 秒后再试，不要反复立即重试。",
                    )
                    return
                sent, dl_err = await self._download_and_send(event, urls)
                if not sent:
                    await self._publish_notice(
                        sid,
                        "系统通知：图片生成成功但下载/发送失败。\n"
                        f"具体错误：{dl_err}\n"
                        "请把具体错误转述给用户，并建议检查网络连接是否正常。",
                    )
                    return
                send_mode = (
                    "合并转发"
                    if (self.send_as_forward and len(sent) > 1)
                    else "直接发送"
                )
                paths_str = "\n".join(f"  - {p}" for p in sent)
                await self._publish_notice(
                    sid,
                    f"系统通知：{mode_str}完成，{len(sent)} 张图片已以「{send_mode}」发送到聊天，"
                    f"尺寸 {size_desc}。"
                    "请用一两句话告知用户图片已生成，不要重复发送、不要使用 <file> 标签。"
                    f"生成的文件：\n{paths_str}",
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[agnes_image_gen] 后台生成失败")
                await self._publish_notice(
                    sid, "系统通知：图片生成失败，请告知用户稍后重试。")
            finally:
                self._gen_tasks.pop(sid, None)

        task = asyncio.create_task(_run())
        self._gen_tasks[sid] = task
        return (
            f"✅ 已开始生成图片（{mode_str}，{style_label}，{size_desc}），需要一点时间，"
            "完成后会自动发送到聊天，请告知用户稍候。"
        )

    # ── Prompt 注入 ──────────────────────────────────────────────

    @on.llm_request()
    async def inject_tool_hint(self, event, req: LLMRequest, tag_set, *_):
        """向 LLM 系统提示注入 agnes_image_gen 工具的使用说明"""
        selfie_note = ""
        if self.selfie_image_path:
            selfie_note = (
                "- **Bot 角色自身的形象参考图已配置**。"
                "用户要求看 Bot 角色自身（即「你」）的图片时，将 use_selfie 设为 true——"
                "例如用户说「你长什么样」「发张你的自拍」「看看你的样子」「你的形象图」"
                "「你换个场景/衣服/姿势」「你拍张照」等。"
                "可与 reference_image_url 同时使用（如「用你的形象 + 这张图的场景」）。"
                "prompt 中用 'the character' 指代 Bot 角色，不要描述外貌特征（参考图已有）。\n"
            )

        for p in req.system_prompt:
            if p.name == "tools":
                hint = (
                    "\n## agnes_image_gen - 图片生成\n"
                    '- 用户说"画一张""生图""AI画图""生成图片"等 -> 调用此工具\n'
                    "- **必须**把中文提示词翻译并扩写为详细的英文提示词（描述构图、风格、光照、色彩等）\n"
                    "- 默认风格是动漫插画，用户可指定写实/油画/水彩\n"
                    "- 尺寸用档位：1K/2K(默认)/3K/4K，配合 ratio 宽高比（1:1/3:4/4:3/16:9(默认)/9:16/2:3/3:2/21:9）。"
                    "用户要求高清/超清/4K 画质时用 2K 或 4K；说竖图/横图/宽屏/壁纸时选对应 ratio；"
                    "用户明确指定分辨率（如 1920x1080）时用 custom_size 参数（宽x高）\n"
                    "- 图生图时：把用户刚发图片的 file_path（如 data/temp/xxx.jpg）"
                    "填到 reference_image_url，本地路径会被自动处理；"
                    "多张参考图用英文逗号分隔（最多 4 张）\n"
                    + selfie_note +
                    "- 图片由工具自动生成并发送到聊天，你只需回复简短确认，**严禁用 <file> 标签再次发图**\n"
                )
                p.content += hint
                break
