"""Small, dependency-free helpers shared by all module configurations."""

import json
import math
from pathlib import Path
from dataclasses import fields
from typing import Mapping, get_type_hints


class ConfigError(ValueError):
    """An invalid experiment configuration."""


def require_mapping(value, path):
    if not isinstance(value, Mapping):
        raise ConfigError("{} must be an object".format(path))
    return dict(value)


def integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise ConfigError("{} must be an integer >= {}".format(path, minimum))
    return value


def read_settings(cls, raw, section):
    """Read primitive dataclass fields, rejecting typos and invalid values."""
    raw = require_mapping(raw, section)
    public_fields = {f.name: f for f in fields(cls) if not f.metadata.get("internal")}
    unknown = set(raw) - set(public_fields)
    if unknown:
        raise ConfigError("{}: unknown fields: {}".format(section, ", ".join(sorted(unknown))))
    result = cls(**raw)
    hints = get_type_hints(cls)
    for name, field in public_fields.items():
        value = getattr(result, name)
        path = "{}.{}".format(section, name)
        kind = hints[name]
        if kind is int:
            if type(value) is not int:
                raise ConfigError("{} must be an integer".format(path))
        elif kind is float:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ConfigError("{} must be a finite number".format(path))
        elif kind is str:
            if not isinstance(value, str):
                raise ConfigError("{} must be a string".format(path))
        elif kind is bool:
            if type(value) is not bool:
                raise ConfigError("{} must be a boolean".format(path))
        else:
            raise TypeError("Unsupported configuration field: {}".format(path))
        if "min" in field.metadata and value < field.metadata["min"]:
            raise ConfigError("{} must be >= {}".format(path, field.metadata["min"]))
        if "gt" in field.metadata and value <= field.metadata["gt"]:
            raise ConfigError("{} must be > {}".format(path, field.metadata["gt"]))
        if "lt" in field.metadata and value >= field.metadata["lt"]:
            raise ConfigError("{} must be < {}".format(path, field.metadata["lt"]))
        if "choices" in field.metadata and value not in field.metadata["choices"]:
            raise ConfigError("{} must be one of {}".format(path, field.metadata["choices"]))
    return result


def read_json(path):
    def bad_constant(value):
        raise ValueError("nonfinite JSON value: " + value)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=bad_constant, object_pairs_hook=unique)


def number(value, name, positive=False, integral=False):
    valid_type = type(value) is int if integral else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("{} must be a finite {}{}".format(name, "positive " if positive else "nonnegative ", "integer" if integral else "number"))
    return value
