#!/bin/bash
# macOS 若双击被系统拦（门禁 / 权限），改用终端直接运行：
#     python3 这个文件夹里的 cct.py setup
cd "$(dirname "$0")"
exec python3 cct.py setup
