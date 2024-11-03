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
DEFAULT_GIT_REPO="https://github.com/perftool-incubator/crucible"
DEFAULT_GIT_BRANCH="master"
GIT_REPO=""
GIT_BRANCH=""
GIT_TAG=""
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
EC_PULL_FAIL=16
EC_RELEASE_DEFAULT_REPO_ONLY=18
EC_RELEASE_CONFLICTS_WITH_BRANCH=19

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

    Usage: $0 --engine-registry <value> [ opt ]

        --engine-registry <full registry url>
        Engine registry.

    optional:

        --engine-auth-file <authentication file>
        Authentication file for pushing images to the remote registry.
  
        --engine-tls-verify true|false
        Use TLS verification.

        --controller-registry <full registry url>
        Controller registry (default is ${CRUCIBLE_CONTROLLER_REGISTRY}).

	--name <your full name>
        --email <your email address>
        Name and email of Crucible user.

        --git-repo <repo path/url>
        --git-branch <branch name>
        Repository and branch to install Crucible from.

        --release [<release>|select]
	Release tag <release> to install a locked-down version of Crucible.
        The option '--release select' lists all available releases on
        interactive prompt.

	--verbose
        Verbose mode that provides more debugging info.

	--list-releases
        Show all available release tags from the remote repository.

    --help
    Displays this usage output.

_USAGE_
}

# list available tags from the remote repository
function list_releases {
    # only default repo is supported for the release mechanism
    git ls-remote --tags \
        --sort='version:refname' \
        https://github.com/perftool-incubator/crucible.git \
        | awk -F/ '{print$NF}' \
        | grep -E '20[0-9]{2}\.[1234]'
}

# cleanup previous installation
function clean_old_install {
    local timestamp

    timestamp=$(date +%d-%m-%Y_%H:%M:%S)
    
    if [ -d $INSTALL_PATH ]; then
        old_install_path="/opt/crucible-moved-on-${timestamp}"
        echo "An existing installation of crucible exists and will be moved to $old_install_path"
        /bin/mv "$INSTALL_PATH" "$old_install_path"
        echo
    fi

    if [ -e ${SYSCONFIG} ]; then
        old_sysconfig="${SYSCONFIG}-moved-on-${timestamp}"
        echo "An existing crucible sysconfig file exists and will be moved to ${old_sysconfig}"
        /bin/mv "${SYSCONFIG}" "${old_sysconfig}"
        echo
    fi

    # reset the update tracker if there is any existing state
    rm -f "${USER_DIR}/update-status*" > /dev/null
}

