#!/bin/bash
# 断点续传某天 ZIP：curl -C - 按现有文件大小续传，完成后校验大小 + CRC。
set -u
DAY="$1"
PIPE=/workspace/project/.pipeline
case "$DAY" in
  day45) EXPECT=739980294 ;;
  day46) EXPECT=213965757 ;;
  *) echo "unknown day $DAY"; exit 2 ;;
esac
DEST="/workspace/project/downloads/ai-agent-6/$DAY/_staging/$DAY.zip"
PANPATH="/自动上传/课程/Luffy/AI 智能体/6 期/$DAY.zip"
ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$PANPATH")
URL="https://d.pcs.baidu.com/rest/2.0/pcs/file?method=download&app_id=250528&path=$ENC"
COOKIE=$PIPE/pan-cookies.txt
LOG=$PIPE/$DAY-download.log

echo "[$(date +%H:%M:%S)] start $DAY expect=$EXPECT" >> "$LOG"
for i in $(seq 1 40); do
  SIZE=$(stat -c %s "$DEST" 2>/dev/null || echo 0)
  if [ "$SIZE" -ge "$EXPECT" ]; then break; fi
  echo "[$(date +%H:%M:%S)] attempt $i resume_from=$SIZE" >> "$LOG"
  curl -sS -L -C - -b "$COOKIE" \
    -A "netdisk; pan.baidu.com" -e "https://pan.baidu.com/" \
    --connect-timeout 30 --speed-time 90 --speed-limit 10240 --max-time 2400 \
    -o "$DEST" "$URL" >> "$LOG" 2>&1
  sleep 3
done

SIZE=$(stat -c %s "$DEST" 2>/dev/null || echo 0)
echo "[$(date +%H:%M:%S)] download loop done size=$SIZE expect=$EXPECT" >> "$LOG"
if [ "$SIZE" != "$EXPECT" ]; then
  echo "INCOMPLETE $DAY size=$SIZE expect=$EXPECT"
  exit 1
fi
if unzip -t "$DEST" > $PIPE/$DAY-crc.log 2>&1; then
  echo "CRC_OK $DAY size=$SIZE"
else
  echo "CRC_FAIL $DAY (see $PIPE/$DAY-crc.log)"
  exit 1
fi
