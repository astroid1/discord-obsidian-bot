# CUDA 12.x + cuDNN 9 runtime: what ctranslate2 (faster-whisper) >= 4.5 and torch cu126 expect.
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
# A stub package so the dependency layer can be built (and cached) before the real source is copied.
RUN mkdir -p src/dob && touch src/dob/__init__.py

# torch/torchaudio/torchcodec from the CUDA 12.6 index (works with any 12.6+ driver; the default
# PyPI wheels are built for CUDA 13 and fail on older drivers). The extra index on the second
# install keeps the resolver from swapping them for PyPI builds.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu126
RUN pip install -U pip \
    && pip install --index-url ${TORCH_INDEX} torch torchaudio torchcodec \
    && pip install --extra-index-url ${TORCH_INDEX} -e ".[gpu]"

# Real source last: code changes only rebuild from here.
COPY src ./src
RUN pip install --no-deps -e .

# The vault is a bind mount owned by the host user; make git not care.
RUN git config --system safe.directory '*' \
    && git config --system core.autocrlf false \
    && git config --system core.filemode false

RUN useradd -m -u 1000 app && mkdir -p /vault /data /inbox /models && chown -R app:app /vault /data /inbox /models /app
USER app

ENTRYPOINT ["dob"]
CMD ["run"]
