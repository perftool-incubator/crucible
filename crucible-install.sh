#!/usr/bin/env bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash

# Repository Source Control
PWD=$(pwd)
PWD=$(readlink -e ${PWD})
SCRIPT_DIR=$(dirname $0)
SCRIPT_DIR=$(readlink -e ${SCRIPT_DIR})
if [ "${PWD}" != "${SCRIPT_DIR}" ]; then
    if ! pushd ${SCRIPT_DIR} > /dev/null; then
        echo "WARNING: Failed to pushd to ${SCRIPT_DIR}"
    fi
fi
git_status=$(git status --porcelain=2 --untracked-files=no --branch 2>&1)
if echo -e "${git_status}" | grep -q "not a git repository"; then
    GIT_REPO="https://github.com/perftool-incubator/crucible.git"
    GIT_BRANCH="master"
else
    git_tracking=$(echo "${git_status}" | grep "branch\.upstream" | awk '{ print $3 }')
    git_remote_branch=$(echo "${git_tracking}" | awk -F'/' '{ print $2 }')
    git_remote_name=$(echo "${git_tracking}" | awk -F'/' '{ print $1 }')
    git_remote_url=$(git remote get-url ${git_remote_name})

    GIT_REPO="${git_remote_url}"
    GIT_BRANCH="${git_remote_branch}"
fi

# Installer Settings
IDENTITY="/root/.crucible/identity"
SYSCONFIG="/etc/sysconfig/crucible"
DEPENDENCIES="podman git"
INSTALL_PATH="/opt/crucible"
GIT_INSTALL_LOG="/tmp/crucible-git-install.log"
CRUCIBLE_CONTROLLER_REGISTRY="quay.io/crucible/controller:latest"
CRUCIBLE_NO_CLIENT_SERVER_REGISTRY=0
VERBOSE=0

# User Exit Codes
EC_DEFAULT_EC=1
EC_FAIL_USER=3
EC_FAIL_CLONE=4
EC_FAIL_INSTALL=5
EC_AUTH_FILE_NOT_FOUND=6
EC_FAIL_DEPENDENCY=7
EC_FAIL_REGISTRY_UNSET=8
#9
EC_INVALID_OPTION=10
EC_UNEXPECTED_ARG=11
EC_FAIL_REGISTRY_SET=12
EC_FAIL_AUTH_SET=13
EC_FAIL_CHECKOUT=14
EC_PUSHD_FAIL=15

function exit_error {
    if [ -e ${GIT_INSTALL_LOG} ]; then
        echo "Contents of ${GIT_INSTALL_LOG}:"
        cat ${GIT_INSTALL_LOG}
        echo
    fi

    # Send message to stderr
    printf '\n%s\n\n' "$1" >&2
    # Return a code specified by $2 or 1 by default
    exit "${2-1}"
}

function usage {
    cat <<_USAGE_

    Crucible installer script.

    Usage: $0 [--client-server-registry <value> | --no-client-server-registry ] [ opt ]

    --client-server-registry <full registry url>
    or
    --no-client-server-registry

    optional:
        --client-server-auth-file <authentication file>
        --controller-registry <full registry url> (default is ${CRUCIBLE_CONTROLLER_REGISTRY})
        --name <your full name>
        --email <your email address>
        --verbose

    --help [displays this usage output]
_USAGE_
}

function identity {

    if [ -z $CRUCIBLE_NAME ] && [ -z $CRUCIBLE_EMAIL ]; then
        if [ -e $IDENTITY ]; then
            echo "Sourcing $IDENTITY"
            . $IDENTITY
        fi
    fi

    if [ -z $CRUCIBLE_NAME ]; then
            echo "Please enter your full name:"
            read CRUCIBLE_NAME
    fi

    if [ -z $CRUCIBLE_EMAIL ]; then
        echo "Please enter your email address:"
        read CRUCIBLE_EMAIL
    fi

    mkdir -p $(dirname $IDENTITY)
    echo "CRUCIBLE_NAME=\"$CRUCIBLE_NAME\"" > $IDENTITY
    echo "CRUCIBLE_EMAIL=\"$CRUCIBLE_EMAIL\"" >> $IDENTITY
}


function has_dependency {
    has_dep=0
    echo "Checking for $1"
    if $1 --version >/dev/null 2>&1; then
        has_dep=1
    else
        echo "Attempting to get it"
        if yum install -y $1 >/dev/null; then
            has_dep=1
        else
            exit_error "You need to install $1 before you can install crucible" $EC_FAIL_DEPENDENCY
        fi
    fi
    echo "$1: Got it"
    echo
}

longopts="name:,email:,help,verbose"
longopts+=",client-server-registry:,client-server-auth-file:,no-client-server-registry"
longopts+=",controller-registry:"
opts=$(getopt -q -o "" --longoptions "$longopts" -n "$0" -- "$@");
if [ $? -ne 0 ]; then
    exit_error "Unrecognized option specified: $@" $EC_INVALID_OPTION
