#!/usr/bin/env bash
set -euo pipefail

# Default to the known SysML environment unless the caller overrides it.
DEFAULT_ENV="/home/balaji/miniconda3/envs/sysml-0.52.0"
ENV_PATH="${SYSML_CONDA_ENV:-$DEFAULT_ENV}"

# Allow the notebook root to be overridden; fall back to the project release folder.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTEBOOK_DIR="${SYSML_NOTEBOOK_DIR:-$PROJECT_ROOT/SysML-v2-Release}"

if [ ! -d "$ENV_PATH" ]; then
  echo "Error: Conda environment not found at '$ENV_PATH'." >&2
  exit 1
fi

if ! command -v conda >/dev/null 2>&1; then
  echo "Error: 'conda' command not found. Ensure Miniconda/Anaconda is installed and on PATH." >&2
  exit 1
fi

# Load the Conda function definitions into this non-interactive shell.
source "$(conda info --base)/etc/profile.d/conda.sh"

conda activate "$ENV_PATH"

# Hand off to Jupyter Lab; forward any CLI flags provided by the caller.
exec jupyter lab --notebook-dir "$NOTEBOOK_DIR" "$@"
