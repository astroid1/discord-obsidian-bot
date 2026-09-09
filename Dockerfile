# CUDA 12.4 + cuDNN 9 runtime: what ctranslate2 (faster-whisper) >= 4.5 and torch cu124 expect.
# Mixing cuDNN versions (e.g. a cudnn8 base + torch's cuDNN 9) crashes ctranslate2, so keep this pairing.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl git ffmpeg tzdata \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

# torch/torchaudio/torchcodec from the CUDA 12.4 index first, then the project with the gpu extra.
RUN pip install -U pip \
    && pip install --index-url https://download.pytorch.org/whl/cu124 torch torchaudio torchcodec \
    && pip install -e ".[gpu]"

# The vault is a bind mount owned by the host user; make git not care.
RUN git config --system safe.directory '*' \
    && git config --system core.autocrlf false \
    && git config --system core.filemode false

RUN useradd -m -u 1000 app && mkdir -p /vault /data /inbox /models && chown -R app:app /vault /data /inbox /models /app
USER app

ENTRYPOINT ["dob"]
CMD ["run"]
