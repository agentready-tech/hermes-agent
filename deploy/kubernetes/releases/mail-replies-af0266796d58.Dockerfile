FROM agentsee/hermes-kubernetes:eda469d9f21e-subscriptions
LABEL org.opencontainers.image.revision="af0266796d581b5c5ddf562ff39f21f0dd4689e7" io.agentsee.runtime.mail-replies="v1"
COPY workspace.py /opt/hermes/hermes_runtime/standalone/workspace.py
RUN /opt/hermes/.venv/bin/python -m py_compile /opt/hermes/hermes_runtime/standalone/workspace.py
