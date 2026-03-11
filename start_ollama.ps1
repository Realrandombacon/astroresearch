# ============================================================
#  Launch Ollama with RTX 3070 as primary GPU
# ============================================================
#  nvidia-smi confirms:
#    CUDA 0 = RTX 3070  (8 GB, primary)
#    CUDA 1 = GTX 1070  (8 GB, overflow)
#
#  CUDA_VISIBLE_DEVICES=0 forces Ollama to use ONLY the RTX 3070.
# ============================================================

$env:CUDA_VISIBLE_DEVICES = "0"
Write-Host "Starting Ollama with RTX 3070 (CUDA 0) as primary GPU..." -ForegroundColor Cyan
Write-Host "CUDA_VISIBLE_DEVICES=$env:CUDA_VISIBLE_DEVICES" -ForegroundColor Green
ollama serve
