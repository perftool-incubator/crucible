#!/usr/bin/env bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash

# Installer Settings
USER_DIR="/root/.crucible"
IDENTITY="/root/.crucible/identity"
SYSCONFIG="/etc/sysconfig/crucible"
DEPENDENCIES="podman git jq"
INSTALL_PATH="/opt/crucible"
GIT_INSTALL_LOG="/tmp/crucible-git-install.log"
CRUCIBLE_CONTROLLER_REGISTRY="quay.io/crucible/controller:latest"
DEFAULT_GIT_REPO="https://github.com/perftool-incubator/crucible.git"
DEFAULT_GIT_BRANCH="master"
GIT_REPO=""
GIT_BRANCH=""
VERBOSE=0

# User Exit Codes
EC_DEFAULT_EC=1
EC_FAIL_USER=3
EC_FAIL_CLONE=4
EC_FAIL_INSTALL=5
EC_AUTH_FILE_NOT_FOUND=6
EC_FAIL_DEPENDENCY=7
EC_FAIL_REGISTRY_UNSET=8
EC_TLS_VERIFY_ERROR=9
EC_INVALID_OPTION=10
EC_UNEXPECTED_ARG=11
EC_FAIL_REGISTRY_SET=12
EC_FAIL_AUTH_SET=13
EC_FAIL_CHECKOUT=14
EC_PUSHD_FAIL=15

# remove a previous installation log
if [ -e ${GIT_INSTALL_LOG} ]; then
    rm ${GIT_INSTALL_LOG}
fi

function determine_git_install_source {
    local PWD SCRIPT_DIR
    local git_status git_use_default git_tracking git_remote_branch git_remote_name git_remote_url

    # check if the user specified a git repository via --git-repo
    if [ -n "${GIT_REPO}" ]; then
        # since the user specified a git repository to use as the
        # installation source there is no need to introspect the
        # current environment to determine where to install from

        return
    fi

    # the default behavior is to introspect the environment to
    # determine what to use as the crucible installation source; there
    # are reasons why we may not be able to do that and if we
    # encounter one of those scenarios we will have to fall back on
    # assuming that the upstream repository and master branch are what
    # we should use; this variable tracks whether or not we have
    # encountered one of those scenarios
    git_use_default=0

    PWD=$(pwd)
    PWD=$(readlink -e ${PWD})
    SCRIPT_DIR=$(dirname $0)
    SCRIPT_DIR=$(readlink -e ${SCRIPT_DIR})
    # if the current working directory is not the same directory where
    # the install script resides then we need to make it the same
    if [ "${PWD}" != "${SCRIPT_DIR}" ]; then
        # make sure we move to the directory where the installer
        # script resides; if we are going to do git operations we need
        # to be in the git repository
        if ! pushd ${SCRIPT_DIR} > /dev/null; then
            # since we can't even pushd to the directory where the
            # script resides lets assume we are in some unknown state
            # and just install from the upstream master branch

            echo "WARNING: Failed to pushd to ${SCRIPT_DIR}"
            git_use_default=1
        fi
    fi

    # query for git repository information using a interface --
    # porcelain v2 -- that is guaranteed to be consistent even as git
    # changes
    git_status=$(git status --porcelain=2 --untracked-files=no --branch 2>&1)

    if echo -e "${git_status}" | grep -q "not a git repository"; then
        # since we are not in a git repository we cannot determine
        # what repo/branch to use for installation so use the default
        # upstream master branch; this implies the installer script
        # was acquired via wget/curl/etc. instead of via git-clone

        git_use_default=1
    fi

    if ! echo -e "${git_status}" | grep "branch\.upstream"; then
        # the repository we are in does not reveal an upstream
        # repository that we should install from so use the default
        # upstream master branch; an example of where this happens is
        # the github runner environment that is created by a pull
        # request

        git_use_default=1
    fi

    if [ "${git_use_default}" == 0 ]; then
        # we were able to introspect the git repository and find the
        # information required to install crucible by pointing at the
        # upstream repository and branch that it is tracking

        git_tracking=$(echo "${git_status}" | grep "branch\.upstream" | awk '{ print $3 }')
        git_remote_branch=$(echo "${git_tracking}" | awk -F'/' '{ print $2 }')
        git_remote_name=$(echo "${git_tracking}" | awk -F'/' '{ print $1 }')
        git_remote_url=$(git remote get-url ${git_remote_name})

        GIT_REPO="${git_remote_url}"
        GIT_BRANCH="${git_remote_branch}"
    elif [ "${git_use_default}" == 1 ]; then
        # fall back on the upstream repository and the master branch
        # as the installation source

        GIT_REPO="${DEFAULT_GIT_REPO}"
        GIT_BRANCH="${DEFAULT_GIT_BRANCH}"
    fi
}

