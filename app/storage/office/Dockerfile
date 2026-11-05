ARG OFFICE_BASE_IMAGE=debian:bookworm-slim
FROM ${OFFICE_BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-impress fonts-noto-cjk \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY render.sh /usr/local/bin/teach-render
RUN chmod 0555 /usr/local/bin/teach-render
USER 65534:65534
ENV HOME=/tmp/home
ENTRYPOINT ["/usr/local/bin/teach-render"]
