#!/usr/bin/env bash
set -euo pipefail

# Resolve conda even when it is not on PATH (common with fresh Miniforge installs).
CONDA_EXE=""
if command -v conda >/dev/null 2>&1; then
  CONDA_EXE="$(command -v conda)"
elif [ -x "$HOME/miniforge3/bin/conda" ]; then
  CONDA_EXE="$HOME/miniforge3/bin/conda"
elif [ -x "$HOME/miniconda3/bin/conda" ]; then
  CONDA_EXE="$HOME/miniconda3/bin/conda"
elif [ -x "$HOME/anaconda3/bin/conda" ]; then
  CONDA_EXE="$HOME/anaconda3/bin/conda"
fi

if [ -z "$CONDA_EXE" ]; then
  echo "Error: 'conda' command not found." >&2
  echo "Install Miniforge/Miniconda or export SYSML_CONDA_ENV to an existing environment." >&2
  exit 1
fi

DEFAULT_ENV_CANDIDATES=(
  "$HOME/miniforge3/envs/sysml-0.57.0"
  "$HOME/miniforge3/envs/sysml-0.52.0"
  "$HOME/miniforge3"
  "$HOME/miniconda3/envs/sysml-0.57.0"
  "$HOME/miniconda3/envs/sysml-0.52.0"
  "$HOME/miniconda3"
)

# Default to the first existing SysML environment unless overridden.
if [ -n "${SYSML_CONDA_ENV:-}" ]; then
  ENV_PATH="$SYSML_CONDA_ENV"
else
  ENV_PATH=""
  for candidate in "${DEFAULT_ENV_CANDIDATES[@]}"; do
    if [ -d "$candidate" ]; then
      ENV_PATH="$candidate"
      break
    fi
  done
fi

# Allow the notebook root to be overridden; fall back to the project release folder.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTEBOOK_DIR="${SYSML_NOTEBOOK_DIR:-$PROJECT_ROOT/SysML-v2-Release}"

if [ -z "$ENV_PATH" ] || [ ! -d "$ENV_PATH" ]; then
  echo "Error: SysML conda environment not found." >&2
  echo "Tried default candidates:" >&2
  for candidate in "${DEFAULT_ENV_CANDIDATES[@]}"; do
    echo "  - $candidate" >&2
  done
  echo "Override by setting SYSML_CONDA_ENV=/path/to/your/env" >&2
  exit 1
fi

# Load the Conda function definitions into this non-interactive shell.
source "$("$CONDA_EXE" info --base)/etc/profile.d/conda.sh"

conda activate "$ENV_PATH"

# Hand off to Jupyter Lab (interactive editing/visualization workflow);
# compile/load checks are performed by requirements_pipeline.py.
exec jupyter lab --notebook-dir "$NOTEBOOK_DIR" "$@"
