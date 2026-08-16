#!/usr/bin/env bash
# Sobe o servidor de OCR (PaddleOCR + GPU) no WSL2, configurando as libs CUDA na mão —
# necessário porque o paddlepaddle-gpu (build CUDA 11.8) carrega cuDNN/cuBLAS/nvrtc por
# nome sem-versão (libX.so), enquanto os pacotes pip instalam só as versionadas (libX.so.N),
# e o driver do WSL mora em /usr/lib/wsl/lib (fora do path padrão do loader).
#
# Uso (no diretório do ocr-server, no outro PC):  ./run_ocr.sh
set -euo pipefail
cd "$(dirname "$0")"

# 1. diretórios de lib dos pacotes nvidia-*-cu11 instalados no venv
NV_LIBS=$(uv run python - <<'PY'
import os, nvidia
base = os.path.dirname(nvidia.__file__)
print(':'.join(os.path.join(base, d, 'lib') for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d, 'lib'))))
PY
)

# 2. cria os symlinks sem-versão (libX.so -> libX.so.N) que o Paddle procura
for d in ${NV_LIBS//:/ }; do
  for f in "$d"/*.so.*; do
    [ -e "$f" ] || continue
    alvo=$(echo "$f" | sed -E 's/\.so\.[0-9].*/.so/')
    [ -e "$alvo" ] || ln -sf "$f" "$alvo"
  done
done

# 3. path do loader: libs nvidia + driver do WSL (que já tem libcuda.so)
export LD_LIBRARY_PATH="$NV_LIBS:/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}"

# 4. sobe o servidor em GPU (nº de instâncias no pool = 1º argumento, default 3)
exec uv run python servidor_ocr.py --host 0.0.0.0 --port 8000 --device gpu --workers "${1:-3}"