# set name and email address
function identity {

    if [ -z "${CRUCIBLE_NAME}" ] && [ -z "${CRUCIBLE_EMAIL}" ]; then
        if [ -e "${IDENTITY}" ]; then
            echo "Sourcing ${IDENTITY}"
            . ${IDENTITY}
        fi
    fi

    if [ -z "${CRUCIBLE_NAME}" ]; then
            echo "Please enter your full name:"
            read CRUCIBLE_NAME
    fi

    if [ -z "${CRUCIBLE_EMAIL}" ]; then
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

# process release tag passed as an argument with '--release <TAG>'
# interactively select from a list of available releases (tags)
# if none is specified as <TAG>
function select_release {
    release="$1"
    if [ "$release" == "select" ]; then
	echo
        echo "Releases:"
        tags=( $(list_releases) )

        numopt=${#tags[@]}
        if [ $numopt -eq 0 ]; then
            echo "No available releases! Using default branch."

        else
            PS3="Select release option # [1-${numopt}]: "
            select release in ${tags[@]}; do
                echo ${tags[@]} | grep -w -q "$release"
                if [ $? -eq 0 ]; then
                    echo "Release '$REPLY) $release' has been selected."
                else
                    echo "Invalid option '$REPLY'! Using default branch."
                fi
                break
            done
        fi
    fi
    GIT_TAG="$release"
}

function update_repos_config() {
    local REPO BRANCH CRUCIBLE_PATH MODE
    local current_repo_location primary_branch
    local update_mode update_target
    REPO=${1}
    CRUCIBLE_PATH=${2}
    BRANCH=${3}

    REPO_FILE=${CRUCIBLE_PATH}/config/repos.json

    current_repo_location=$(jq_query ${REPO_FILE} '.official[] | select(.name == "crucible") | .repository')
    if [ "${REPO}" != "${current_repo_location}" ]; then
        echo "Setting crucible repository to '${REPO}' in '${REPO_FILE}'" >> ${GIT_INSTALL_LOG}
        jq_update ${REPO_FILE} repository --arg repository "${REPO}" '(.official[] | select(.name == "crucible") | .repository) |= $repository'
    fi

    if [ -z "${BRANCH}" -a -z "$(git branch --show-current)" ]; then
        # since we are not on a branch right now (immediately after a
        # clone) and no branch was requested, let's assume this is in
        # the CI environment and we have to do some special handling
        BRANCH="HEAD"
        echo "Detected CI environment, setting BRANCH=HEAD"
    fi

    primary_branch=$(jq_query ${REPO_FILE} '.official[] | select(.name == "crucible") | ."primary-branch"')
    checkout_target=$(jq_query ${REPO_FILE} '.official[] | select(.name == "crucible") | .checkout.target')
    checkout_mode=$(jq_query ${REPO_FILE} '.official[] | select(.name == "crucible") | .checkout.mode')
    if [ -n "${BRANCH}" ]; then
        update_mode=0
        update_target=0
        if [ "${BRANCH}" == "${primary_branch}" ]; then
            if [ "${BRANCH}" != "${checkout_target}" ]; then
                update_target=1
            fi
        else
            if [ "${BRANCH}" != "${checkout_target}" ]; then
                update_target=1

                if [ "${BRANCH}" == "HEAD" ]; then
                    echo "Detected CI environment, setting crucible primary-branch to '${BRANCH}' in '${REPO_FILE}'" >> ${GIT_INSTALL_LOG}
                    jq_update ${REPO_FILE} primary-branch --arg primary_branch ${BRANCH} '(.official[] | select(.name == "crucible") | ."primary-branch") |= $primary_branch'
                fi
            fi
        fi
        if echo "${BRANCH}" | grep -q "^\(20[0-9][0-9]\.[1234]\|version-test\|ci-verstion-test\)$"; then
            # this is a version install so lock it down
            update_mode=1
            MODE="locked"
        fi
        if [ ${update_mode} -eq 1 ]; then
            echo "Setting crucible checkout.mode to '${MODE}' in '${REPO_FILE}'" >> ${GIT_INSTALL_LOG}
            jq_update ${REPO_FILE} checkout.mode --arg checkout_mode ${MODE} '(.official[] | select(.name == "crucible") | .checkout.mode) |= $checkout_mode'
        fi
        if [ ${update_target} -eq 1 ]; then
            echo "Setting crucible checkout.target to '${BRANCH}' in '${REPO_FILE}'" >> ${GIT_INSTALL_LOG}
            jq_update ${REPO_FILE} checkout.target --arg checkout_target ${BRANCH} '(.official[] | select(.name == "crucible") | .checkout.target) |= $checkout_target'
        fi
    else
        git checkout ${checkout_target} >> ${GIT_INSTALL_LOG} 2>&1 ||
            exit_error "Failed to git checkout ${checkout_target}, checking ${GIT_INSTALL_LOG} for details" ${EC_FAIL_CHECKOUT}
    fi
}

longopts="name:,email:,help,list-releases,verbose"
longopts+=",client-server-registry:,client-server-auth-file:,client-server-tls-verify:"
longopts+=",engine-registry:,engine-auth-file:,engine-tls-verify:"
longopts+=",controller-registry:,git-repo:,git-branch:,release:"
opts=$(getopt -q -o "" --longoptions "$longopts" -n "$0" -- "$@");
if [ $? -ne 0 ]; then
    opts=$(echo "$@")
    exit_error "Unrecognized option specified: ${opts}" ${EC_INVALID_OPTION}
fi
eval set -- "$opts";
while true; do
    case "$1" in
        --client-server-tls-verify|--engine-tls-verify)
            shift;
            CRUCIBLE_ENGINE_TLS_VERIFY="$1"
            shift;
            ;;
        --client-server-registry|--engine-registry)
            shift;
            CRUCIBLE_ENGINE_REGISTRY="$1"
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
        --client-server-auth-file|--engine-auth-file)
            shift;
            CRUCIBLE_ENGINE_AUTH_FILE="$1"
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
        --list-releases)
	    shift;
	    list_releases
	    exit
	    ;;
	--release)
	    shift;
	    select_release "$1"
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

