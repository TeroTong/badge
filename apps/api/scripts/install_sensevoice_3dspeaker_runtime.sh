#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -n "${UPLOAD_DIR:-}" ]]; then
  DEFAULT_RUNTIME_ROOT="${UPLOAD_DIR}/asr_runtime"
else
  DEFAULT_RUNTIME_ROOT="${API_ROOT}/.runtime/asr_runtime"
fi

RUNTIME_ROOT="${ASR_RUNTIME_DIR:-${DEFAULT_RUNTIME_ROOT}}"
THREED_SPEAKER_REPO_PATH="${THREED_SPEAKER_REPO_PATH:-${RUNTIME_ROOT}/3D-Speaker}"
THREED_SPEAKER_MODEL_CACHE_DIR="${THREED_SPEAKER_MODEL_CACHE_DIR:-${RUNTIME_ROOT}/modelscope}"

mkdir -p "${RUNTIME_ROOT}" "${THREED_SPEAKER_MODEL_CACHE_DIR}"

python -m pip install --upgrade pip setuptools wheel
python -m pip install \
  numpy \
  scipy \
  scikit-learn \
  pyyaml \
  soundfile \
  kaldiio \
  modelscope \
  "funasr>=1.1.3" \
  torch \
  torchaudio

if [[ -d "${THREED_SPEAKER_REPO_PATH}/.git" ]]; then
  git -C "${THREED_SPEAKER_REPO_PATH}" pull --ff-only
else
  rm -rf "${THREED_SPEAKER_REPO_PATH}"
  git clone --depth 1 https://github.com/modelscope/3D-Speaker.git "${THREED_SPEAKER_REPO_PATH}"
fi

cat <<EOF
SenseVoice + 3D-Speaker runtime is ready.

Recommended env:
  ASR_PROVIDER=sensevoice_3dspeaker
  THREED_SPEAKER_REPO_PATH=${THREED_SPEAKER_REPO_PATH}
  THREED_SPEAKER_MODEL_CACHE_DIR=${THREED_SPEAKER_MODEL_CACHE_DIR}
EOF
