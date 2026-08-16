# Home Assistant add-on image.
#
# BUILD_FROM is supplied by the Supervisor from build.yaml. The default here is
# only a safety net: with no build.yaml the Supervisor passes nothing, and a
# bare `FROM ${BUILD_FROM}` fails the build with "base name should not be
# blank" rather than falling back to anything.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

# paho comes from Alpine's community repo rather than pip: it avoids PEP 668
# externally-managed-environment handling entirely, needs no network beyond the
# apk mirror, and pins to a version this project has been tested against
# (1.6.1). python3 is not in the plain HA base image.
RUN apk add --no-cache python3 py3-paho-mqtt

WORKDIR /app
COPY mppt/ /app/mppt/
COPY addon_bootstrap.py /app/

# Unbuffered so log lines reach the add-on log view as they happen rather than
# sitting in a pipe buffer until the process exits.
ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/addon_bootstrap.py"]
