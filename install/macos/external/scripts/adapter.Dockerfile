FROM python:3.11-slim
COPY source/install/macos/omlx-proxy/vendor/requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/container-adapter.py /usr/local/bin/nomad-adapter.py
WORKDIR /adapter
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/usr/local/bin/nomad-adapter.py"]
