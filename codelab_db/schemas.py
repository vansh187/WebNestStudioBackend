"""Request bodies for POST /query and POST /test (draft B5.2).

The connection URL is a SecretStr so it never shows in a repr, a log line or
a validation error. Bodies are parsed from bytes the router has already
size-capped, never echoed back."""

import json
import logging
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictFloat, StrictInt
from pydantic import StrictStr, TypeAdapter, ValidationError

from codelab_db.constants import LOGGER_NAME, MONGO_OPERATIONS
from codelab_db.errors import BadRequest, DbError

logger = logging.getLogger(LOGGER_NAME)

MAX_URL_LENGTH = 2048
MAX_PARAMS = 100
MAX_JSON_DEPTH = 64
MAX_DOCUMENTS = 500
MAX_PIPELINE_STAGES = 50

Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr, None]
Param = Union[Scalar, list[Scalar]]


def _check_url(value: SecretStr) -> SecretStr:
    try:
        raw = value.get_secret_value()
        if not raw.strip():
            raise ValueError("url is required")
        if len(raw) > MAX_URL_LENGTH:
            raise ValueError(f"url is longer than {MAX_URL_LENGTH} characters")
        return value
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("url could not be read") from exc


def _check_params(value):
    try:
        if value is not None and len(value) > MAX_PARAMS:
            raise ValueError(f"at most {MAX_PARAMS} params are allowed")
        return value
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("params could not be read") from exc


Url = Annotated[SecretStr, AfterValidator(_check_url)]
Params = Annotated[list[Param] | dict[str, Param] | None, AfterValidator(_check_params)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    url: Url


class SqlQueryRequest(_Base):
    engine: Literal["postgres", "mysql"]
    sql: str = Field(min_length=1)
    params: Params = None


class MongoOptions(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    limit: int = Field(default=0, ge=0, le=1_000_000)
    skip: int = Field(default=0, ge=0, le=1_000_000)
    sort: list[tuple[str, Literal[1, -1]]] | dict[str, Literal[1, -1]] | None = None
    projection: dict | None = None
    upsert: bool = False


class MongoQueryRequest(_Base):
    engine: Literal["mongodb"]
    operation: Literal[MONGO_OPERATIONS]  # type: ignore[valid-type]
    database: str | None = Field(default=None, max_length=64)
    collection: str | None = Field(default=None, max_length=120)
    filter: dict | None = None
    update: dict | list[dict] | None = None
    document: dict | None = None
    documents: list[dict] | None = Field(default=None, max_length=MAX_DOCUMENTS)
    pipeline: list[dict] | None = Field(default=None, max_length=MAX_PIPELINE_STAGES)
    options: MongoOptions = Field(default_factory=MongoOptions)


class TestConnectionRequest(_Base):
    __test__ = False   # not a pytest test class

    engine: Literal["postgres", "mysql", "mongodb"]


QueryRequest = Annotated[Union[SqlQueryRequest, MongoQueryRequest], Field(discriminator="engine")]
_query_adapter = TypeAdapter(QueryRequest)


def _depth(value: object) -> int:
    """Nesting depth of decoded JSON, without recursion."""
    try:
        deepest = 0
        stack = [(value, 1)]
        while stack:
            current, level = stack.pop()
            deepest = max(deepest, level)
            if deepest > MAX_JSON_DEPTH:
                return deepest
            if isinstance(current, dict):
                stack.extend((child, level + 1) for child in current.values())
            elif isinstance(current, list):
                stack.extend((child, level + 1) for child in current)
        return deepest
    except Exception as exc:
        raise BadRequest("the body could not be read") from exc


def _decode(body: bytes) -> object:
    try:
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise BadRequest("the body must be a JSON object")
        if _depth(data) > MAX_JSON_DEPTH:
            raise BadRequest(f"the body is nested more than {MAX_JSON_DEPTH} levels deep")
        return data
    except DbError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BadRequest("the body must be valid JSON") from exc


def _first_problem(error: ValidationError) -> str:
    try:
        problem = error.errors(include_input=False)[0]
        where = ".".join(str(part) for part in problem.get("loc", ()))
        message = str(problem.get("msg", "invalid value")).removeprefix("Value error, ")
        return f"{where}: {message}" if where else message
    except Exception:
        logger.warning("could not describe a database proxy validation error", exc_info=True)
        return "invalid value"


def parse_query_request(body: bytes) -> SqlQueryRequest | MongoQueryRequest:
    try:
        return _query_adapter.validate_python(_decode(body))
    except DbError:
        raise
    except ValidationError as exc:
        raise BadRequest(_first_problem(exc)) from exc
    except Exception as exc:
        logger.warning("database proxy query body could not be parsed type=%s", type(exc).__name__)
        raise BadRequest("the body could not be read") from exc


def parse_test_request(body: bytes) -> TestConnectionRequest:
    try:
        return TestConnectionRequest.model_validate(_decode(body))
    except DbError:
        raise
    except ValidationError as exc:
        raise BadRequest(_first_problem(exc)) from exc
    except Exception as exc:
        logger.warning("database proxy test body could not be parsed type=%s", type(exc).__name__)
        raise BadRequest("the body could not be read") from exc
