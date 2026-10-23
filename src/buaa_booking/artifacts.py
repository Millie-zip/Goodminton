from __future__ import annotations

import logging
import shutil
import stat
import sys
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Iterator, TextIO


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _Tee:
    def __init__(self, console: TextIO | None, log: TextIO) -> None:
        self.console = console
        self.log = log

    def write(self, text: str) -> int:
        self.log.write(text)
        self.log.flush()
        if self.console is not None:
            self.console.write(text)
        return len(text)

    def flush(self) -> None:
        self.log.flush()
        if self.console is not None:
            self.console.flush()

    def __getattr__(self, name: str):
        return getattr(self.console if self.console is not None else self.log, name)


class _Formatter(logging.Formatter):
    def __init__(self, timezone: tzinfo) -> None:
        super().__init__("%(asctime)s [%(levelname)s] %(message)s")
        self.timezone = timezone

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, self.timezone).isoformat(timespec="seconds")


def _check_path(path: Path, root: Path) -> None:
    # 不跟随符号链接、Windows junction 或其他重解析点。
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"跳过链接或重解析点：{path}")
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"清理目标不在 artifacts 内：{path}")


def _check_tree(path: Path, root: Path) -> None:
    _check_path(path, root)
    if path.is_dir():
        for child in path.iterdir():
            _check_tree(child, root)


def cleanup_artifacts(root: Path, *, now: datetime, active_dir: Path) -> None:
    """保留当天及前四天；清理失败只记日志，不改变预约结果。"""
    if root.is_symlink() or root.is_junction():
        logger.warning("artifacts_cleanup_failed: 跳过 artifacts 根目录链接：%s", root)
        return
    root = root.resolve()
    cutoff = now.date() - timedelta(days=4)
    active_dir = active_dir.resolve()

    def clean(path: Path) -> None:
        try:
            _check_path(path, root)
            if path.resolve() == active_dir:
                return
            if path.is_dir():
                try:
                    record_date = date.fromisoformat(path.name)
                    if path.name != record_date.isoformat():
                        raise ValueError("非 YYYY-MM-DD 目录")
                except ValueError:
                    # 兼容 calibration-*、morning-live 等历史目录。
                    modified = datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo).date()
                    for child in list(path.iterdir()):
                        clean(child)
                    if modified < cutoff and not any(path.iterdir()):
                        _check_path(path, root)
                        path.rmdir()
                        logger.info("artifacts_deleted: %s", path.relative_to(root))
                    return
                if record_date >= cutoff:
                    return
                _check_tree(path, root)
                shutil.rmtree(path)
            else:
                modified = datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo).date()
                if modified >= cutoff:
                    return
                _check_path(path, root)
                path.unlink()
            logger.info("artifacts_deleted: %s", path.relative_to(root))
        except (OSError, ValueError):
            logger.warning("artifacts_cleanup_failed: %s", path, exc_info=True)

    try:
        for path in list(root.iterdir()):
            clean(path)
        logger.info("artifacts_cleanup_finished: 保留 %s 起的记录", cutoff)
    except OSError:
        logger.warning("artifacts_cleanup_failed: %s", root, exc_info=True)


@contextmanager
def daily_run_artifacts(root: Path, run_date: date, timezone: tzinfo) -> Iterator[Path]:
    output_dir = root / run_date.isoformat()
    # ExitStack 让清理在日志关闭前执行，也覆盖日志初始化失败的情况。
    with ExitStack() as stack:
        try:
            if root.is_symlink() or root.is_junction():
                raise ValueError(f"artifacts 根目录不能是链接：{root}")
            output_dir.mkdir(parents=True, exist_ok=True)
            _check_path(output_dir, root.resolve())
            log_path = output_dir / "run.log"
            if log_path.exists() or log_path.is_symlink():
                _check_path(log_path, root.resolve())
            log = stack.enter_context((output_dir / "run.log").open("a", encoding="utf-8"))
            handler = logging.StreamHandler(log)
            handler.setFormatter(_Formatter(timezone))
            root_logger = logging.getLogger()
            previous_level = root_logger.level
            stack.callback(root_logger.setLevel, previous_level)
            stack.callback(handler.close)
            stack.callback(root_logger.removeHandler, handler)
            root_logger.addHandler(handler)
            root_logger.setLevel(min(previous_level, logging.INFO))
            stack.enter_context(redirect_stdout(_Tee(sys.stdout, log)))
            stack.enter_context(redirect_stderr(_Tee(sys.stderr, log)))
            logger.info("run_started: 运行日期 %s", run_date)
            try:
                yield output_dir
            except BaseException:
                logger.exception("run_failed: 主流程异常退出")
                raise
            else:
                logger.info("run_finished: 主流程已结束")
        finally:
            try:
                cleanup_artifacts(root, now=datetime.now(timezone), active_dir=output_dir)
            except Exception:
                logger.warning("artifacts_cleanup_failed: %s", root, exc_info=True)
