# Copyright 2023-2024 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License version 3, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
# SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Basic lifecycle commands for a Craft Application."""
from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap
from typing import TYPE_CHECKING, Any


from craft_cli import CommandGroup, emit
from craft_parts.features import Features
from typing_extensions import override

from craft_application.commands import base


if TYPE_CHECKING:  # pragma: no cover
    import argparse


def get_lifecycle_command_group() -> CommandGroup:
    """Return the lifecycle related command group."""
    commands: list[type[_BaseLifecycleCommand]] = [
        CleanCommand,
        PullCommand,
        OverlayCommand,
        BuildCommand,
        StageCommand,
        PrimeCommand,
        PackCommand,
    ]
    if not Features().enable_overlay:
        commands.remove(OverlayCommand)

    return CommandGroup(
        "Lifecycle",
        commands,  # type: ignore[arg-type] # https://github.com/canonical/craft-cli/pull/157
    )


class ProException(Exception):
    """Base Exception class for ProServices."""

    pass


class InvalidProStatus(ProException):
    """Raised when ProServices does not environment Ubuntu Pro configuration."""

    pass


class UnableToImportProApi(ProException):
    """Raised when uaclient module is not found."""

    pass


class ProServices(set[str]):
    """Class for managing pro-services within the lifecycle."""

    separator: str = ","
    build_service_scope: set[str] = {
        "esm-apps",
        "esm-infa",
        "fips-preview",
        "fips-updates",
    }

    def __str__(self) -> str:
        """Convert to string for display to user."""

        result = ", ".join(self)

        return result

    @classmethod
    def from_csv(cls, services: str) -> ProServices:
        """Create a new ProServices instance from a csv string."""

        split = [service.strip() for service in services.split(cls.separator)]
        result = cls(split)

        return result

    @classmethod
    def pro_attached(cls) -> bool:
        """Returns True if environment is attached to Ubuntu Pro."""

        # TODO: fix pyright when uaclient module is not available
        try:
            import uaclient  # pyright: ignore

            response = (  # pyright: ignore
                uaclient.api.u.pro.status.is_attached.v1.is_attached()  # pyright: ignore
            )

            result = response.is_attached  # pyright: ignore

            return result  # pyright: ignore

        except ImportError as exc:
            raise UnableToImportProApi() from exc

    @classmethod
    def pro_services(cls) -> ProServices:
        """Return set of enabled Ubuntu Pro services in the environment.
        The returned set only includes services relevant to lifecycle commands."""

        try:
            import uaclient  # pyright: ignore

            response = (  # pyright: ignore
                uaclient.api.u.pro.status.enabled_services.v1.enabled_services()  # pyright: ignore
            )

            service_names = {  # pyright: ignore
                service.name for service in response.enabled_services  # pyright: ignore
            }

            # remove any services that aren't relevant to build services
            service_names = service_names.intersection(  # pyright: ignore
                cls.build_service_scope  # pyright: ignore
            )

            result = cls(service_names)  # pyright: ignore

            return result

        except ImportError as exc:
            raise UnableToImportProApi() from exc

    def validate(self):
        """Validate the environment against pro services specified in this ProServices instance."""

        try:

            # first, check Ubuntu Pro status
            # Since we extend the set class, cast ourselves to bool to check if we empty. if we are not
            # empty, this implies we require pro services.
            if self.pro_attached() != bool(self):
                if bool(self):
                    raise InvalidProStatus(
                        "This command requires a Ubuntu Pro environment, "
                        "but no Ubuntu Pro attachment was found."
                    )
                else:
                    raise InvalidProStatus(
                        "This command requires a non-Ubuntu Pro environment, "
                        "but Ubuntu Pro attachment was found."
                    )

            # TODO:
            # - Optimization: could we just check if the services we need are available and skip
            #   checking if we are attached? or could we return early here if we are detached and did not request pro services?
            # - What if pro services are attached, but no services within the build_service_scope are used?

            # second, check that the set of enabled pro services in the environment matches
            # the services specified in this set
            if (services := self.pro_services()) != self:
                raise InvalidProStatus(
                    "The requested pro services do not match the services enabled in the environment.\n"
                    f"Enabled Pro Services: {services}\n"
                    f"Specified Pro Services: {self}\n"
                )

        except UnableToImportProApi as exc:
            # TODO: if we ever run in environments where the uaclient module
            # is not available we may need to add it to the path, or suppress this
            # exception on platforms that do not support uaclient.
            #
            # If we have difficulty importing uaclient, we may want to
            # revert back to calling the command directly via subprocess

            raise exc


