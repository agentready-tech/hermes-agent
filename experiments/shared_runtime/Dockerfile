# The dependency stage is built from this same clean commit's native Dockerfile.
# No inherited Hermes application release or application patch overlay is used.
ARG DEPENDENCY_IMAGE
FROM ${DEPENDENCY_IMAGE}
ARG SOURCE_REVISION
ARG PROTOTYPE_VERSION
WORKDIR /opt/hermes
COPY --link --chmod=a+rX,go-w . .
RUN /opt/hermes/.venv/bin/python deploy/kubernetes/release_source.py verify /opt/hermes
ENV HERMES_PYTHON=/opt/hermes/.venv/bin/python
ENV PATH=/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin
ENV PYTHONPATH=/opt/hermes
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${PROTOTYPE_VERSION}" \
      io.agentsee.runtime.policy="local-prototype-only" \
      io.agentsee.runtime.source="clean-git-archive"
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/opt/hermes/experiments/shared_runtime/worker.py"]
