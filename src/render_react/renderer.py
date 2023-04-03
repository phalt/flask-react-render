from __future__ import annotations

import re
import typing
from functools import wraps
from os.path import exists
from pathlib import Path

import flask
import structlog

from src import settings
from src.apis.types import NoneType, generate_interfaces
from src.app import app
from src.render_react.converter import converter
from src.utils import unwrap

log = structlog.getLogger(__name__)


def _build_render_context_for_base_template() -> typing.Dict[str, typing.Any]:
    """
    Return the render context required for base.jinja2
    """
    blueprint_id = unwrap(flask.request.blueprint).replace(".", "-")

    html_classes = [f"blueprint-{blueprint_id}"]

    return {
        "html_classes": html_classes,
        "html_id": f"endpoint-{unwrap(flask.request.endpoint).replace('.', '-')}",
        "is_development": settings.in_dev_environment,
    }


class render_html:
    def __init__(
        self,
        template=None,
    ):
        self.template = template

    def __call__(self, f, template=None):
        @wraps(f)
        def wrapped(*args, **kwargs):
            response = f(*args, **kwargs)

            if not isinstance(response, dict):
                return response

            template = self.template

            if template is None:
                directory = "/".join(f.__module__.split(".")[2:])
                template = f"/{directory}/{f.__name__}.jinja2"

            log.info(f"Looking for {template}...")

            out = flask.render_template(
                template,
                **response,
                **_build_render_context_for_base_template(),
                **getattr(flask.g, "context", {}),
            )

            status = 200
            headers = {
                "Content-Type": "text/html; charset=utf-8",
            }

            return out, status, headers

        return wrapped


class render_react:
    """
    Decorate a flask endpoint such that the response will be a page rendered with a React component.

    Example:

        @attr.define
        class PreferencesProps:
            max_widgets: int

        @blueprint.route("/get-preferences")
        @render_react()
        def preferences() -> PreferencesProps:
            return PreferencesProps(
                max_widgets=4,
            )

    The return value can either be `None`, or an attrs class. If it's an attrs class, it will be used as props for the
    React component.
    """

    template: str

    def __init__(self):
        self.template = "render_react.jinja2"

    def __call__(self, view_function):
        from src.apis.types_manager import api_route_type_manager

        self.view_function = view_function
        self.view_function_types = typing.get_type_hints(self.view_function)
        self.return_type = self.view_function_types.pop("return", None)
        self.module = re.sub(r".*\.", "", view_function.__module__)
        self.name = view_function.__name__

        # At server start, write out the typescript type file for the props, if the view function returns them.
        self._write_typescript_type_file()

        @wraps(view_function)
        def wrapped(*args, **kwargs):
            """Handles a request to the endpoint that the view function is serving.

            Outputs a standard templated flask response, using a template that renders a React component.
            """
            response = view_function(*args, **kwargs)

            render_react_context = {
                "react_entrypoint_filename": f"js/template/{self.module}/{self.name}.tsx",
                "base_data": {
                    "urlMap": api_route_type_manager.get_url_map(),
                },
            }

            html = flask.render_template(
                self.template,
                __render_react_response=response,
                props=converter.unstructure(response),
                **_build_render_context_for_base_template(),
                **render_react_context,
            )
            status = 200
            headers = {
                "Content-Type": "text/html; charset=utf-8",
            }

            return html, status, headers

        return wrapped

    def _write_typescript_type_file(self):
        if self.return_type == NoneType:
            return

        # The endpoint has declared a return type. Endpoints are allowed to declare they return None. If they don't
        # (which will be the vast majority of them), we treat the return type as props for the page-level react
        # component.
        # If the endpoint declares a return type, it must be an attrs class. That way, we can propagate the typing on
        # the class to the frontend.
        assert getattr(self.return_type, "__attrs_attrs__", False), (
            f"Endpoint {self.module}.{self.name} missing attrs return annotation (which is required by"
            f"@render_react)."
        )

        # If a react component file for this view function doesn't exist yet, create a basic one
        _write_react_page_file(module=self.module, endpoint=self.name)

        _write_typescript_file(
            type_data=self._generate_typescript_type_file_contents(),
            module=self.module,
            endpoint=self.name,
        )

    def _generate_typescript_type_file_contents(self) -> str:
        typescript_imports, typescript_interfaces = generate_interfaces(
            self.return_type, name="PageProps", default_export=True
        )

        export_string = "// This file is generated by @render_react in admin, changes will be overwritten\n\n"

        if typescript_imports:
            export_string += typescript_imports.render()
            export_string += "\n"

        export_string += typescript_interfaces.render()

        return export_string.strip() + "\n"


def _write_react_page_file(module: str, endpoint: str) -> None:
    """
    Create a simple template file for a React page component if one does not exist yet.
    Will only run if ENVIRONMENT is set to development
    """
    if not settings.in_dev_environment:
        return
    react_page_file_path = (
        Path(app.root_path) / "js" / "template" / module / f"{endpoint}.tsx"
    )
    template_path = Path(app.root_path) / "template" / "render_react.template"

    if not exists(react_page_file_path):
        log.info(
            "Creating new react page for this endpoint",
            module=module,
            endpoint=endpoint,
            filename=str(react_page_file_path),
        )
        with template_path.resolve().open("r") as template_file:
            template_file_content = template_file.read()
        react_page_file_path.parent.mkdir(exist_ok=True)
        with react_page_file_path.resolve().open("w+") as new_file:
            new_file.write(template_file_content)
    return


def _write_typescript_file(
    *,
    module: str,
    endpoint: str,
    type_data: typing.Optional[str],
) -> None:
    """Intelligently writes typing data to the named file.

    Ensures the directory the file is meant to be in is created, if it doesn't exist already, and removes the directory
    the file was meant to be in if the file has been removed because the type data is gone.

    Only writes the file if the type data has changed to prevent excessive writes.

    Doesn't attempt to write the files unless the server is running in development mode.
    """
    if settings.ENVIRONMENT != "development":
        return

    typescript_file_path = (
        Path(app.root_path) / "js" / "template" / module / f"{endpoint}.type.ts"
    ).resolve()
    try:
        with typescript_file_path.open() as fh:
            existing_type_data = fh.read()
    except IOError:
        existing_type_data = None

    if type_data != existing_type_data:
        if type_data:
            log.info(
                "writing new type data",
                module=module,
                endpoint=endpoint,
                filename=str(typescript_file_path),
            )

            # Create the directory if it's not already there.
            typescript_file_path.parent.mkdir(exist_ok=True)

            with typescript_file_path.open("w") as fh:
                fh.write(type_data)
        else:
            log.info(
                "removing type data",
                module=module,
                endpoint=endpoint,
                filename=str(typescript_file_path),
            )

            typescript_file_path.unlink()

            # Remove the directory if empty. The pythonic way might be to try it and catch the exception, but in this
            # case we'd rather not even try if we have even an inkling that there might still be a file in there.
            if any(typescript_file_path.parent.iterdir()):
                typescript_file_path.parent.rmdir()