# --release conflicts with --git-repo or --git-branch
if [ -n "${GIT_TAG}" ]; then
    if [ -n "${GIT_REPO}" -a "${GIT_REPO}" != "${DEFAULT_GIT_REPO}" ]; then
        exit_error "Only default repo is supported for installing a release." $EC_RELEASE_DEFAULT_REPO_ONLY
    fi
    if [ -n "${GIT_BRANCH}" ]; then
        exit_error "Cannot install from release and specify '--git-branch'." $EC_RELEASE_CONFLICTS_WITH_BRANCH
    fi
fi

determine_git_install_source

# Override GIT_BRANCH **AFTER** determine function
if [ -n "${GIT_TAG}" ]; then
    GIT_BRANCH="${GIT_TAG}"
fi

if [ "`id -u`" != "0" ]; then
    exit_error "You must run this as root, exiting" $EC_FAIL_USER
fi

if [ -z ${CRUCIBLE_ENGINE_REGISTRY+x} ]; then
    exit_error "You must specify a registry with the --engine-registry option." $EC_FAIL_REGISTRY_UNSET
fi

identity

for dep in $DEPENDENCIES; do
    has_dependency $dep
done

if [ ! -z ${CRUCIBLE_ENGINE_AUTH_FILE+x} ]; then
    if [ ! -f $CRUCIBLE_ENGINE_AUTH_FILE ]; then
        exit_error "Crucible authentication file not found. See --engine-auth-file for details." $EC_AUTH_FILE_NOT_FOUND
    fi
fi

if [ ! -z ${CRUCIBLE_ENGINE_TLS_VERIFY+x} ]; then
    if [ "${CRUCIBLE_ENGINE_TLS_VERIFY}" != "true" -a "${CRUCIBLE_ENGINE_TLS_VERIFY}" != "false" ]; then
        exit_error "Incorrect Crucible client server tls verify option [${CRUCIBLE_ENGINE_TLS_VERIFY}].  See --engine-tls-verify for details." $EC_TLS_VERIFY_ERROR
    fi
fi

clean_old_install

echo "Installing crucible in $INSTALL_PATH"
echo "Using Git repo:   ${GIT_REPO}"
echo "Using Git branch: ${GIT_BRANCH}"
git_clone_cmd="git -c advice.detachedHead=false clone ${GIT_REPO} ${INSTALL_PATH}"
echo ${git_clone_cmd} > ${GIT_INSTALL_LOG}
${git_clone_cmd} >> $GIT_INSTALL_LOG 2>&1 ||
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

# make sure the branch being installed has jqlib support before
# attempting to use it
if [ -e ${INSTALL_PATH}/bin/jqlib ]; then
    source ${INSTALL_PATH}/bin/jqlib
    update_repos_config ${GIT_REPO} ${INSTALL_PATH} ${GIT_BRANCH}
fi

$INSTALL_PATH/bin/subprojects-install $GIT_TAG >>"$GIT_INSTALL_LOG" 2>&1 ||
    exit_error "Failed to execute crucible-project install, check $GIT_INSTALL_LOG for details" $EC_FAIL_INSTALL

