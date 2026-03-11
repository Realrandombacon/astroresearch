@echo off
REM ============================================================
REM  Launch Ollama with RTX 3070 as primary GPU
REM ============================================================
REM  nvidia-smi confirms:
REM    CUDA 0 = RTX 3070  (8 GB, primary)
REM    CUDA 1 = GTX 1070  (8 GB, overflow)
REM
REM  CUDA_VISIBLE_DEVICES=0 forces Ollama to use ONLY the RTX 3070.
REM ============================================================

set CUDA_VISIBLE_DEVICES=0
echo Starting Ollama with RTX 3070 (CUDA 0) as primary GPU...
echo CUDA_VISIBLE_DEVICES=%CUDA_VISIBLE_DEVICES%
ollama serve
