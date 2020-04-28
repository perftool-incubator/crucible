#!/bin/bash
# This builds and pushes a container image for crucible, to be used for publishing a release
# of crucible which is runnable by users via podman or docker..
# This does not use workshop to build the container, as workshop may not be available in
# some environments.
# This script is a work-in-progress and may end up being used for CI as well.
set -e

if [ `id -u` -ne 0 ]; then
    echo "You should run this as root, exiting"
    exit 1
fi

#if [ "$#" -ne 1 -o ! -e $1 ]; then
if [ -z "$1" -o ! -e "$1" ]; then
    echo "usage: [sudo] ./create-container.sh <registry-auth-file>"
    echo "       <registry-auth-file> = typically $HOME/.docker/config.json"
    exit 1
fi

authfile="$1"
registry="quay.io/atheurer"
userenv="centos8"
userenv_src="registry.centos.org/centos/centos:8"
buildah_container_name="${userenv}_crucible"
podman_container_name="crucible-alpha"
rpm_packages="gcc make glibc-langpack-en perl-App-cpanminus git python3 redis"
cpan_packages="Coro JSON JSON::XS Data::Dumper Digest::SHA Getopt::Long DBI DBD::SQLite Data::UUID"

# Clean up any existing local builds
echo "Checking for existing podman container $podman_container_name"
podman_container_id="`podman ps -a | grep -- "$podman_container_name" | awk '{print $1}'`"
if [ ! -z "$podman_container_id" ]; then
    echo "Removing podman container $podman_container_id"
    podman rm "$podman_container_id"
fi
echo "Checking for existing buildah container $buildah_container_name"
buildah_container_id=`buildah containers | grep -- "$buildah_container_name" | awk '{print $1}'`
if [ ! -z "$buildah_container_id" ]; then
    echo "Removing buildah container $buildah_container_id"
    buildah rm $buildah_container_id
fi

# Create a new image
echo "Creating buildah container $buildah_container_name from $userenv_src"
buildah --name $buildah_container_name from $userenv_src
echo "Installing packages"
buildah run $buildah_container_name -- /bin/bash -c \
        "yum install -y $rpm_packages; cpanm $cpan_modules;\
         pip3 install redis;\
         git clone https://github.com/perftool-incubator/crucible.git /opt/crucible;\
         /opt/crucible/bin/install"
buildah_container_id=`buildah containers | grep $buildah_container_name | awk '{print $1}'`
buildah commit $buildah_container_id $registry/$buildah_container_name
buildah_image_id=`buildah images | grep -- "$registry/$buildah_container_name" | awk '{print $3}'`

# Push this image to the registry
echo "Pushing $buildah_image_id to $registry as $podman_container_name"
buildah push --authfile $authfile $buildah_image_id docker://$registry/$podman_container_name:latest
