#!/bin/sh
# 优先使用构建上下文中预置的发行包，避免国内构建时重复访问 GitHub。
# 保留远程下载回退，使插件在独立安装且没有本地二进制时仍可使用。
set -e
if [ -f /app/bin/yt-dlp ]; then
  cp /app/bin/yt-dlp /usr/local/bin/yt-dlp
else
  curl -fL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
fi
chmod +x /usr/local/bin/yt-dlp