fi
eval set -- "$opts";
while true; do
    case "$1" in
        --no-client-server-registry)
            shift;
            CRUCIBLE_NO_CLIENT_SERVER_REGISTRY=1
            ;;
        --client-server-registry)
            shift;
            CRUCIBLE_CLIENT_SERVER_REGISTRY="$1"
            shift;
            ;;
        --name)
            shift;
            CRUCIBLE_NAME="$1"
            shift;
            ;;
        --email)
            shift;
            CRUCIBLE_EMAIL="$1"
            shift;
            ;;
        --client-server-auth-file)
            shift;
            CRUCIBLE_CLIENT_SERVER_AUTH_FILE="$1"
            shift;
            ;;
        --controller-registry)
            shift;
            CRUCIBLE_CONTROLLER_REGISTRY="$1"
            shift;
            ;;
        --help)
            shift;
            usage
            exit
            ;;
        --verbose)
            shift;
            VERBOSE=1
            ;;
        --)
            shift;
            break;
           ;;
        *)
           exit_error "Unexpected argument [$1]" $EC_UNEXPECTED_ARG
           shift;
           break;
           ;;
    esac
done

if [ "`id -u`" != "0" ]; then
    exit_error "You must run this as root, exiting" $EC_FAIL_USER
fi

if [ ${CRUCIBLE_NO_CLIENT_SERVER_REGISTRY} == 1 ]; then
    if [ ! -z ${CRUCIBLE_CLIENT_SERVER_REGISTRY+x} ]; then
        exit_error "You cannot specify both --no-client-server-registry and --client-server-registry." $EC_FAIL_REGISTRY_SET
    fi

    if [ ! -z ${CRUCIBLE_CLIENT_SERVER_AUTH_FILE+x} ]; then
        exit_error "You cannot specify both --no-client-server-registry and --client-server-auth-file." $EC_FAIL_AUTH_SET
    fi
else
    if [ -z ${CRUCIBLE_CLIENT_SERVER_REGISTRY+x} ]; then
        exit_error "You must specify a registry with the --client-server-registry option." $EC_FAIL_REGISTRY_UNSET
    fi
fi

identity

for dep in $DEPENDENCIES; do
    has_dependency $dep
done

if [ ${CRUCIBLE_NO_CLIENT_SERVER_REGISTRY} == 0 ]; then
    if [ ! -z ${CRUCIBLE_CLIENT_SERVER_AUTH_FILE+x} ]; then
        if [ ! -f $CRUCIBLE_CLIENT_SERVER_AUTH_FILE ]; then
            exit_error "Crucible authentication file not found. See --client-server-auth-file for details." $EC_AUTH_FILE_NOT_FOUND
        fi
    fi
fi

if [ -d $INSTALL_PATH ]; then
    old_install_path="/opt/crucible-moved-on-`date +%d-%m-%Y_%H:%M:%S`"
    echo "An existing installation of crucible exists and will be moved to $old_install_path"
    /bin/mv "$INSTALL_PATH" "$old_install_path"
fi

echo "Installing crucible in $INSTALL_PATH"
git clone $GIT_REPO $INSTALL_PATH > $GIT_INSTALL_LOG 2>&1 ||
    exit_error "Failed to git clone $GIT_REPO, check $GIT_INSTALL_LOG for details" $EC_FAIL_CLONE
if pushd ${INSTALL_PATH} > /dev/null; then
    git checkout ${GIT_BRANCH} >> $GIT_INSTALL_LOG 2>&1 ||
        exit_error "Failed to git checkout ${GIT_BRANCH}, check $GIT_INSTALL_LOG for details" $EC_FAIL_CHECKOUT
    popd > /dev/null
else
    exit_error "Failed to pushd to ${INSTALL_PATH}, check ${GIT_INSTALL_LOG} for details" $EC_PUSHD_FAIL
fi
$INSTALL_PATH/bin/subprojects-install >>"$GIT_INSTALL_LOG" 2>&1 ||
    exit_error "Failed to execute crucible-project install, check $GIT_INSTALL_LOG for details" $EC_FAIL_INSTALL

SYSCONFIG_CRUCIBLE_CLIENT_SERVER_REGISTRY=""
SYSCONFIG_CRUCIBLE_CLIENT_SERVER_AUTH=""
if [ ${CRUCIBLE_NO_CLIENT_SERVER_REGISTRY} == 0 ]; then
    SYSCONFIG_CRUCIBLE_CLIENT_SERVER_REGISTRY="${CRUCIBLE_CLIENT_SERVER_REGISTRY}"
    SYSCONFIG_CRUCIBLE_CLIENT_SERVER_AUTH="\"${CRUCIBLE_CLIENT_SERVER_AUTH_FILE}\""
fi

# native crucible install script already created this, only append
cat << _SYSCFG_ >> $SYSCONFIG
CRUCIBLE_USE_CONTAINERS=1
CRUCIBLE_USE_LOGGER=1
CRUCIBLE_CONTAINER_IMAGE=${CRUCIBLE_CONTROLLER_REGISTRY}
CRUCIBLE_CLIENT_SERVER_REPO=${SYSCONFIG_CRUCIBLE_CLIENT_SERVER_REGISTRY}
CRUCIBLE_CLIENT_SERVER_AUTH=${SYSCONFIG_CRUCIBLE_CLIENT_SERVER_AUTH}
_SYSCFG_

if [ ${VERBOSE} == 1 ]; then
    cat ${GIT_INSTALL_LOG}
    echo
fi

echo "Installation is complete.  Run \"crucible help\" to see what's possible"
echo "You can also source /etc/profile.d/crucible_completions.sh (or re-login) to use tab completion for crucible"
echo

