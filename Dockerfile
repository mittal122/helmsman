# Helmsman — production image. Multi-stage: fetch the k8s CLIs, then a slim runtime.
FROM python:3.12-slim AS tools
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates tar && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH=amd64
RUN set -eux; b=/out; mkdir -p "$b"; \
    gh() { curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$1/releases/latest" | sed -E 's#.*/tag/##'; }; \
    KV="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; \
    curl -fsSLo "$b/kubectl" "https://dl.k8s.io/release/$KV/bin/linux/${TARGETARCH}/kubectl"; \
    hv="$(gh helm/helm)"; curl -fsSL "https://get.helm.sh/helm-${hv}-linux-${TARGETARCH}.tar.gz" | tar -xz -C "$b" --strip-components=1 "linux-${TARGETARCH}/helm"; \
    t="$(gh yannh/kubeconform)"; curl -fsSL "https://github.com/yannh/kubeconform/releases/download/${t}/kubeconform-linux-${TARGETARCH}.tar.gz" | tar -xz -C "$b" kubeconform; \
    t="$(gh zegl/kube-score)"; v="${t#v}"; curl -fsSL "https://github.com/zegl/kube-score/releases/download/${t}/kube-score_${v}_linux_${TARGETARCH}.tar.gz" | tar -xz -C "$b" kube-score; \
    t="$(gh aquasecurity/trivy)"; v="${t#v}"; a="Linux-64bit"; if [ "$TARGETARCH" = arm64 ]; then a="Linux-ARM64"; fi; \
    curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/${t}/trivy_${v}_${a}.tar.gz" | tar -xz -C "$b" trivy; \
    chmod +x "$b"/*

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=tools /out/ /usr/local/bin/
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY chart/ chart/
COPY prompts/ prompts/
RUN useradd -u 10001 -m appuser && chown -R appuser /app
USER 10001
EXPOSE 8000
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
