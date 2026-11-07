"""`sandboxed-devenv image ...`: the base image that every backend is launched from (control role, M2)."""

from sandboxed_devenv import common


def register(subparsers):
    image = common.add_group(subparsers, "image", "the base image backends are launched from (control)")

    build = image.add_parser(
        "build",
        help="download the Ubuntu cloud image, boot it with the base cloud-init, clean, power off, register",
    )
    build.add_argument("--force", action="store_true", help="rebuild even when the versions have not changed")
    build.set_defaults(handler=cmd_build)

    ls = image.add_parser("ls", help="base images and their versions")
    ls.set_defaults(handler=cmd_ls)

    rm = image.add_parser("rm", help="delete a base image")
    rm.add_argument("image", metavar="IMAGE", help="the image as `image ls` names it")
    rm.set_defaults(handler=cmd_rm)


def cmd_build(args) -> int:
    retval = common.not_implemented("image build", "M2")
    return retval


def cmd_ls(args) -> int:
    retval = common.not_implemented("image ls", "M2")
    return retval


def cmd_rm(args) -> int:
    retval = common.not_implemented("image rm", "M2")
    return retval
