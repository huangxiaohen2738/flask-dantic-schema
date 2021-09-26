import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

from humps import camelize, decamelize
from pydantic import BaseModel
from pydantic.json import pydantic_encoder
from pydantic.schema import model_schema
from flask import Flask, current_app, render_template_string
from flask.typing import ResponseReturnValue
from flask.json import JSONDecoder as QuartJSONDecoder, \
    JSONEncoder as QuartJSONEncoder

from .typing import ServerObject
from .validation import (
    DataSource,
    FLASK_SCHEMA_QUERYSTRING_ATTRIBUTE,
    FLASK_SCHEMA_REQUEST_ATTRIBUTE,
    FLASK_SCHEMA_RESPONSE_ATTRIBUTE,
)

FLASK_SCHEMA_HIDDEN_ATTRIBUTE = "_flask_schema_hidden"
REF_PREFIX = "#/components/schemas/"

PATH_RE = re.compile("<(?:[^:]*:)?([^>]+)>")

REDOC_TEMPLATE = """
<head>
  <title>{{ title }}</title>
  <style>
    body {
      margin: 0;
      padding: 0;
    }
  </style>
</head>
<body>
  <redoc spec-url="{{ openapi_path }}"></redoc>
  <script src="{{ redoc_js_url }}"></script>
</body>
"""

SWAGGER_TEMPLATE = """
<head>
  <link type="text/css" rel="stylesheet" href="{{ swagger_css_url }}">
  <title>{{ title }}</title>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{{ swagger_js_url }}"></script>
  <script>
    const ui = SwaggerUIBundle({
      deepLinking: true,
      dom_id: "#swagger-ui",
      layout: "BaseLayout",
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset
      ],
      showExtensions: true,
      showCommonExtensions: true,
      url: "{{ openapi_path }}"
    });
  </script>
</body>
"""


def hide_route(func: Callable) -> Callable:
    """Mark the func as hidden.

    This will prevent the route from being included in the
    autogenerated documentation.
    """
    setattr(func, FLASK_SCHEMA_HIDDEN_ATTRIBUTE, True)
    return func


class PydanticJSONEncoder(QuartJSONEncoder):
    def default(self, object_: Any) -> Any:
        return pydantic_encoder(object_)


class CasingJSONEncoder(PydanticJSONEncoder):
    def encode(self, object_: Any) -> Any:
        if isinstance(object_, (list, Mapping)):
            object_ = camelize(object_)
        return super().encode(camelize(object_))


class CasingJSONDecoder(QuartJSONDecoder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, object_hook=self.object_hook, **kwargs)

    def object_hook(self, object_: dict) -> Any:
        return decamelize(object_)


class FlaskSchema:
    """A Flask-Schema instance.

        app = Flask(__name__)
        FlaskSchema(app)

        flask_schema = FlaskSchema()

    or

        def create_app():
            app = Flask(__name__)
            flask_schema.init_app(app)
            return app

    Arguments:
        openapi_path: The path used to serve the openapi json on, or None
            to disable documentation.
        redoc_ui_path: The path used to serve the documentation UI using
            redoc or None to disable redoc documentation.
        swagger_ui_path: The path used to serve the documentation UI using
            swagger or None to disable swagger documentation.
        title: The publishable title for the app.
        version: The publishable version for the app.
    """

    def __init__(
        self,
        app: Optional[Flask] = None,
        *,
        openapi_path: Optional[str] = "/openapi.json",
        redoc_ui_path: Optional[str] = "/redocs",
        swagger_ui_path: Optional[str] = "/docs",
        title: Optional[str] = None,
        version: str = "0.1.0",
        tags: Optional[List[TagObject]] = None,
        convert_casing: bool = False,
        servers: Optional[List[ServerObject]] = [],
    ) -> None:
        self.openapi_path = openapi_path
        self.redoc_ui_path = redoc_ui_path
        self.swagger_ui_path = swagger_ui_path
        self.title = title
        self.version = version
        self.tags: List[TagObject] = tags or []
        self.convert_casing = convert_casing
        self.servers = servers
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        app.extensions["FLASK_SCHEMA"] = self
        self.title = app.name if self.title is None else self.title
        if self.convert_casing:
            app.json_decoder = CasingJSONDecoder
            app.json_encoder = CasingJSONEncoder
        else:
            app.json_encoder = PydanticJSONEncoder

        app.make_response = convert_model_result(
            app.make_response)  # type: ignore

        app.config.setdefault(
            "FLASK_SCHEMA_SWAGGER_JS_URL",
            "https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/3.47.1/swagger-ui-bundle.js",
        )
        app.config.setdefault(
            "FLASK_SCHEMA_SWAGGER_CSS_URL",
            "https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/3.47.1/swagger-ui.min.css",
        )
        app.config.setdefault(
            "FLASK_SCHEMA_REDOC_JS_URL",
            "https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js",
        )

        if self.openapi_path is not None:
            hide_route(app.send_static_file.__func__)  # type: ignore
            app.add_url_rule(self.openapi_path, "openapi", self.openapi)
            if self.redoc_ui_path is not None:
                app.add_url_rule(self.redoc_ui_path, "redoc_ui", self.redoc_ui)
            if self.swagger_ui_path is not None:
                app.add_url_rule(self.swagger_ui_path, "swagger_ui",
                                 self.swagger_ui)

        app.cli.add_command(_schema_command)

    @hide_route
    def openapi(self) -> dict:
        return _build_openapi_schema(current_app, self)

    @hide_route
    def swagger_ui(self) -> str:
        return render_template_string(
            SWAGGER_TEMPLATE,
            title=self.title,
            openapi_path=self.openapi_path,
            swagger_js_url=current_app.config["FLASK_SCHEMA_SWAGGER_JS_URL"],
            swagger_css_url=current_app.config["FLASK_SCHEMA_SWAGGER_CSS_URL"],
        )

    @hide_route
    def redoc_ui(self) -> str:
        return render_template_string(
            REDOC_TEMPLATE,
            title=self.title,
            openapi_path=self.openapi_path,
            redoc_js_url=current_app.config["FLASK_SCHEMA_REDOC_JS_URL"],
        )


