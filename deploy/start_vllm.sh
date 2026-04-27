#!/bin/bash
# vLLM startup script for Qwen2.5-72B on g5.12xlarge (4×A10G)
# Model stored on EBS at /home/ubuntu/models/
# Usage: bash /home/ubuntu/start_vllm.sh
set -e

export PATH="/opt/pytorch/bin:$PATH"

MODEL_PATH=$(find /home/ubuntu/models -name "config.json" -path "*Qwen2*72B*GPTQ*" -exec dirname {} \; | head -1)

if [ -z "$MODEL_PATH" ]; then
    echo "ERROR: Model not found at /home/ubuntu/models/"
    echo "Download with: python3 -c \"from modelscope import snapshot_download; snapshot_download('Qwen/Qwen2.5-72B-Instruct-GPTQ-Int4', cache_dir='/home/ubuntu/models/cache')\""
    exit 1
fi

echo "Model: $MODEL_PATH"

# Kill old vLLM if running
pkill -f "vllm.entrypoints" 2>/dev/null || true
sleep 2

nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_PATH" \
    --served-model-name "Qwen2.5-72B" \
    --tensor-parallel-size 4 \
    --gpu-memory-utilization 0.90 \
    --max-model-len 8192 \
    --host 0.0.0.0 \
    --port 8000 \
    --enforce-eager \
    --disable-custom-all-reduce \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    > /home/ubuntu/vllm.log 2>&1 &

echo "vLLM PID: $!"
echo "Waiting for health check..."

for i in $(seq 1 60); do
    sleep 5
    if curl -s http://localhost:8000/health >/dev/null 2>&1; then
        echo "✅ vLLM READY after $((i*5))s"
        curl -s http://localhost:8000/v1/models | python3 -m json.tool 2>/dev/null || true
        exit 0
    fi
    echo "  waiting... $i/60"
done

echo "❌ TIMEOUT — check /home/ubuntu/vllm.log"
tail -20 /home/ubuntu/vllm.log
exit 1
