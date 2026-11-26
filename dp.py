#!/usr/bin/env python3
"""启动入口：调用 core.main.main 并把它返回的退出码落地。

main() 自身不调用 sys.exit（这样它也能被当库调用），退出码由这里转给进程。
"""
import sys

from core.main import main

if __name__ == "__main__":
    sys.exit(main())
