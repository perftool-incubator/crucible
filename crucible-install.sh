#!/usr/bin/env bash
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=bash
# -*- mode: sh; indent-tabs-mode: nil; sh-basic-offset: 4 -*-

# Installer Settings
SYSCONFIG="/etc/sysconfig/crucible"
DEPENDENCIES="podman git"
INSTALL_PATH="/opt/crucible"
GIT_REPO="https://github.com/perftool-incubator/crucible.git"
GIT_INSTALL_LOG="/tmp/crucible-git-install.log"

# User Exit Codes
EC_DEFAULT_EC=1
EC_FAIL_USER=3
EC_FAIL_CLONE=4
EC_FAIL_INSTALL=5
EC_AUTH_FILE_NOT_FOUND=6
EC_FAIL_DEPENDENCY=7
EC_FAIL_REGISTRY_UNSET=8
EC_FAIL_AUTH_UNSET=9
EC_INVALID_OPTION=10
EC_UNEXPECTED_ARG=11

function exit_error {
    # Send message to stderr
    printf '\n%s\n\n' "$1" >&2
    # Return a code specified by $2 or 1 by default
    exit "${2-1}"
}

function usage {
    cat <<_USAGE_

    Crucible installer script.

    Usage: $0 --registry <value> --auth-file <value> [ opt ]

    --registry <registry/crucible>
    --auth-file <authentication file>

    optional:
        --name <your full name>
        --email <your email address>

    --help [displays this usage output]
_USAGE_
}

function identity {
    identity="$HOME/.crucible/identity"
    if [ -e $identity ]; then
        echo "Sourcing $identity"
        . $identity
    else
        mkdir -p "$HOME/.crucible"
    fi

    if [ -z "$CRUCIBLE_NAME" ]; then
        echo "Please enter your full name:"
        read CRUCIBLE_NAME
        echo "CRUCIBLE_NAME=\"$CRUCIBLE_NAME\"" >>"$identity"
    fi
    if [ -z "$CRUCIBLE_EMAIL" ]; then
        echo "Please enter your email address:"
        read CRUCIBLE_EMAIL
        echo "CRUCIBLE_EMAIL=\"$CRUCIBLE_EMAIL\"" >>"$identity"
    fi
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

longopts="name:,email:,registry:,auth-file:,help"
opts=$(getopt -q -o "" --longoptions "$longopts" -n "$0" -- "$@");
if [ $? -ne 0 ]; then
    exit_error "Unrecognized option specified: $@" $EC_INVALID_OPTION
fi
eval set -- "$opts";
while true; do
    case "$1" in
       --registry)
            shift;
            CRUCIBLE_REGISTRY="$1"
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
        --auth-file)
            shift;
            CRUCIBLE_AUTH_FILE="$1"
            shift;
            ;;
        --help)
            shift;
            usage
            exit
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

if [ -z ${CRUCIBLE_REGISTRY+x} ]; then
    exit_error "You must specify a registry with the --registry option." $EC_FAIL_REGISTRY_UNSET
fi

if [ -z ${CRUCIBLE_AUTH_FILE+x} ]; then
    exit_error "You must specify an authentication file with the --auth-file option." $EC_FAIL_AUTH_UNSET
fi

identity

for dep in $DEPENDENCIES; do
    has_dependency $dep
done

if [ -d $INSTALL_PATH ]; then
    old_install_path="/opt/crucible-moved-on-`date +%d-%m-%Y_%H:%M:%S`"
    echo "An existing installation of crucible exists and will be moved to $old_install_path"
    /bin/mv "$INSTALL_PATH" "$old_install_path"
fi

echo "Installing crucible in $INSTALL_PATH"
git clone $GIT_REPO $INSTALL_PATH > $GIT_INSTALL_LOG 2>&1 ||
    exit_error "Failed to git clone $GIT_REPO, check $GIT_INSTALL_LOG for details" $EC_FAIL_CLONE
$INSTALL_PATH/bin/subprojects-install >>"$GIT_INSTALL_LOG" 2>&1 ||
    exit_error "Failed to execute crucbile-project install, check $GIT_INSTALL_LOG for details" $EC_FAIL_INSTALL

if [ ! -f $CRUCIBLE_AUTH_FILE ]; then
    exit_error "Crucible authentication file not found. See --auth-file for details." $EC_AUTH_FILE_NOT_FOUND
fi

# native crucible install script already created this, only append
cat << _SYSCFG_ >> $SYSCONFIG
CRUCIBLE_USE_CONTAINERS=1
CRUCIBLE_USE_LOGGER=1
CRUCIBLE_CONTAINER_IMAGE=$CRUCIBLE_REGISTRY/controller:latest
CRUCIBLE_CLIENT_SERVER_REPO=$CRUCIBLE_REGISTRY/client-server
CRUCIBLE_CLIENT_SERVER_AUTH="$CRUCIBLE_AUTH_FILE"
_SYSCFG_

echo "Installation is complete.  Run \"crucible help\" to see what's possible"
echo "You can also source /etc/profile.d/crucible_completions.sh (or re-login) to use tab completion for crucible"
echo

