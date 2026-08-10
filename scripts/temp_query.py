import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pymysql
from dotenv import load_dotenv

# 加载项目根目录的 .env（脚本位于 scripts/ 下，需定位到上一级）
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

LOC = {'esp_75497C': '🚶 楼梯', 'esp_B5C6B0': '🏠 大厅', 'esp_25BFA9': '🍳 厨房', 'esp_B5B6BC': '🌳 室外'}
IDS = ['esp_75497C', 'esp_B5C6B0', 'esp_25BFA9', 'esp_B5B6BC']

conn = pymysql.connect(
    host=os.environ.get('TEMP_DB_HOST', ''),
    port=int(os.environ.get('TEMP_DB_PORT', '0')),
    user=os.environ.get('TEMP_DB_USER', ''),
    password=os.environ.get('TEMP_DB_PASSWORD', ''),
    database=os.environ.get('TEMP_DB_NAME', ''),
    ssl_disabled=True,
)
c = conn.cursor()

c.execute("SELECT client_id,temp,hum,DATE_FORMAT(curr_time,'%%m-%%d %%H:%%i') FROM `8266_temp` WHERE client_id IN %s AND id IN (SELECT MAX(id) FROM `8266_temp` GROUP BY client_id) ORDER BY FIELD(client_id,'esp_75497C','esp_B5C6B0','esp_25BFA9','esp_B5B6BC')", (IDS,))
latest = {r[0]:r for r in c.fetchall()}

c.execute("SELECT client_id,ROUND(MIN(temp),1),ROUND(MAX(temp),1),ROUND(MIN(hum),1),ROUND(MAX(hum),1) FROM `8266_temp` WHERE client_id IN %s AND curr_time >= NOW() - INTERVAL 24 HOUR GROUP BY client_id ORDER BY FIELD(client_id,'esp_75497C','esp_B5C6B0','esp_25BFA9','esp_B5B6BC')", (IDS,))
stats = {r[0]:r for r in c.fetchall()}

c.close(); conn.close()

for eid in IDS:
    cur, st = latest.get(eid), stats.get(eid)
    if not cur or not st: continue
    nt, nh, ts = cur[1], cur[2], cur[3]
    mn, mx, mnh, mxh = st[1], st[2], st[3], st[4]
    trend = '➡️ 中间' if mx-mn>0.5 and 0.3<(nt-mn)/(mx-mn)<0.7 else ('⬆️ 偏高' if mx-mn>0.5 else '➡️ 平稳')
    print(f"{LOC[eid]} {ts} | 🌡️ {nt:.1f}°C({mn:.1f}~{mx:.1f}) 💧 {nh:.1f}%({mnh:.1f}~{mxh:.1f}) {trend}")
