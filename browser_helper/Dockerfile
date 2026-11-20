FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DISPLAY=:99 \
    HOME=/run/browser/home TMPDIR=/run/browser/tmp \
    XDG_CACHE_HOME=/run/browser/cache XDG_CONFIG_HOME=/run/browser/config \
    XDG_RUNTIME_DIR=/run/browser/xdg
WORKDIR /opt/pixchive-browser
USER root
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -i \
        -e 's|http://azure.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' \
        -e 's|http://security.ubuntu.com/ubuntu|https://security.ubuntu.com/ubuntu|g' {} + \
    && apt-get -o Acquire::Retries=3 update && apt-get install -y --no-install-recommends \
        xvfb x11vnc x11-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY chromium-policy.json /etc/chromium/policies/managed/pixchive.json
COPY chromium-policy.json /etc/opt/chrome/policies/managed/pixchive.json
COPY pixchive_browser/ ./pixchive_browser/
COPY entrypoint.py launch.py THIRD_PARTY_NOTICES.md ./
COPY vendor/ ./vendor/
RUN mkdir -p /state /run/browser && chown 1000:1000 /state /run/browser
EXPOSE 8765
ENTRYPOINT ["python", "launch.py"]
