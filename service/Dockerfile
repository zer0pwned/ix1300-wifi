# ix1300 scan service for the ScanSnap iX1300 (Wi-Fi).
# Build arguments:
#   WITH_TESSERACT=1   tesseract for page rotation and OCR (0 = no tesseract)
#   TESSERACT_LANGS    more OCR languages, for example "deu fra"
#   WITH_PILLOW=1      Pillow for blank detection, paper size and deskew (0 = no Pillow)
#   UID=1000, GID=1000 user and group of the service (the owner of ./data on the host)
# Without Pillow, the service uses the djpeg and jpegtran programs (libjpeg-turbo-progs).
FROM python:3.12-slim

ARG WITH_TESSERACT=1
ARG TESSERACT_LANGS=""
ARG WITH_PILLOW=1
ARG UID=1000
ARG GID=1000

RUN set -eux; \
    pkgs="libjpeg-turbo-progs libusb-1.0-0"; \
    if [ "$WITH_TESSERACT" = "1" ]; then \
        pkgs="$pkgs tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd"; \
        for l in $TESSERACT_LANGS; do pkgs="$pkgs tesseract-ocr-$l"; done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends $pkgs; \
    rm -rf /var/lib/apt/lists/*; \
    if [ "$WITH_PILLOW" = "1" ]; then pip install --no-cache-dir Pillow; fi; \
    groupadd -g "$GID" ix1300; \
    useradd -u "$UID" -g "$GID" -M -d /data -s /usr/sbin/nologin ix1300; \
    mkdir -p /data /config; \
    chown ix1300:ix1300 /data; \
    chmod 0700 /data

WORKDIR /app
COPY ix1300 /app/ix1300
COPY config.example.toml /app/config.example.toml

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    OMP_THREAD_LIMIT=1 \
    IX1300_CONFIG=/config/config.toml

# The service needs no root rights (Wi-Fi uses ports above 1024).
# The USB setup commands need root: "docker compose run --user root --privileged ...".
USER ix1300
VOLUME ["/config", "/data"]
ENTRYPOINT ["python", "-m", "ix1300"]
CMD ["serve"]
