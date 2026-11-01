"""Types shared by the PII code, the request models, the result models and the settings.

Kept free of imports from the rest of the package, so that the models and the settings can use
them.
"""

from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, GetJsonSchemaHandler, field_validator, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

# What the service looks for: the entity types of Presidio that its recognizers and the spaCy
# models cover.
EntityType = Literal[
    "PERSON",
    "LOCATION",
    "ORGANIZATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "URL",
]
ENTITY_TYPES: tuple[EntityType, ...] = get_args(EntityType)

# What a found entity becomes: ``[<TYPE>]``, ``[REDACTED]``, ``[<TYPE>:<12 hex>]``, or nothing
# (``annotate``: the text stays and the report lists the entities).
PiiMode = Literal["mask", "redact", "hash", "annotate"]

# ``auto`` takes the language of the extraction; ``de`` and ``en`` force that language's model.
PiiLanguageOption = Literal["auto", "de", "en"]
PiiLanguage = Literal["de", "en"]  # the model that was used

# The size of the spaCy models (``PII_MODEL_SIZE``): `lg` finds names better, `md` needs a tenth of
# the volume and half the memory.
ModelSize = Literal["lg", "md"]

# Whose key signed a hash: the operator's (the same value hashes the same in every result) or a
# random one made for this result.
HashScope = Literal["service", "result"]


def model_names(size: ModelSize) -> dict[PiiLanguage, str]:
    """The two spaCy models of a size: the directory names on the models volume, the keys of
    ``pii/models.toml``."""
    if size not in get_args(ModelSize):
        raise ValueError(f"unknown spaCy model size {size!r}")
    return {"de": f"de_core_news_{size}", "en": f"en_core_web_{size}"}


@dataclass(frozen=True)
class Span:
    """An entity found in one string: ``start`` and ``end`` are code point offsets into it
    (``end`` exclusive)."""

    start: int
    end: int
    type: EntityType
    score: float


class PiiOptions(BaseModel, extra="forbid", hide_input_in_errors=True, strict=True):
    """The ``pii`` option of a request. A bare string is the mode: ``"pii": "mask"`` is
    ``{"mode": "mask"}``."""

    mode: PiiMode = Field(
        description="`mask`: an entity becomes `[<TYPE>]`, e.g. `[PERSON]`. `redact`: it becomes "
        "`[REDACTED]`. `hash`: it becomes `[<TYPE>:<12 hex>]`, a keyed hash that is equal for "
        "equal values. `annotate`: the text stays as it is and `pii.items` lists every entity "
        "(not with `format: html`)."
    )
    entities: list[EntityType] | None = Field(
        default=None,
        min_length=1,
        description="The entity types to handle, without duplicates; default: persons and "
        "contact or payment data (`PII_DEFAULT_ENTITIES`). Places, organisations and URLs only "
        "when named.",
    )
    language: PiiLanguageOption = Field(
        default="auto",
        description="The model for names: `auto` takes the language of the extraction (other "
        "languages use the service's fallback, with the warning `pii_language_unsupported`); "
        "`de` or `en` forces it.",
    )
    score_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Least confidence of an entity, 0.0 to 1.0; default: the operator's "
        "(`PII_SCORE_THRESHOLD`). Lower finds more and mistakes more.",
    )

    @model_validator(mode="before")
    @classmethod
    def _bare_string_is_the_mode(cls, data: Any) -> Any:
        return {"mode": data} if isinstance(data, str) else data

    @field_validator("entities")
    @classmethod
    def _no_duplicates(cls, value: list[EntityType] | None) -> list[EntityType] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("entities must not repeat a type")
        return value


class PiiEntity(BaseModel):
    """One entity of ``annotate``: where it is and what it is. ``start`` and ``end`` are code
    points in the string of ``field``; ``text`` is the value itself."""

    field: str
    type: EntityType
    start: int
    end: int
    score: float
    text: str


class PiiReport(BaseModel):
    """What the PII step did to a result (``ExtractResult.pii``)."""

    mode: PiiMode
    language: PiiLanguage  # the model used
    entities: dict[EntityType, int]  # count per type, only the types found
    items: list[PiiEntity] | None = None  # annotate only
    hash_scope: HashScope | None = None  # hash only


class BareModeOrObject:
    """The JSON schema of a request's ``pii`` field: a bare mode (``"pii": "mask"``) or the
    ``PiiOptions`` object, and null where the field is optional. The validator of
    ``PiiOptions`` reads the bare string; this tells generated clients and validating gateways
    that the string is allowed. Use it as ``Annotated[PiiOptions | None, BareModeOrObject()]``.
    """

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        bare_mode: JsonSchemaValue = {"type": "string", "enum": list(get_args(PiiMode))}
        if "anyOf" in schema:  # optional: the object or null
            rest = {key: value for key, value in schema.items() if key != "anyOf"}
            return {**rest, "anyOf": [bare_mode, *schema["anyOf"]]}
        return {"anyOf": [bare_mode, schema]}
