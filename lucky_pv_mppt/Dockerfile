# Home Assistant add-on image.
#
# BUILD_FROM is supplied by the Supervisor and resolves to the right base image
# for the machine's architecture.
ARG BUILD_FROM
FROM ${BUILD_FROM}

# Set as an env var rather than the --break-system-packages flag: pip older
# than 23.0 does not know the flag and would fail the build, but ignores an
# unrecognised env var.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# The HA base images are Alpine. python3 is not included in the plain base.
RUN apk add --no-cache python3 py3-pip \
    && python3 -m pip install --no-cache-dir 'paho-mqtt>=1.6,<3'

WORKDIR /app
COPY mppt/ /app/mppt/
COPY addon_bootstrap.py /app/

# Unbuffered so log lines reach the add-on log view as they happen rather than
# sitting in a pipe buffer until the process exits.
ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/addon_bootstrap.py"]
