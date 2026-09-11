# 运行镜像只复制经常改动的 sidecar 源码；系统包、Python 依赖和浏览器归档位于
# Dockerfile.base 构建的本地镜像，从而避免日常更新访问 GHCR 或重复安装大依赖层。
ARG CAMOFOX_BROWSER_BASE_IMAGE=geo-camofox-browser-base:py312-camoufox152.0.4b29
FROM ${CAMOFOX_BROWSER_BASE_IMAGE}

USER root

COPY --chown=node:node app ./app
COPY --chown=node:node docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

USER node
ENV CAMOFOX_PORT=9377
EXPOSE 9377
EXPOSE 5900 6080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9377"]