function exit_error {
    if [ -e ${GIT_INSTALL_LOG} ]; then
        echo "Contents of ${GIT_INSTALL_LOG}:"
        cat ${GIT_INSTALL_LOG}
        echo
    fi

    # Send message to stderr
    printf 'ERROR:\n%s\n\n' "$1" >&2
    # Return a code specified by $2 or 1 by default
    exit "${2-1}"
}

function usage {
    cat <<_USAGE_

    Crucible installer script.

    Usage: $0 --client-server-registry <value> [ opt ]

    --client-server-registry <full registry url>

    optional:
        --client-server-auth-file <authentication file>
        --client-server-tls-verify true|false
        --controller-registry <full registry url> (default is ${CRUCIBLE_CONTROLLER_REGISTRY})
        --name <your full name>
        --email <your email address>
        --verbose
        --git-repo <repo path/url>
        --git-branch <branch name>

    --help [displays this usage output]
_USAGE_
}

# cleanup previous installation
function clean_old_install {
    if [ -d $INSTALL_PATH ]; then
        old_install_path="/opt/crucible-moved-on-`date +%d-%m-%Y_%H:%M:%S`"
        echo "An existing installation of crucible exists and will be moved to $old_install_path"
        /bin/mv "$INSTALL_PATH" "$old_install_path"
    fi

    # reset the update tracker if there is any existing state
    rm -f "${USER_DIR}/update-status*" > /dev/null
}

# set name and email address
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

# check for dependencies and attempt to install any missing
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
longopts+=",client-server-registry:,client-server-auth-file:,client-server-tls-verify:"
longopts+=",controller-registry:,git-repo:,git-branch:"
opts=$(getopt -q -o "" --longoptions "$longopts" -n "$0" -- "$@");
if [ $? -ne 0 ]; then
    exit_error "Unrecognized option specified: $@" $EC_INVALID_OPTION
fi
eval set -- "$opts";
while true; do
    case "$1" in
        --client-server-tls-verify)
            shift;
            CRUCIBLE_CLIENT_SERVER_TLS_VERIFY="$1"
            shift;
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
        --git-repo)
            shift;
            GIT_REPO="$1"
            shift;
            ;;
        --git-branch)
            shift;
            GIT_BRANCH="$1"
            shift;
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

determine_git_install_source

if [ "`id -u`" != "0" ]; then
    exit_error "You must run this as root, exiting" $EC_FAIL_USER
fi

if [ -z ${CRUCIBLE_CLIENT_SERVER_REGISTRY+x} ]; then
    exit_error "You must specify a registry with the --client-server-registry option." $EC_FAIL_REGISTRY_UNSET
fi

identity

for dep in $DEPENDENCIES; do
    has_dependency $dep
done

if [ ! -z ${CRUCIBLE_CLIENT_SERVER_AUTH_FILE+x} ]; then
    if [ ! -f $CRUCIBLE_CLIENT_SERVER_AUTH_FILE ]; then
        exit_error "Crucible authentication file not found. See --client-server-auth-file for details." $EC_AUTH_FILE_NOT_FOUND
    fi
fi