class _BaseLifecycleCommand(base.ExtensibleCommand):
    """Base class for lifecycle-related commands.

    All lifecycle commands must know where to execute (locally or in a build
    environment) but do not have to provide shell access into the environment.
    """

    @override
    def _run(self, parsed_args: argparse.Namespace, **kwargs: Any) -> None:
        emit.trace(f"lifecycle command: {self.name!r}, arguments: {parsed_args!r}")

    @override
    def _fill_parser(self, parser: argparse.ArgumentParser) -> None:
        super()._fill_parser(parser)  # type: ignore[arg-type]

        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--destructive-mode",
            action="store_true",
            help="Build in the current host",
        )
        group.add_argument(
            "--use-lxd",
            action="store_true",
            help="Build in a LXD container.",
        )

        parser.add_argument(
            "--pro",
            type=ProServices.from_csv,
            metavar="<pro-services>",
            help="Enable Ubuntu Pro service by name. Each service should be separated by a comma.",
            default=ProServices(),
        )

    @override
    def get_managed_cmd(self, parsed_args: argparse.Namespace) -> list[str]:
        cmd = super().get_managed_cmd(parsed_args)

        cmd.extend(parsed_args.parts)

        return cmd

    @override
    def provider_name(self, parsed_args: argparse.Namespace) -> str | None:
        return "lxd" if parsed_args.use_lxd else None

    @override
    def run_managed(self, parsed_args: argparse.Namespace) -> bool:
        """Return whether the command should run in managed mode or not.

        The command will run in managed mode unless the `--destructive-mode` flag
        is passed OR `CRAFT_BUILD_ENVIRONMENT` is set to `host`.
        """
        if parsed_args.destructive_mode:
            emit.debug(
                "Not running managed mode because `--destructive-mode` was passed"
            )
            return False

        build_env = os.getenv("CRAFT_BUILD_ENVIRONMENT")
        if build_env and build_env.lower().strip() == "host":
            emit.debug(
                f"Not running managed mode because CRAFT_BUILD_ENVIRONMENT={build_env}"
            )
            return False

        return True


