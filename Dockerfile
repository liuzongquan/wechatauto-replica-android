# syntax=docker/dockerfile:1
# Host controller only: no Android runtime, emulator, WeChat APK or credentials.
FROM python:3.12-slim-bookworm AS build
ARG RUNTIME_LOCK=requirements/runtime.lock
WORKDIR /build
COPY . .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements/build.lock \
    && python -m pip download --no-cache-dir --require-hashes --only-binary=:all: \
      --dest /wheels -r "$RUNTIME_LOCK" \
    && python -m build --wheel --no-isolation --outdir /project-wheel \
    && cp "$RUNTIME_LOCK" /runtime.lock

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/controller
RUN apt-get update \
    && apt-get install --yes --no-install-recommends adb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 controller \
    && useradd --uid 10001 --gid controller --create-home controller
COPY --from=build /wheels /wheels
COPY --from=build /project-wheel /project-wheel
COPY --from=build /runtime.lock /runtime.lock
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
      --require-hashes -r /runtime.lock \
    && python -m pip install --no-cache-dir --no-index --no-deps /project-wheel/*.whl \
    && rm -rf /wheels /project-wheel /runtime.lock
USER 10001:10001
WORKDIR /home/controller
ENTRYPOINT ["wechatauto-android"]
CMD ["--help"]