SYSCONFIG_CRUCIBLE_ENGINE_REGISTRY="${CRUCIBLE_ENGINE_REGISTRY}"
SYSCONFIG_CRUCIBLE_ENGINE_AUTH=""
SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY=""
SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY="true"
if [ -n "${CRUCIBLE_ENGINE_AUTH_FILE}" ]; then
    SYSCONFIG_CRUCIBLE_ENGINE_AUTH="${CRUCIBLE_ENGINE_AUTH_FILE}"
fi
if [ -n "${CRUCIBLE_ENGINE_TLS_VERIFY}" ]; then
    SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY="${CRUCIBLE_ENGINE_TLS_VERIFY}"
fi

REGISTRIES_CFG=${INSTALL_PATH}/config/registries.json
REGISTRIES_CFG_SCHEMA=${INSTALL_PATH}/schema/registries.json

if [ -e ${INSTALL_PATH}/bin/_registries.lib ]; then
    source ${INSTALL_PATH}/bin/_registries.lib

    cat << _SYSCFG_ >> $SYSCONFIG
CRUCIBLE_USE_CONTAINERS=1
CRUCIBLE_USE_LOGGER=1
_SYSCFG_

    create_registries_json \
        ${CRUCIBLE_CONTROLLER_REGISTRY} \
        ${SYSCONFIG_CRUCIBLE_ENGINE_REGISTRY} \
        ${SYSCONFIG_CRUCIBLE_ENGINE_AUTH} \
        ${SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY}

    # when the 'base' file is sourced with this particular parameter
    # set it will force the registries.json to be validated
    CRUCIBLE_CFG_JSON_VALIDATION="yes"
    SESSION_ID="installer"
    CRUCIBLE_HOME=${INSTALL_PATH} source ${INSTALL_PATH}/bin/base
else
    SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY="\"${SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY}\""
    if [ -n "${SYSCONFIG_CRUCIBLE_ENGINE_AUTH_FILE}" ]; then
        SYSCONFIG_CRUCIBLE_ENGINE_AUTH="\"${SYSCONFIG_CRUCIBLE_ENGINE_AUTH}\""
    fi
    if [ -n "${CRUCIBLE_ENGINE_TLS_VERIFY}" ]; then
        SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY="\"${SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY}\""
    fi

    cat << _SYSCFG_ >> $SYSCONFIG
CRUCIBLE_USE_CONTAINERS=1
CRUCIBLE_USE_LOGGER=1
CRUCIBLE_CONTROLLER_IMAGE=${CRUCIBLE_CONTROLLER_REGISTRY}
CRUCIBLE_ENGINE_REPO=${SYSCONFIG_CRUCIBLE_ENGINE_REGISTRY}
CRUCIBLE_ENGINE_REPO_AUTH_TOKEN=${SYSCONFIG_CRUCIBLE_ENGINE_AUTH}
CRUCIBLE_ENGINE_REPO_TLS_VERIFY=${SYSCONFIG_CRUCIBLE_ENGINE_TLS_VERIFY}
_SYSCFG_
fi

if [ ${VERBOSE} == 1 ]; then
    echo
    echo "Contents of git install log: '${GIT_INSTALL_LOG}'"
    cat ${GIT_INSTALL_LOG}
    echo
    ${INSTALL_PATH}/bin/crucible repo info
    if [ -e ${INSTALL_PATH}/bin/jqlib ]; then
        echo
        ${INSTALL_PATH}/bin/crucible repo config show
    fi
    if [ -e ${REGISTRIES_CFG} ]; then
        echo
        echo "Contents of registries config file ${REGISTRIES_CFG}:"
        cat ${REGISTRIES_CFG}
    fi
fi

echo
echo "Pulling Crucible controller container image:"
podman --log-level error pull ${CRUCIBLE_CONTROLLER_REGISTRY} ||
    exit_error "Failed to pull controller image ${CRUCIBLE_CONTROLLER_REGISTRY}" ${EC_PULL_FAIL}

echo
echo "Installation is complete.  Run \"crucible help\" to see what's possible"
echo "You can also source /etc/profile.d/crucible_completions.sh (or re-login) to use tab completion for crucible"
echo
