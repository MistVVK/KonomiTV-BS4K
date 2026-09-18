FROM rclone/rclone:1.73.0 AS rclone
FROM python:3.14-alpine

ARG KONOMITV_UID=1000
ARG KONOMITV_GID=1000
RUN apk add --no-cache fuse3 ca-certificates \
    && addgroup -g "${KONOMITV_GID}" cloud \
    && adduser -D -H -u "${KONOMITV_UID}" -G cloud cloud \
    && printf '\nuser_allow_other\n' >> /etc/fuse.conf
COPY --from=rclone /usr/local/bin/rclone /usr/bin/rclone
COPY server/app/utils/KonomiTVBS4KCloudSidecar.py /opt/konomitv-bs4k-cloud-sidecar.py
USER ${KONOMITV_UID}:${KONOMITV_GID}
ENTRYPOINT ["python", "/opt/konomitv-bs4k-cloud-sidecar.py"]
