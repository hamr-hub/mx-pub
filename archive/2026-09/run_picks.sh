#!/bin/bash
# Run loop_publish_v2 20 times for 20 picks, skip xhs, normal mode for verification
set -e
cd /Users/hyx/codespace/mx-pub
export SKIP_PLATFORMS=xhs
export MX_PUB_NORMAL_MODE=1
LOG=/Users/hyx/codespace/mx-pub/picks_publish.log
: > $LOG

for i in $(seq 1 30); do
  remaining=$(python3 -c "import json; q=json.load(open('publish_queue.json')); print(q.get('remaining', 0))")
  if [ "$remaining" = "0" ]; then
    echo "[driver] all done at iter $i" | tee -a $LOG
    break
  fi
  echo "===== iter $i (remaining=$remaining) =====" | tee -a $LOG
  python3 loop_publish_v2.py 2>&1 | tee -a $LOG
  sleep 2
done
echo "[driver] final state:"
python3 -c "import json; q=json.load(open('publish_queue.json')); print('total',q['total'],'remaining',q.get('remaining',0)); [print(v['_theme'],v['name'],'->',list(v.get('published',{}).keys())) for v in q['videos']]"
