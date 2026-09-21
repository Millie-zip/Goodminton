try:
    from .cli import main
except ImportError:  # 兼容直接执行脚本。
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from buaa_booking.cli import main


if __name__ == "__main__":
    main()
