from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import shutil
import time
import unicodedata
import uuid
from dataclasses import dataclass
from io import BytesIO
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol
from .models import DailyPlan, SlotCandidate


class AutoCaptchaUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    page: Any
    plan: DailyPlan
    slot: SlotCandidate
    artifacts_dir: Path
    image_bytes: bytes | None = None
    response_payload: Any = None
    token: str | None = None
    secret_key: str | None = None
    word_list: list[str] | None = None


class AutoCaptchaSolver(Protocol):
    async def solve(self, challenge: CaptchaChallenge) -> bool:
        """Return True after submitting all clicks; the caller must confirm the payment page."""
        ...


class EmptyAutoCaptchaSolver:
    async def solve(self, challenge: CaptchaChallenge) -> bool:
        del challenge
        raise AutoCaptchaUnavailable("自动验证码求解器尚未配置")


@dataclass(slots=True)
class OCRChar:
    char: str
    x: float
    y: float
    score: float


class LocalClickCaptchaSolver:
    """
    页面约定：
        #captcha-image
            验证码图片元素

        #captcha-prompt
            例如：
                请依次点击【山，月，河】

        #captcha-success
            验证成功后出现的元素
    """

    def __init__(
        self,
        image_selector: str = "#captcha-image",
        prompt_selector: str = "#captcha-prompt",
        success_selector: str = "#captcha-success",
        click_interval: float = 0.05,
        confidence_threshold: float = 0.55,
        save_debug_artifacts: bool = False,
        debug_retention_seconds: int = 3600,
    ):
        self.image_selector = image_selector
        self.prompt_selector = prompt_selector
        self.success_selector = success_selector

        self.click_interval = click_interval
        self.confidence_threshold = confidence_threshold
        self.save_debug_artifacts = save_debug_artifacts
        self.debug_retention_seconds = debug_retention_seconds
        self.last_failure_reason = ""

        if debug_retention_seconds <= 0:
            raise ValueError("debug_retention_seconds 必须大于 0")

        rapidocr = import_module("rapidocr_onnxruntime")
        self.ocr = rapidocr.RapidOCR()

    async def solve(self, challenge: CaptchaChallenge) -> bool:
        self.last_failure_reason = ""
        page = challenge.page
        initial_url = page.url

        debug_dir = (
            self._prepare_debug_dir(challenge.artifacts_dir)
            if self.save_debug_artifacts
            else None
        )
        debug_challenge = (
            CaptchaChallenge(
                page=challenge.page,
                plan=challenge.plan,
                slot=challenge.slot,
                artifacts_dir=debug_dir,
                image_bytes=challenge.image_bytes,
                response_payload=challenge.response_payload,
                token=challenge.token,
                secret_key=challenge.secret_key,
                word_list=challenge.word_list,
            )
            if debug_dir is not None
            else None
        )

        # -------------------------------------------------
        # 1. 读取点击顺序
        # -------------------------------------------------

        prompt_locator = await self._find_prompt_locator(page)
        prompt_text = await self._read_prompt_text(prompt_locator, challenge.response_payload)
        targets = self._targets_from_payload(challenge.response_payload) or challenge.word_list
        if not targets:
            self.last_failure_reason = f"无法读取目标字符，页面提示：{prompt_text!r}"
            targets = self._parse_prompt(prompt_text)

        if not targets:
            await self._save_debug_text(
                debug_challenge,
                "prompt_error.txt",
                f"无法解析验证码提示：{prompt_text}",
            )

            return False

        # -------------------------------------------------
        # 2. 获取验证码图片
        # -------------------------------------------------

        image_locator = await self._find_image_locator(page)
        response_image = challenge.image_bytes
        screenshot_bytes = response_image or await image_locator.screenshot()

        if debug_dir is not None:
            self._write_private_bytes(debug_dir / "captcha.png", screenshot_bytes)

        try:
            from PIL import Image

            image = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        except Exception as exc:
            raise AutoCaptchaUnavailable("验证码图片无法解码") from exc

        image_width, image_height = image.size

        if image_width <= 0 or image_height <= 0:
            raise AutoCaptchaUnavailable("验证码图片尺寸无效")

        # -------------------------------------------------
        # 3. OCR
        # -------------------------------------------------

        chars = await asyncio.to_thread(
            self._recognize_targets,
            screenshot_bytes,
            targets,
        )

        chars = [
            char
            for char in chars
            if self._is_valid_ocr_position(char, image_width, image_height)
        ]
        chars = self._without_footer(chars, image_width, image_height)

        debug_data = {
            "prompt": prompt_text,
            "targets": targets,
            "ocr": [
                {
                    "char": c.char,
                    "x": c.x,
                    "y": c.y,
                    "score": c.score,
                }
                for c in chars
            ],
        }

        if debug_dir is not None:
            self._write_private_text(
                debug_dir / "captcha_ocr.json",
                json.dumps(debug_data, ensure_ascii=False, indent=2),
            )

        # -------------------------------------------------
        # 4. 根据目标字符寻找坐标
        # -------------------------------------------------

        click_points: list[OCRChar] = []

        used_indices: set[int] = set()

        for target in targets:

            candidate = self._find_best_char(
                target,
                chars,
                used_indices,
            )

            if candidate is None:
                self.last_failure_reason = (
                    f"OCR 未找到目标字符 {target!r}，目标 {targets!r}，"
                    f"识别到 {[c.char for c in chars]!r}"
                )
                await self._save_debug_text(
                    debug_challenge,
                    "missing_char.txt",
                    (
                        f"未找到目标字符：{target}\n"
                        f"目标序列：{targets}\n"
                        f"OCR结果："
                        f"{[(x.char, x.score) for x in chars]}"
                    ),
                )

                return False

            index, char = candidate

            used_indices.add(index)

            click_points.append(char)

        # -------------------------------------------------
        # 5. OCR图片坐标 -> 浏览器CSS坐标
        # -------------------------------------------------

        element_box = await image_locator.bounding_box()

        if not self._is_valid_element_box(element_box):
            raise AutoCaptchaUnavailable(
                "无法获取验证码元素坐标"
            )

        success_locator = page.locator(self.success_selector)
        success_was_visible = await success_locator.is_visible()
        if success_was_visible:
            self.last_failure_reason = "求解前成功元素已经可见"
            await self._save_debug_text(
                debug_challenge,
                "success_already_visible.txt",
                "求解前成功元素已经可见，无法确认本次验证码结果",
            )
            return False

        if response_image is None and await image_locator.screenshot() != screenshot_bytes:
            self.last_failure_reason = "OCR 期间验证码图片发生变化"
            await self._save_debug_text(
                debug_challenge,
                "captcha_changed.txt",
                "OCR 完成后验证码图片发生变化，已取消点击",
            )
            return False

        # -------------------------------------------------
        # 6. 按提示顺序点击
        # -------------------------------------------------

        for point_index, point in enumerate(click_points):

            if not await self._captcha_is_current(
                page,
                image_locator,
                prompt_locator,
                expected_url=initial_url,
                    expected_targets=targets,
                    expected_prompt=prompt_text,
            ):
                self.last_failure_reason = "点击前验证码页面或提示发生变化"
                await self._save_debug_text(
                    debug_challenge,
                    "captcha_state_changed.txt",
                    "点击前页面已不再是同一轮验证码，已取消剩余点击",
                )
                return False

            element_box = await image_locator.bounding_box()
            if not self._is_valid_element_box(element_box):
                self.last_failure_reason = "验证码图片元素位置无效"
                return False

            scale_x = element_box["width"] / image_width
            scale_y = element_box["height"] / image_height

            browser_x = (
                element_box["x"]
                + point.x * scale_x
            )

            browser_y = (
                element_box["y"]
                + point.y * scale_y
            )

            if not self._is_click_inside_element(browser_x, browser_y, element_box):
                self.last_failure_reason = "点击坐标超出验证码图片"
                await self._save_debug_text(
                    debug_challenge,
                    "invalid_click_position.txt",
                    f"点击坐标无效：({browser_x}, {browser_y})",
                )
                return False

            await page.mouse.click(
                browser_x,
                browser_y,
            )

            if point_index + 1 < len(click_points):
                await asyncio.sleep(self.click_interval)

        # 点击提交后立即交回预约流程；由 _handle_captcha 确认真实支付页。
        # 目标站可能已打开支付页，但原页验证码弹窗仍停留一段时间。
        return True

    # =====================================================
    # OCR
    # =====================================================

    @staticmethod
    def _without_footer(chars: list[OCRChar], width: int, height: int) -> list[OCRChar]:
        # 目标站右下角的“场馆预约”水印，不保证被 OCR 完整识别成四个字，
        # 也不保证排在结果末尾；只过滤该区域的水印字，不截断目标字符。
        return [char for char in chars if not (
            char.char in "场馆预约" and char.x >= width * 0.65 and char.y >= height * 0.85
        )]

    def _recognize_targets(self, image_bytes: bytes, targets: list[str]) -> list[OCRChar]:
        """Retry rotated, enlarged views only when the normal OCR misses a target."""
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise AutoCaptchaUnavailable("验证码图片无法解码")
        height, width = image.shape[:2]
        chars = self._without_footer(self._run_ocr(image_bytes), width, height)

        def complete() -> bool:
            used: set[int] = set()
            for target in targets:
                candidate = self._find_best_char(target, chars, used)
                if candidate is None:
                    return False
                used.add(candidate[0])
            return True

        if complete():
            return chars
        enlarged = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        enlarged_height, enlarged_width = enlarged.shape[:2]
        for angle in (0, -30, 30, -45, 45, -15, 15):
            matrix = cv2.getRotationMatrix2D((enlarged_width / 2, enlarged_height / 2), angle, 1)
            cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
            rotated_width = int(enlarged_height * sine + enlarged_width * cosine)
            rotated_height = int(enlarged_height * cosine + enlarged_width * sine)
            matrix[0, 2] += (rotated_width - enlarged_width) / 2
            matrix[1, 2] += (rotated_height - enlarged_height) / 2
            rotated = cv2.warpAffine(
                enlarged, matrix, (rotated_width, rotated_height), borderValue=(255, 255, 255),
            )
            ok, encoded = cv2.imencode(".png", rotated)
            if not ok:
                continue
            inverse = cv2.invertAffineTransform(matrix)
            for char in self._run_ocr(encoded.tobytes()):
                x, y = inverse @ np.array([char.x, char.y, 1.0]) / 2
                candidate = OCRChar(char.char, float(x), float(y), char.score)
                if not self._is_valid_ocr_position(candidate, width, height):
                    continue
                if not self._without_footer([candidate], width, height):
                    continue
                duplicate = next((old for old in chars if old.char == candidate.char
                                  and math.hypot(old.x - candidate.x, old.y - candidate.y) < 12), None)
                if duplicate is None:
                    chars.append(candidate)
                elif candidate.score > duplicate.score:
                    duplicate.x, duplicate.y, duplicate.score = candidate.x, candidate.y, candidate.score
            if complete():
                break
        return chars

    def _run_ocr(
        self,
        image_bytes: bytes,
    ) -> list[OCRChar]:
        try:
            import cv2
            import numpy as np

            if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
                raise ValueError("empty image bytes")
            array = np.frombuffer(image_bytes, dtype=np.uint8)
            image = cv2.imdecode(array, cv2.IMREAD_COLOR)
            if image is None or getattr(image, "size", 0) == 0:
                raise ValueError("cv2.imdecode returned an empty image")

            raw_result = self.ocr(image)
            if not isinstance(raw_result, (tuple, list)) or len(raw_result) < 1:
                raise TypeError("unexpected OCR return value")
            result = raw_result[0]
            if result is None:
                return []
            if not isinstance(result, (tuple, list)):
                raise TypeError("OCR result must be a sequence")

            chars: list[OCRChar] = []
            for item in result:
                if not isinstance(item, (tuple, list)) or len(item) < 3:
                    raise TypeError("OCR item must contain box, text and score")
                box = item[0]
                text = unicodedata.normalize("NFKC", str(item[1])).strip()
                score = float(item[2])
                if not math.isfinite(score):
                    raise ValueError("OCR score is not finite")
                if score < self.confidence_threshold or not text:
                    continue

                text = "".join(c for c in text if self._is_valid_char(c))
                if not text:
                    continue
                char_centers = self._split_box(box, len(text))
                for char, center in zip(text, char_centers):
                    chars.append(
                        OCRChar(
                            char=char,
                            x=center[0],
                            y=center[1],
                            score=score,
                        )
                    )
            return chars
        except AutoCaptchaUnavailable:
            raise
        except Exception as exc:
            raise AutoCaptchaUnavailable("OCR 输入或返回格式无效") from exc

    # =====================================================
    # 一个OCR框中可能包含多个字符
    # =====================================================

    @staticmethod
    def _split_box(
        box,
        count: int,
    ) -> list[tuple[float, float]]:
        if count <= 0:
            raise AutoCaptchaUnavailable("OCR 字符数量无效")

        # RapidOCR一般返回：
        #
        # [
        #   top-left,
        #   top-right,
        #   bottom-right,
        #   bottom-left
        # ]

        try:
            if len(box) != 4 or any(len(point) < 2 for point in box):
                raise ValueError("box must contain four points")
            p0, p1, p2, p3 = (
                (float(point[0]), float(point[1]))
                for point in box
            )
            if not all(
                math.isfinite(value)
                for point in (p0, p1, p2, p3)
                for value in point
            ):
                raise ValueError("box coordinates must be finite")
        except (TypeError, ValueError, IndexError) as exc:
            raise AutoCaptchaUnavailable("OCR 坐标框格式无效") from exc

        points = []

        for i in range(count):

            t = (
                i + 0.5
            ) / count

            top_x = (
                p0[0]
                + (p1[0] - p0[0]) * t
            )

            top_y = (
                p0[1]
                + (p1[1] - p0[1]) * t
            )

            bottom_x = (
                p3[0]
                + (p2[0] - p3[0]) * t
            )

            bottom_y = (
                p3[1]
                + (p2[1] - p3[1]) * t
            )

            center_x = (
                top_x + bottom_x
            ) / 2

            center_y = (
                top_y + bottom_y
            ) / 2

            points.append(
                (
                    float(center_x),
                    float(center_y),
                )
            )

        return points

    # =====================================================
    # 提示解析
    # =====================================================

    @staticmethod
    def _parse_prompt(
        prompt: str,
    ) -> list[str]:
        """
        只接收网页提示：请依次点击【汉字，汉字，汉字】。
        """
        han = r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
        match = re.search(
            rf"请依次点击\s*[【\[]\s*({han})\s*[,，、]\s*({han})"
            rf"\s*[,，、]\s*({han})\s*[】\]]",
            prompt,
        )
        return list(match.groups()) if match else []

    # =====================================================
    # 查找字符
    # =====================================================

    @staticmethod
    def _normalize_ocr_char(value: str) -> str:
        if not isinstance(value, str):
            return ""
        return "".join(
            ch for ch in unicodedata.normalize("NFKC", value).strip() if not ch.isspace()
        )

    def _find_best_char(
        self,
        target: str,
        chars: list[OCRChar],
        used_indices: set[int],
    ) -> tuple[int, OCRChar] | None:

        normalized_target = self._normalize_ocr_char(target)
        matches = (
            (index, char)
            for index, char in enumerate(chars)
            if index not in used_indices
            and not any(math.hypot(char.x - chars[used].x, char.y - chars[used].y) < 12
                        for used in used_indices)
            and self._normalize_ocr_char(char.char) == normalized_target
        )

        # 同一个字符出现多次时，
        # 暂时选OCR置信度最高的。
        #
        # 后面可以换成：
        #
        # detection probability
        # + spatial prior
        # + prompt matching
        #
        # 的联合打分。
        return max(matches, key=lambda item: item[1].score, default=None)

    @staticmethod
    def _is_valid_ocr_position(
        char: OCRChar,
        image_width: int,
        image_height: int,
    ) -> bool:
        values = (char.x, char.y, char.score)
        return (
            image_width > 0
            and image_height > 0
            and all(math.isfinite(value) for value in values)
            and 0 <= char.x < image_width
            and 0 <= char.y < image_height
        )

    @staticmethod
    def _is_valid_element_box(box: Any) -> bool:
        if not isinstance(box, dict):
            return False
        try:
            values = tuple(float(box[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            return False
        return (
            all(math.isfinite(value) for value in values)
            and values[2] > 0
            and values[3] > 0
        )

    @staticmethod
    def _is_click_inside_element(x: float, y: float, box: dict[str, float]) -> bool:
        if not math.isfinite(x) or not math.isfinite(y):
            return False
        return (
            box["x"] <= x <= box["x"] + box["width"]
            and box["y"] <= y <= box["y"] + box["height"]
        )

    @staticmethod
    def _is_success_transition(was_visible: bool, is_visible: bool) -> bool:
        return not was_visible and is_visible

    async def _captcha_is_current(
        self,
        page: Any,
        image_locator: Any,
        prompt_locator: Any,
        *,
        expected_url: str,
        expected_targets: list[str],
        expected_prompt: str,
    ) -> bool:
        try:
            if page.url != expected_url:
                return False
            if not await image_locator.is_visible():
                return False
            if await prompt_locator.count() > 0 and await prompt_locator.is_visible():
                current_prompt = await prompt_locator.inner_text()
                return self._parse_prompt(current_prompt) in (expected_targets, [])
            return bool(expected_prompt or expected_targets)
        except Exception:
            return False

    async def _find_image_locator(self, page: Any) -> Any:
        """查找 AJ-Captcha 图片，兼容 iframe 和 CSS 背景图。"""
        selectors = (
            self.image_selector,
            ".verify-img-panel",
            ".verify-img-panel canvas",
            ".verify-img-panel img",
            "canvas",
            "img[src*='captcha']",
            "img[src^='data:image']",
            "[class*='captcha'] img",
            "[class*='verify'] img",
            "[style*='background-image']",
        )
        scopes = [page]
        main_frame = getattr(page, "main_frame", None)
        for frame in getattr(page, "frames", []):
            if frame is not main_frame:
                scopes.append(frame)

        fallback_candidates: list[tuple[float, Any]] = []
        for scope in scopes:
            for selector_index, selector in enumerate(selectors):
                try:
                    locator = scope.locator(selector)
                    count = min(await locator.count(), 20)
                except Exception:
                    continue
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        box = await candidate.bounding_box()
                        if not self._is_valid_element_box(box):
                            continue
                        width = float(box["width"])
                        height = float(box["height"])
                        if width < 120 or height < 60:
                            continue

                        # 配置选择器和 AJ-Captcha 面板优先。
                        if selector_index <= 3:
                            return candidate

                        ratio = width / height
                        if not 1.2 <= ratio <= 4.5:
                            continue
                        area = width * height
                        score = area - selector_index * 1000
                        fallback_candidates.append((score, candidate))
                    except Exception:
                        continue

        if fallback_candidates:
            return max(fallback_candidates, key=lambda item: item[0])[1]
        raise AutoCaptchaUnavailable("无法定位验证码图片元素")

    async def _find_prompt_locator(self, page: Any) -> Any:
        scopes = [page] + [
            frame for frame in getattr(page, "frames", [])
            if frame is not getattr(page, "main_frame", None)
        ]
        for scope in scopes:
            for selector in (self.prompt_selector, ".verify-bar-area .verify-msg", ".verify-bar-area"):
                locator = scope.locator(selector)
                for index in range(min(await locator.count(), 10)):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
        return page.locator(self.prompt_selector)

    @staticmethod
    async def _read_prompt_text(locator: Any, payload: Any) -> str:
        try:
            if await locator.count() > 0 and await locator.is_visible():
                return await locator.inner_text()
        except Exception:
            pass
        payloads = [payload]
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), dict):
                payloads.insert(0, payload["data"])
            rep_data = payload.get("data", {}).get("repData") if isinstance(payload.get("data"), dict) else payload.get("repData")
            if isinstance(rep_data, dict):
                payloads.insert(0, rep_data)
        for item in payloads:
            if not isinstance(item, dict):
                continue
            for key in ("wordList", "word", "clickWord", "prompt", "tip", "message"):
                value = item.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, list) and value and all(isinstance(x, str) for x in value):
                    return ",".join(value)
        return ""

    @staticmethod
    def _targets_from_payload(payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        candidates = [payload]
        if isinstance(payload.get("data"), dict):
            candidates.insert(0, payload["data"])
            rep_data = payload["data"].get("repData")
            if isinstance(rep_data, dict):
                candidates.insert(0, rep_data)
        rep_data = payload.get("repData")
        if isinstance(rep_data, dict):
            candidates.insert(0, rep_data)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ("wordList", "words", "clickWords", "targetWords"):
                value = item.get(key)
                if isinstance(value, list) and all(isinstance(x, str) and x for x in value):
                    return [x for x in value if len(x) == 1]
        return []

    # =====================================================
    # 工具函数
    # =====================================================

    @staticmethod
    def _is_valid_char(
        char: str,
    ) -> bool:

        if re.match(
            r"[\u4e00-\u9fffA-Za-z0-9]",
            char,
        ):
            return True

        return False

    def _prepare_debug_dir(self, artifacts_dir: Path) -> Path:
        base_dir = artifacts_dir.resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_expired_debug_dirs(base_dir)
        debug_dir = base_dir / f"captcha-attempt-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        debug_dir.mkdir(mode=0o700)
        self._restrict_permissions(debug_dir, 0o700)
        return debug_dir

    def _cleanup_expired_debug_dirs(self, base_dir: Path) -> None:
        cutoff = time.time() - self.debug_retention_seconds
        for candidate in base_dir.iterdir():
            if not candidate.name.startswith("captcha-attempt-"):
                continue
            try:
                resolved = candidate.resolve()
                if resolved.parent != base_dir or candidate.stat().st_mtime >= cutoff:
                    continue
                if candidate.is_symlink():
                    candidate.unlink()
                elif candidate.is_dir():
                    shutil.rmtree(candidate)
            except OSError:
                continue

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    @classmethod
    def _write_private_bytes(cls, path: Path, content: bytes) -> None:
        path.write_bytes(content)
        cls._restrict_permissions(path, 0o600)

    @classmethod
    def _write_private_text(cls, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        cls._restrict_permissions(path, 0o600)

    async def _save_debug_text(
        self,
        challenge: CaptchaChallenge | None,
        filename: str,
        content: str,
    ) -> None:
        if challenge is None:
            return
        path = challenge.artifacts_dir / Path(filename).name
        self._write_private_text(path, content)


def _extract_repdata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    candidates: list[Any] = [payload]
    if isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
        rep_data = payload["data"].get("repData")
        if isinstance(rep_data, dict):
            candidates.append(rep_data)
    if isinstance(payload.get("repData"), dict):
        candidates.append(payload["repData"])
    for item in candidates:
        if isinstance(item, dict) and isinstance(item.get("repData"), dict):
            return item["repData"]
    return {}


def extract_clickword_fields(payload: Any) -> dict[str, Any]:
    rep_data = _extract_repdata(payload)
    word_list: list[str] = []
    if isinstance(rep_data.get("wordList"), list):
        word_list = [value for value in rep_data["wordList"] if isinstance(value, str)]
    token = rep_data.get("token") if isinstance(rep_data.get("token"), str) else None
    secret_key = rep_data.get("secretKey") if isinstance(rep_data.get("secretKey"), str) else None
    original_image = rep_data.get("originalImageBase64") if isinstance(rep_data.get("originalImageBase64"), str) else None
    if original_image is None and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            original_image = data.get("originalImageBase64") if isinstance(data.get("originalImageBase64"), str) else None
    return {
        "original_image_base64": original_image,
        "word_list": word_list,
        "token": token,
        "secret_key": secret_key,
        "rep_data": rep_data,
    }


def decode_clickword_response(payload: Any) -> bytes:
    """Extract and decode AJ-Captcha clickWord's originalImageBase64 field."""
    rep_data = _extract_repdata(payload)
    candidates = [payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            candidates.insert(0, payload["data"])
        if rep_data:
            candidates.insert(0, rep_data)
    value = extract_clickword_fields(payload).get("original_image_base64")
    if value is None:
        for item in candidates:
            if isinstance(item, dict):
                candidate = item.get("originalImageBase64")
                if isinstance(candidate, str):
                    value = candidate
                    break
                if isinstance(item.get("repData"), dict):
                    candidate = item["repData"].get("originalImageBase64")
                    if isinstance(candidate, str):
                        value = candidate
                        break
    if not value:
        raise AutoCaptchaUnavailable("captcha/get 响应缺少 originalImageBase64")
    encoded = value.split(",", 1)[-1].strip()
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        try:
            image_bytes = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise AutoCaptchaUnavailable("originalImageBase64 不是有效的 Base64") from exc
    if not image_bytes:
        raise AutoCaptchaUnavailable("originalImageBase64 解码后为空")
    return image_bytes