def _split_definitions(schema: dict) -> Tuple[dict, dict]:
    new_schema = schema.copy()
    definitions = new_schema.pop("definitions", {})
    return definitions, new_schema


def convert_model_result(func: Callable) -> Callable:
    @wraps(func)
    def decorator(result) -> ResponseReturnValue:
        status_or_headers = None
        headers = None
        if isinstance(result, tuple):
            value, status_or_headers, headers = result + (None,) * (
                3 - len(result))
        else:
            value = result

        if is_dataclass(value):
            dict_or_value = asdict(value)
        elif isinstance(value, BaseModel):
            dict_or_value = value.dict()
        else:
            dict_or_value = value
        return func((dict_or_value, status_or_headers, headers))

    return decorator


def _build_openapi_schema(app: Flask, extension: FlaskSchema) -> dict:
    paths: Dict[str, dict] = {}
    components = {"schemas": {}}  # type: ignore
    for rule in app.url_map.iter_rules():
        func = app.view_functions[rule.endpoint]
        if getattr(func, FLASK_SCHEMA_HIDDEN_ATTRIBUTE, False):
            continue

        path_object = {  # type: ignore
            "parameters": [],
            "responses": {},
        }
        if func.__doc__ is not None:
            summary, *description = func.__doc__.splitlines()
            path_object["description"] = "\n".join(description)
            path_object["summary"] = summary

        response_models = getattr(func, FLASK_SCHEMA_RESPONSE_ATTRIBUTE, {})
        for status_code, model_class in response_models.items():
            schema = model_schema(model_class, ref_prefix=REF_PREFIX)
            if extension.convert_casing:
                schema = camelize(schema)
            definitions, schema = _split_definitions(schema)
            components["schemas"].update(definitions)
            path_object["responses"][status_code] = {  # type: ignore
                "content": {
                    "application/json": {
                        "schema": schema,
                    },
                },
                "description": model_class.__doc__,
            }

        request_data = getattr(func, FLASK_SCHEMA_REQUEST_ATTRIBUTE, None)
        if request_data is not None:
            schema = model_schema(request_data[0], ref_prefix=REF_PREFIX)
            if extension.convert_casing:
                schema = camelize(schema)
            definitions, schema = _split_definitions(schema)
            components["schemas"].update(definitions)

            if request_data[1] == DataSource.JSON:
                encoding = "application/json"
            else:
                encoding = "application/x-www-form-urlencoded"

            path_object["requestBody"] = {
                "content": {
                    encoding: {
                        "schema": schema,
                    },
                },
            }

        querystring_model = getattr(func, FLASK_SCHEMA_QUERYSTRING_ATTRIBUTE,
                                    None)
        if querystring_model is not None:
            schema = model_schema(querystring_model, ref_prefix=REF_PREFIX)
            if extension.convert_casing:
                schema = camelize(schema)
            definitions, schema = _split_definitions(schema)
            components["schemas"].update(definitions)
            for name, type_ in schema["properties"].items():
                path_object["parameters"].append(  # type: ignore
                    {
                        "name": name,
                        "in": "query",
                        "schema": type_,
                    }
                )

        for name, converter in rule._converters.items():
            path_object["parameters"].append(  # type: ignore
                {
                    "name": name,
                    "in": "path",
                }
            )

        path = re.sub(PATH_RE, r"{\1}", rule.rule)
        paths.setdefault(path, {})

        for method in rule.methods:
            if method == "HEAD" or (
                method == "OPTIONS" and rule.provide_automatic_options):  # type: ignore  # noqa: E501
                continue
            paths[path][method.lower()] = path_object

    return {
        "openapi": "3.0.3",
        "info": {
            "title": extension.title,
            "version": extension.version,
        },
        "components": components,
        "paths": paths,
        "tags": extension.tags,
        "servers": extension.servers,
    }