if [ ! -z ${CRUCIBLE_CLIENT_SERVER_TLS_VERIFY+x} ]; then
    if [ "${CRUCIBLE_CLIENT_SERVER_TLS_VERIFY}" != "true" -a "${CRUCIBLE_CLIENT_SERVER_TLS_VERIFY}" != "false" ]; then
        exit_error "Incorrect Crucible client server tls verify option [${CRUCIBLE_CLIENT_SERVER_TLS_VERIFY}].  See --client-server-tls-verify for details." $EC_TLS_VERIFY_ERROR
    fi
fi

clean_old_install

echo "Installing crucible in $INSTALL_PATH"
echo "Using Git repo:   ${GIT_REPO}"
echo "Using Git branch: ${GIT_BRANCH}"
git clone $GIT_REPO $INSTALL_PATH > $GIT_INSTALL_LOG 2>&1 ||
    exit_error "Failed to git clone $GIT_REPO, check $GIT_INSTALL_LOG for details" $EC_FAIL_CLONE
if [ -n "${GIT_BRANCH}" ]; then
    if pushd ${INSTALL_PATH} > /dev/null; then
        git checkout ${GIT_BRANCH} >> $GIT_INSTALL_LOG 2>&1 ||
            exit_error "Failed to git checkout ${GIT_BRANCH}, check $GIT_INSTALL_LOG for details" $EC_FAIL_CHECKOUT
        popd > /dev/null
    else
        exit_error "Failed to pushd to ${INSTALL_PATH}, check ${GIT_INSTALL_LOG} for details" $EC_PUSHD_FAIL
    fi
else
    echo "No specific git branch requested, using default"
fi
$INSTALL_PATH/bin/subprojects-install >>"$GIT_INSTALL_LOG" 2>&1 ||
    exit_error "Failed to execute crucible-project install, check $GIT_INSTALL_LOG for details" $EC_FAIL_INSTALL

SYSCONFIG_CRUCIBLE_CLIENT_SERVER_REGISTRY="${CRUCIBLE_CLIENT_SERVER_REGISTRY}"
SYSCONFIG_CRUCIBLE_CLIENT_SERVER_AUTH=""
SYSCONFIG_CRUCIBLE_CLIENT_SERVER_TLS_VERIFY="\"true\""
if [ ! -z ${CRUCIBLE_CLIENT_SERVER_AUTH_FILE+x} ]; then
    SYSCONFIG_CRUCIBLE_CLIENT_SERVER_AUTH="\"${CRUCIBLE_CLIENT_SERVER_AUTH_FILE}\""
fi
if [ ! -z ${CRUCIBLE_CLIENT_SERVER_TLS_VERIFY+x} ]; then
    SYSCONFIG_CRUCIBLE_CLIENT_SERVER_TLS_VERIFY="\"${CRUCIBLE_CLIENT_SERVER_TLS_VERIFY}\""
fi

# native crucible install script already created this, only append
cat << _SYSCFG_ >> $SYSCONFIG
CRUCIBLE_USE_CONTAINERS=1
CRUCIBLE_USE_LOGGER=1
CRUCIBLE_CONTAINER_IMAGE=${CRUCIBLE_CONTROLLER_REGISTRY}
CRUCIBLE_CLIENT_SERVER_REPO=${SYSCONFIG_CRUCIBLE_CLIENT_SERVER_REGISTRY}
CRUCIBLE_CLIENT_SERVER_AUTH=${SYSCONFIG_CRUCIBLE_CLIENT_SERVER_AUTH}
CRUCIBLE_CLIENT_SERVER_TLS_VERIFY=${SYSCONFIG_CRUCIBLE_CLIENT_SERVER_TLS_VERIFY}
_SYSCFG_

if [ ${VERBOSE} == 1 ]; then
    cat ${GIT_INSTALL_LOG}
    echo
fi

echo "Installation is complete.  Run \"crucible help\" to see what's possible"
echo "You can also source /etc/profile.d/crucible_completions.sh (or re-login) to use tab completion for crucible"
echo
