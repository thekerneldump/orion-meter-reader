ARG PYTHON_VERSION=3.12

FROM debian:bookworm-slim AS rtl433-build
ARG RTL433_VERSION=25.12

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        librtlsdr-dev \
        libusb-1.0-0-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --branch "${RTL433_VERSION}" --depth 1 \
        https://github.com/merbanan/rtl_433.git /src/rtl_433 \
    && cmake -S /src/rtl_433 -B /src/rtl_433/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_RTLSDR=ON \
        -DENABLE_SOAPYSDR=OFF \
    && cmake --build /src/rtl_433/build --parallel \
    && cmake --install /src/rtl_433/build --prefix /usr/local

FROM python:${PYTHON_VERSION}-slim-bookworm
ARG APP_VERSION=0.0.1

LABEL org.opencontainers.image.title="Orion Meter Reader" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        librtlsdr0 \
        libusb-1.0-0 \
        rtl-sdr \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rtl433-build /usr/local/bin/rtl_433 /usr/local/bin/rtl_433
COPY orion-meter-reader.py /app/orion-meter-reader.py

WORKDIR /app
ENV PYTHONUNBUFFERED=1
EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/healthz', timeout=3)"]

CMD ["python3", "/app/orion-meter-reader.py"]
