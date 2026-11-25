FROM python:3.13-slim

# Use the Tsinghua Debian mirror for the subsequent HSB harness image build.
# The harness Dockerfile reuses this task image as BASE, so the rewritten
# sources are inherited by the gptme runtime layer.
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
    -exec sed -i \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_MIRROR}/debian-security|g" \
      -e "s|https://deb.debian.org/debian-security|${DEBIAN_MIRROR}/debian-security|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}/debian|g" \
      -e "s|https://deb.debian.org/debian|${DEBIAN_MIRROR}/debian|g" {} +

WORKDIR /app
COPY input /app/input
COPY web_pages /app/web_pages
COPY mcp_server.py /app/mcp_server.py
COPY meeting_tools.py /app/meeting_tools.py
RUN mkdir -p /app/output/email-drafts
