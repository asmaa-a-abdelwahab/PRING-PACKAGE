cd ~/PRING

mkdir -p $HOME/tmp $HOME/pip-cache $HOME/hf-cache $HOME/venvs

export TMPDIR=$HOME/tmp
export TEMP=$HOME/tmp
export TMP=$HOME/tmp
export PIP_CACHE_DIR=$HOME/pip-cache
export HF_HOME=$HOME/hf-cache

python -m venv $HOME/venvs/pring-cuda-env
source $HOME/venvs/pring-cuda-env/bin/activate

python -m pip install --upgrade pip setuptools wheel

python -m pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cu124

python -m pip install --no-cache-dir -r requirements-hpc-gpu.txt
python -m pip install -e .

python - <<'PY'
import torch
print("torch:", torch.__version__)
print("torch cuda:", torch.version.cuda)
print("cuda available:", torch.cuda.is_available())
PY