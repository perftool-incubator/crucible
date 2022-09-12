This directory contains information to build a crucible-controller container image.  Crucible uses containers so that little or no software dependencies are required to be installed.  The actual container usage is mostly transparent to the user.  The main crucible script, `crucible`, is still installed on the controller host, and this script along with others, launches containers dynamically as needed for the user's particular needs at that time.  Some containers, once launched, are persistent, like `crucible-redis`, and some are temporary, like when running, `crucible run <benchmark>`.

Crucible includes a subproject dedicated to taking container requirements from mulitple sources, merging them, then building a container.  While this is used primarily to build container images on-demand for running benchmarks and tools, it can also be used to build the crucible-controller's container image.  Included in this directory is a 'requirement' file for this subproject, [workshop](https://github.com/perftool-incubator/workshop), and in this README are example workshop commands to build the crucible-controller container image and upload to a registry.

The file, [controller-workshop.json](controller-workshop.json), included in this directory is used by workshop to build a container image.  While workshop can process multiple requirement files, all of the crucible-controller's requirments are documented in this one file.  This includes all software requirements for all crucible-controller functions, such as buildah for workshop and redis for roadblock.

Note that building this container image may not be necessary.  Public container images are available at `quay.io/crucible/controller`.  Building a new container image should only be necessary if you want to add functionality to crucible that requires new software to be included in the image.  This implies you have forked the crucible project (which is perfectly fine) and you are maintaining this forked version with changes from upstream.  If you do this, keep in mind that you will need to create a new container project in a container registry, to store your forked crucible container images, and you will have to alter the crucible-controller installer to have it reference your container project.  If you are creating this container image because you have a new or updated benchmark or tool, STOP.  The crucible-controller image does not require any benchmark or tool software.  All benchmark and tool software dependencies are handled automatically when the end-user issues a `crucible run <benchmark>` on the crucible-controller host.  This README is about "building the machine that builds the machines".

While running crucible-controller is done with almost no software dependencies (and therefore users have to install almost nothing extra to support running crucible), building a crucible-controller container image does require some software, namely buildah, perl, and several perl modules.  The crucible-controller itself is now capable of bootstrapping itself -- meaning you can build the controller using the controller.  This is done by first running, `crucible console`, which will drop you into the pre-existing crucible-container image, which already does have all software installed, and then you can run the workhop command below to generate the crucible container image. 

To build the container image:

```
crucible console
cd /opt/crucible/workshop
./build-controller.sh
```

This process may take a while, as it is downloading a Linux container and installing many software packages.

To upload a container image run (you may need to log in to your container registry service first):

```
crucible console
cd /opt/crucible/workshop
podman login <registry>
./push-controller testing
```

You will need write acces to your container registry project, and any users who wish to install this image will need read access.  By default the crucible installer assumes public read access to the crucible-controller image.
