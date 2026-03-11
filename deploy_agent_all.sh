#!/bin/bash
# Деплой обновлённого агента на все 12 серверов с сервера панели
# Запускать на 45.156.26.113

AGENT_PATH="/tmp/agent_cron.py"
AGENT_B64=$(base64 -w0 "$AGENT_PATH")

declare -A SERVERS
SERVERS=(
    ["vps-1"]="94.232.41.116:Xlmmama_609)"
    ["vps-2"]="109.205.56.114:Xlmmama_609)"
    ["vps-3"]="94.232.40.187:Xlmmama_609)"
    ["vps-4"]="45.156.26.17:Xlmmama_609)"
    ["vps-5"]="94.232.43.193:Xlmmama_609)"
    ["vps-6"]="94.232.43.146:Xlmmama_609)"
    ["vps-7"]="93.183.71.175:4f46394a1dba45!"
    ["vps-8"]="94.232.44.188:Xlmmama_609)"
    ["vps-9"]="178.236.254.35:G672C22J1l0I"
    ["vps-10"]="94.232.44.53:Xlmmama_609)"
    ["vps-11"]="94.232.43.63:Xlmmama_609)"
    ["vps-12"]="94.232.40.170:Xlmmama_609)"
)

for name in $(echo "${!SERVERS[@]}" | tr ' ' '\n' | sort -V); do
    IFS=':' read -r ip pass <<< "${SERVERS[$name]}"
    echo "=== $name ($ip) ==="
    
    sshpass -p "$pass" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@$ip" bash -s <<REMOTE_EOF
        mkdir -p /opt/traffic_agent /var/lib/traffic_agent/history
        echo '$AGENT_B64' | base64 -d > /opt/traffic_agent/agent.py
        chmod +x /opt/traffic_agent/agent.py
        
        # Проверяем что скрипт валиден
        python3 -c "import ast; ast.parse(open('/opt/traffic_agent/agent.py').read()); print('Syntax OK')"
        
        # Запускаем чтобы создать первый снапшот (без отправки — конфиг может отсутствовать)
        python3 -c "
import sys
sys.path.insert(0, '/opt/traffic_agent')
exec(open('/opt/traffic_agent/agent.py').read().split('if __name__')[0])
raw = read_raw_traffic()
if raw:
    stats = compute_traffic(raw)
    save_daily_snapshot(stats)
    print(f'Snapshot created: {len(stats)} interfaces')
else:
    print('No interfaces found')
"
        
        # Проверяем снапшот
        ls -la /var/lib/traffic_agent/history/ 2>/dev/null
REMOTE_EOF
    
    if [ $? -eq 0 ]; then
        echo "✅ $name done"
    else
        echo "❌ $name FAILED"
    fi
    echo ""
done
