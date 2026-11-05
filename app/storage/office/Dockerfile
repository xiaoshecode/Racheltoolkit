ARG OFFICE_BASE_IMAGE=debian:bookworm-slim
FROM ${OFFICE_BASE_IMAGE}
ARG DEBIAN_MIRROR=http://deb.debian.org
# Keep Debian's archive signatures enabled when selecting a nearer mirror.
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-impress fonts-noto-cjk \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY render.sh /usr/local/bin/teach-render
RUN chmod 0555 /usr/local/bin/teach-render
USER 65534:65534
ENV HOME=/tmp/home
ENTRYPOINT ["/usr/local/bin/teach-render"]
