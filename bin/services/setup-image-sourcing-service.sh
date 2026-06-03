#!/usr/bin/env bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash

. /etc/sysconfig/crucible

if [ -z "${CRUCIBLE_HOME}" -o ! -e "${CRUCIBLE_HOME}" ]; then
    echo "ERROR: Could not find \${CRUCIBLE_HOME} [${CRUCIBLE_HOME}], exiting."
    exit 1
fi

UNIT_FILE="${CRUCIBLE_HOME}/bin/services/crucible-image-sourcing.service"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="crucible-image-sourcing"

if [ ! -f "${UNIT_FILE}" ]; then
    echo "ERROR: Unit file not found at ${UNIT_FILE}"
    exit 1
fi

action="${1:-install}"

case "${action}" in
    install)
        echo "Installing ${SERVICE_NAME} systemd service..."

        cp "${UNIT_FILE}" "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
        systemctl daemon-reload
        systemctl enable "${SERVICE_NAME}"

        echo ""
        echo "${SERVICE_NAME} service installed and enabled."
        echo ""
        echo "The service will start automatically on boot."
        echo "To start it now:  systemctl start ${SERVICE_NAME}"
        echo "To check status:  systemctl status ${SERVICE_NAME}"
        echo ""
        echo "Configure the listening port in ${CRUCIBLE_HOME}/config/services.json"
        echo "under the image-sourcing.services section."
        ;;
    uninstall)
        echo "Uninstalling ${SERVICE_NAME} systemd service..."

        if systemctl is-active "${SERVICE_NAME}" 2>/dev/null | grep -q "^active$"; then
            systemctl stop "${SERVICE_NAME}"
        fi
        systemctl disable "${SERVICE_NAME}" 2>/dev/null
        rm -f "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
        systemctl daemon-reload

        echo "${SERVICE_NAME} service uninstalled."
        ;;
    *)
        echo "Usage: $(basename $0) [install|uninstall]"
        exit 1
        ;;
esac