class LifecycleCommand(_BaseLifecycleCommand):
    """A command that will run the lifecycle and can shell into the environment.

    LifecycleCommands do not require a part. For example 'pack' will run
    the lifecycle but cannot be run on a specific part.
    """

    @override
    def _fill_parser(self, parser: argparse.ArgumentParser) -> None:
        super()._fill_parser(parser)

        if self._should_add_shell_args():
            group = parser.add_mutually_exclusive_group()
            group.add_argument(
                "--shell",
                action="store_true",
                help="Shell into the environment in lieu of the step to run.",
            )
            group.add_argument(
                "--shell-after",
                action="store_true",
                help="Shell into the environment after the step has run.",
            )

        parser.add_argument(
            "--debug",
            action="store_true",
            help="Shell into the environment if the build fails.",
        )

        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--platform",
            type=str,
            metavar="name",
            default=os.getenv("CRAFT_PLATFORM"),
            help="Set platform to build for",
        )
        group.add_argument(
            "--build-for",
            type=str,
            metavar="arch",
            default=os.getenv("CRAFT_BUILD_FOR"),
            help="Set architecture to build for",
        )

    @override
    def get_managed_cmd(self, parsed_args: argparse.Namespace) -> list[str]:
        """Get the command to run in managed mode.

        :param parsed_args: The parsed arguments used.
        :returns: A list of strings ready to be passed into a craft-providers executor.
        :raises: RuntimeError if this command is not supposed to run managed.
        """
        cmd = super().get_managed_cmd(parsed_args)

        if getattr(parsed_args, "shell", False):
            cmd.append("--shell")
        if getattr(parsed_args, "shell_after", False):
            cmd.append("--shell-after")

        return cmd

    @override
    def _run(
        self,
        parsed_args: argparse.Namespace,
        step_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Run a lifecycle step command."""
        super()._run(parsed_args)

        shell = getattr(parsed_args, "shell", False)
        shell_after = getattr(parsed_args, "shell_after", False)
        debug = getattr(parsed_args, "debug", False)

        step_name = step_name or self.name

        if shell:
            previous_step = self._services.lifecycle.previous_step_name(step_name)
            step_name = previous_step
            shell_after = True

        try:
            self._run_lifecycle(parsed_args, step_name)
        except Exception as err:
            if debug:
                emit.progress(str(err), permanent=True)
                _launch_shell()
            raise

        if shell_after:
            _launch_shell()

    def _run_lifecycle(
        self,
        parsed_args: argparse.Namespace,  # noqa: ARG002 (unused argument is for subclasses)
        step_name: str | None = None,
    ) -> None:
        """Run the lifecycle."""
        self._services.lifecycle.run(step_name=step_name)

    def _run_post_prime_steps(self) -> None:
        """Run post-prime steps."""
        self._services.package.update_project()
        self._services.package.write_metadata(self._services.lifecycle.prime_dir)

    @staticmethod
    def _should_add_shell_args() -> bool:
        return True


class LifecyclePartsCommand(LifecycleCommand):
    """A command that can run the lifecycle for a particular part."""

    # All lifecycle-related commands need a project to work
    always_load_project = True

    @override
    def _fill_parser(self, parser: argparse.ArgumentParser) -> None:
        super()._fill_parser(parser)  # type: ignore[arg-type]
        parser.add_argument(
            "parts",
            metavar="part-name",
            type=str,
            nargs="*",
            help="Optional list of parts to process",
        )

    @override
    def _run_lifecycle(
        self, parsed_args: argparse.Namespace, step_name: str | None = None
    ) -> None:
        """Run the lifecycle, optionally for a part or list of parts."""
        self._services.lifecycle.run(
            step_name=step_name,
            part_names=parsed_args.parts,
        )


class PullCommand(LifecyclePartsCommand):
    """Command to pull parts."""

    name = "pull"
    help_msg = "Download or retrieve artifacts defined for a part"
    overview = textwrap.dedent(
        """
        Download or retrieve artifacts defined for a part. If part names
        are specified only those parts will be pulled, otherwise all parts
        will be pulled.
        """
    )


class OverlayCommand(LifecyclePartsCommand):
    """Command to overlay parts."""

    name = "overlay"
    help_msg = "Create part layers over the base filesystem."
    overview = textwrap.dedent(
        """
        Execute operations defined for each part on a layer over the base
        filesystem, potentially modifying its contents.
        """
    )


class BuildCommand(LifecyclePartsCommand):
    """Command to build parts."""

    name = "build"
    help_msg = "Build artifacts defined for a part"
    overview = textwrap.dedent(
        """
        Build artifacts defined for a part. If part names are specified only
        those parts will be built, otherwise all parts will be built.
        """
    )


class StageCommand(LifecyclePartsCommand):
    """Command to stage parts."""

    name = "stage"
    help_msg = "Stage built artifacts into a common staging area"
    overview = textwrap.dedent(
        """
        Stage built artifacts into a common staging area. If part names are
        specified only those parts will be staged. The default is to stage
        all parts.
        """
    )


class PrimeCommand(LifecyclePartsCommand):
    """Command to prime parts."""

    name = "prime"
    help_msg = "Prime artifacts defined for a part"
    overview = textwrap.dedent(
        """
        Prepare the final payload to be packed, performing additional
        processing and adding metadata files. If part names are specified only
        those parts will be primed. The default is to prime all parts.
        """
    )

    @override
    def _run(
        self,
        parsed_args: argparse.Namespace,
        step_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Run the prime command."""
        super()._run(parsed_args, step_name=step_name)
        self._run_post_prime_steps()


class PackCommand(LifecycleCommand):
    """Command to pack the final artifact."""

    always_load_project = True

    name = "pack"
    help_msg = "Create the final artifact"
    overview = textwrap.dedent(
        """
        Process parts and create the final artifact.
        """
    )

    @override
    def _fill_parser(self, parser: argparse.ArgumentParser) -> None:
        super()._fill_parser(parser)

        parser.add_argument(
            "--output",
            "-o",
            type=pathlib.Path,
            default=pathlib.Path(),
            help="Output directory for created packages.",
        )

    @override
    def _run(
        self,
        parsed_args: argparse.Namespace,
        step_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Run the pack command."""
        if step_name not in ("pack", None):
            raise RuntimeError(f"Step name {step_name} passed to pack command.")
        super()._run(parsed_args, step_name="prime")
        self._run_post_prime_steps()

        emit.progress("Packing...")
        packages = self._services.package.pack(
            self._services.lifecycle.prime_dir, parsed_args.output
        )

        if not packages:
            emit.progress("No packages created.", permanent=True)
        elif len(packages) == 1:
            emit.progress(f"Packed {packages[0].name}", permanent=True)
        else:
            package_names = ", ".join(pkg.name for pkg in packages)
            emit.progress(f"Packed: {package_names}", permanent=True)

    @staticmethod
    @override
    def _should_add_shell_args() -> bool:
        return False


class CleanCommand(_BaseLifecycleCommand):
    """Command to remove part assets."""

    always_load_project = True
    name = "clean"
    help_msg = "Remove a part's assets"
    overview = textwrap.dedent(
        """
        Clean up artifacts belonging to parts. If no parts are specified,
        remove the packing environment.
        """
    )

    @override
    def _fill_parser(self, parser: argparse.ArgumentParser) -> None:
        super()._fill_parser(parser)  # type: ignore[arg-type]
        parser.add_argument(
            "parts",
            metavar="part-name",
            type=str,
            nargs="*",
            help="Optional list of parts to process",
        )

    @override
    def _run(
        self,
        parsed_args: argparse.Namespace,
        **kwargs: Any,
    ) -> None:
        """Run the clean command.

        The project's work directory will be cleaned if:
        - the `--destructive-mode` flag is provided OR
        - `CRAFT_BUILD_ENVIRONMENT` is set to `host` OR
        - no list of specific parts to clean is provided

        Otherwise, it will clean an instance.
        """
        super()._run(parsed_args)

        if not super().run_managed(parsed_args) or not self._should_clean_instances(
            parsed_args
        ):
            self._services.lifecycle.clean(parsed_args.parts)
        else:
            self._services.provider.clean_instances()

    @override
    def run_managed(self, parsed_args: argparse.Namespace) -> bool:
        """Return whether the command should run in managed mode or not.

        Clean will run in managed mode unless:
        - the `--destructive-mode` flag is provided OR
        - `CRAFT_BUILD_ENVIRONMENT` is set to `host` OR
        - a list of specific parts to clean is provided
        """
        if not super().run_managed(parsed_args):
            return False

        return not self._should_clean_instances(parsed_args)

    @staticmethod
    def _should_clean_instances(parsed_args: argparse.Namespace) -> bool:
        return not bool(parsed_args.parts)


def _launch_shell() -> None:
    """Launch a user shell for debugging environment."""
    emit.progress("Launching shell on build environment...", permanent=True)
    with emit.pause():
        subprocess.run(["bash"], check=False)
